#!/usr/bin/env bash
# Linux/WSL entrypoint. Python owns child process groups and shutdown.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec uv run --locked python - "$@" <<'PY'
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from checkpoint.server.__main__ import config_from_env


class StartupError(Exception):
    pass


def stop(signum, _frame):
    raise SystemExit(128 + signum)


def cleanup(children):
    # Each child owns its session; never signal the caller's process group.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, signal.SIG_IGN)
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    for child in children:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # Kill remaining group members even if their direct parent already exited.
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def main():
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Usage: ./scripts/run_all.sh [ENGINE_ARGS...] | --demo")
        return
    demo = bool(args and args[0] == "--demo")
    if demo and len(args) != 1:
        raise StartupError("Demo mode does not accept engine arguments.")
    load_dotenv(Path.cwd() / ".env")
    try:
        config = config_from_env(os.environ)
    except ValueError:
        raise StartupError("Invalid Checkpoint configuration.") from None
    ports = [config.APP_PORT] if demo else [config.APP_PORT, config.ENGINE_AUDIO_PORT]
    if len(set(ports)) != len(ports):
        raise StartupError("Server and engine ports must differ.")
    # Check all ports before starting either child. A later bind race is handled
    # like any other child startup failure, without killing an existing owner.
    for port in ports:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                raise StartupError(f"Required port {port} is unavailable.") from None
    os.environ["APP_PORT"] = str(config.APP_PORT)
    os.environ["ENGINE_AUDIO_PORT"] = str(config.ENGINE_AUDIO_PORT)
    children = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        server = subprocess.Popen(
            [sys.executable, "-m", "checkpoint.server", *(["--demo"] if demo else [])],
            start_new_session=True,
        )
        children.append(server)
        base = f"http://127.0.0.1:{config.APP_PORT}"
        # Proxy environment settings must never route local control requests out.
        http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 10
        while True:
            if server.poll() is not None:
                raise StartupError("Server exited before it was ready.")
            try:
                with http.open(base + "/mic", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise StartupError("Server did not become ready.")
            time.sleep(0.05)
        if demo:
            def post(route, data):
                request = urllib.request.Request(
                    base + route,
                    data=json.dumps(data).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with http.open(request, timeout=2) as response:
                    result = json.load(response)
                    if result.get("ok") is not True:
                        raise StartupError("Demo setup failed.")
                    if route == "/dock/debug/chat" and result.get("queued") is not True:
                        raise StartupError("Demo question could not be queued.")
            post("/dock/focus", {"on": True})
            for user, text in (
                ("river", "What editor are you using?"),
                ("sage", "How do you stay focused while coding?"),
                ("ember", "Which keyboard do you use?"),
            ):
                post("/dock/debug/chat", {"user": user, "text": text})
            print("Demo: three sample questions ready; no Twitch or STT engine.", flush=True)
        else:
            children.append(subprocess.Popen(
                [sys.executable, "-m", "checkpoint.stt", "--mic", *args],
                start_new_session=True,
            ))
        print(f"Dock: {base}/dock/\nOverlay: {base}/overlay/", flush=True)
        if not demo:
            print(f"Mic: {base}/mic", flush=True)
        print("Press Ctrl-C to stop Checkpoint.", flush=True)
        while True:
            for child in children:
                if child.poll() is not None:
                    raise StartupError("A Checkpoint process exited; stopping the others.")
            time.sleep(0.1)
    finally:
        cleanup(children)


try:
    main()
except (StartupError, OSError, urllib.error.URLError, ValueError):
    # Do not render exception strings: they may contain environment-derived data.
    print("Checkpoint could not start or continue; check configuration, ports and child logs.", file=sys.stderr)
    sys.exit(1)
PY
