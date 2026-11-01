"""Launcher subprocess checks use a fake app, no keys or external services."""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_APP = r"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace


def config_from_env(environ):
    return SimpleNamespace(
        APP_PORT=int(environ.get("APP_PORT", 8200)),
        ENGINE_AUDIO_PORT=int(environ.get("ENGINE_AUDIO_PORT", 8766)),
    )


def record(**data):
    with open(os.environ["FAKE_LOG"], "a") as target:
        target.write(json.dumps(data) + "\n")


def run():
    role = __package__.rsplit(".", 1)[-1]
    record(role=role, pid=os.getpid(), argv=sys.argv[1:])
    if os.environ.get("FAKE_FAIL") == role:
        raise SystemExit(7)
    if role == "stt":
        port = int(os.environ["ENGINE_AUDIO_PORT"])
        listener = socket.socket()
        listener.bind(("127.0.0.1", port))
        listener.listen()
        descendant = None
        if os.environ.get("FAKE_DESCENDANT"):
            descendant = subprocess.Popen([
                sys.executable, "-c",
                "import socket,time; "
                "s=socket.socket(); "
                "s.bind(('127.0.0.1'," + os.environ["FAKE_DESCENDANT"] + ")); "
                "s.listen(); time.sleep(60)",
            ])
            record(role="descendant", pid=descendant.pid)
        def stop(*_):
            if descendant is not None:
                descendant.wait(timeout=3)
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while True:
            time.sleep(0.02)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            if os.environ.get("FAKE_UNREADY"):
                self.send_response(503)
            else:
                self.send_response(200)
            self.end_headers()
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            record(path=self.path, body=data)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"queued":true}')
    HTTPServer(("127.0.0.1", int(os.environ["APP_PORT"])), Handler).serve_forever()


if __name__ == "__main__":
    run()
"""


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def available(port):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    assert predicate(), "launcher did not reach the expected state"


@pytest.fixture
def checkout(tmp_path):
    script = ROOT / "scripts/run_all.sh"
    assert script.exists(), "WP6 launcher has not been implemented"
    (tmp_path / "scripts").mkdir()
    shutil.copy2(script, tmp_path / "scripts/run_all.sh")
    for name in ("server", "stt"):
        package = tmp_path / "checkpoint" / name
        package.mkdir(parents=True)
        (package / "__init__.py").touch()
        (package / "__main__.py").write_text(FAKE_APP)
    (tmp_path / "checkpoint/__init__.py").touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        "import os,sys\n"
        "args=sys.argv[1:]\n"
        "assert args[:3] == ['run', '--locked', 'python']\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *args[3:]])\n"
    )
    fake_uv.chmod(0o755)
    app_port, engine_port = free_port(), free_port()
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        APP_PORT=str(app_port),
        ENGINE_AUDIO_PORT=str(engine_port),
        FAKE_LOG=str(tmp_path / "calls.jsonl"),
    )
    for key in ("FAKE_FAIL", "FAKE_DESCENDANT", "FAKE_UNREADY"):
        env.pop(key, None)
    processes = []

    def calls():
        path = tmp_path / "calls.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def start(*args, updates=None):
        process = subprocess.Popen(
            ["bash", str(tmp_path / "scripts/run_all.sh"), *args],
            cwd=tmp_path.parent,
            env=dict(env, **(updates or {})),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        processes.append(process)
        return process

    yield tmp_path, env, calls, start
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=3)
    # A failed test must not leave fake child processes behind.
    for item in calls():
        pid = item.get("pid")
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_normal_mode_passes_engine_args_and_releases_both_ports(checkout, stop_signal):
    _, env, calls, start = checkout
    process = start("--save-hyps", "data/rehearsal hyps.jsonl")
    wait_until(lambda: any(call.get("role") == "stt" for call in calls()))
    wait_until(lambda: not available(int(env["ENGINE_AUDIO_PORT"])))
    roles = [call for call in calls() if "role" in call]
    assert [call["role"] for call in roles] == ["server", "stt"]
    assert roles[0]["argv"] == []
    assert roles[1]["argv"] == ["--mic", "--save-hyps", "data/rehearsal hyps.jsonl"]
    process.send_signal(stop_signal)
    output, _ = process.communicate(timeout=8)
    assert process.returncode in (0, 128 + stop_signal), output
    assert available(int(env["APP_PORT"]))
    assert available(int(env["ENGINE_AUDIO_PORT"]))
    for role in roles:
        with pytest.raises(ProcessLookupError):
            os.kill(role["pid"], 0)


def test_demo_starts_only_server_then_seeds_existing_endpoints(checkout):
    path, env, calls, start = checkout
    (path / ".env").write_text(
        "TWITCH_CLIENT_ID=fake-client\nDEEPGRAM_API_KEY=fake-key\n"
    )
    process = start("--demo")
    wait_until(lambda: len([call for call in calls() if "path" in call]) == 4)
    roles = [call for call in calls() if "role" in call]
    assert len(roles) == 1
    assert roles[0]["role"] == "server" and roles[0]["argv"] == ["--demo"]
    posts = [call for call in calls() if "path" in call]
    assert posts[0] == dict(path="/dock/focus", body={"on": True})
    assert [post["path"] for post in posts[1:]] == ["/dock/debug/chat"] * 3
    assert len({post["body"]["user"] for post in posts[1:]}) == 3
    assert all(post["body"]["text"].endswith("?") for post in posts[1:])
    assert all("test" not in post["body"]["user"] for post in posts[1:])
    assert available(int(env["ENGINE_AUDIO_PORT"]))
    process.terminate()
    output, _ = process.communicate(timeout=8)
    assert process.returncode in (0, 143), output
    assert available(int(env["APP_PORT"]))
    assert "fake-key" not in output and "fake-client" not in output


def test_env_file_ports_and_explicit_environment_precedence(checkout):
    path, env, calls, start = checkout
    app_port = free_port()
    (path / ".env").write_text(f"APP_PORT={app_port}\nENGINE_AUDIO_PORT=1\n")
    env.pop("APP_PORT")
    process = start("--demo")
    wait_until(lambda: len([call for call in calls() if "path" in call]) == 4)
    assert not available(app_port)
    process.terminate()
    output, _ = process.communicate(timeout=8)
    assert process.returncode in (0, 143), output
    assert available(app_port)


@pytest.mark.parametrize("occupied", ["APP_PORT", "ENGINE_AUDIO_PORT"])
def test_busy_port_rejects_before_starting_any_child(checkout, occupied):
    _, env, calls, start = checkout
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", int(env[occupied])))
        listener.listen()
        process = start()
        output, _ = process.communicate(timeout=8)
        assert process.returncode != 0, output
        assert calls() == []
        assert not available(int(env[occupied]))


def test_demo_does_not_require_unused_engine_port(checkout):
    _, env, calls, start = checkout
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", int(env["ENGINE_AUDIO_PORT"])))
        listener.listen()
        process = start("--demo")
        wait_until(lambda: len([call for call in calls() if "path" in call]) == 4)
        process.terminate()
        process.communicate(timeout=8)
        assert not available(int(env["ENGINE_AUDIO_PORT"]))


@pytest.mark.parametrize("role", ["server", "stt"])
def test_child_failure_stops_every_started_process(checkout, role):
    _, env, calls, start = checkout
    process = start(updates={"FAKE_FAIL": role})
    output, _ = process.communicate(timeout=8)
    assert process.returncode != 0, output
    assert available(int(env["APP_PORT"]))
    assert available(int(env["ENGINE_AUDIO_PORT"]))
    for call in calls():
        if "pid" in call:
            with pytest.raises(ProcessLookupError):
                os.kill(call["pid"], 0)


def test_shutdown_terminates_child_descendants(checkout):
    _, env, calls, start = checkout
    descendant_port = free_port()
    process = start(updates={"FAKE_DESCENDANT": str(descendant_port)})
    wait_until(lambda: not available(descendant_port))
    process.terminate()
    output, _ = process.communicate(timeout=8)
    assert process.returncode in (0, 143), output
    assert available(descendant_port)
    assert available(int(env["APP_PORT"]))
    assert available(int(env["ENGINE_AUDIO_PORT"]))


def test_invalid_demo_engine_args_fail_without_children(checkout):
    _, _, calls, start = checkout
    process = start("--demo", "--save-hyps", "data/demo.jsonl")
    output, _ = process.communicate(timeout=8)
    assert process.returncode != 0, output
    assert calls() == []
