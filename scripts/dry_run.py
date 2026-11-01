"""Replay the private four-card recording scenario without external services.

Usage: uv run python scripts/dry_run.py --scenario docs/private/demo
Inputs: part1.json (four messages), part2.json (one repeated question), and
replay.json: {"users": {"test2": "river", "test3": "sage"},
"answers": ["First answer.", "Second answer.", null, "Last answer."]}.
The second question is read aloud as a separate echo turn. The third card is
skipped; the fourth uses Next on a partial, then Cancel quote. Timings are real.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.core.config import Config  # noqa: E402
from scripts.chat_script import validate_script  # noqa: E402
from scripts.shoot import VIEWPORTS, capture  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Scenario:
    part1: tuple
    part2: tuple
    users: dict
    answers: tuple


def load_scenario(directory):
    directory = Path(directory)
    parts = [
        validate_script(json.loads((directory / f"part{i}.json").read_text()), Config())
        for i in (1, 2)
    ]
    if [len(part) for part in parts] != [4, 1]:
        raise ValueError("Expected four cards followed by one repeated question")
    data = json.loads((directory / "replay.json").read_text())
    users, answers = data["users"], data["answers"]
    if (
        not isinstance(users, dict)
        or set(users) != {"test2", "test3"}
        or any(
            not isinstance(u, str) or not re.fullmatch(r"\w+", u, re.ASCII)
            for u in users.values()
        )
        or any(entry.account not in users for part in parts for entry in part)
    ):
        raise ValueError("Only test2/test3 accounts with valid debug users are allowed")
    if (
        not isinstance(answers, list)
        or len(answers) != 4
        or answers[2] is not None
        or any(
            not isinstance(answers[i], str) or not answers[i].strip() for i in (0, 1, 3)
        )
    ):
        raise ValueError("Expected answers for cards 1, 2 and 4; null for Skip")
    return Scenario(*parts, users, tuple(answers))


def hypotheses(text, uid, wall, *, final=True):
    """Cumulative Updates at spoken word ends, then an actual simulated final."""
    words, previous = [], 0.0
    for seq, token in enumerate(text.split(), 1):
        start = words[-1]["end"] + 0.03 if words else 0.0
        end = start + max(0.16, min(0.4, 0.12 + len(token) * 0.035))
        words.append(dict(text=token, start=start, end=end, prob=1.0))
        yield (
            end - previous,
            dict(
                utt_id=uid, seq=seq, final=False, words=list(words), utt_start_wall=wall
            ),
        )
        previous = end
    if final:
        yield (
            0.15,
            dict(
                utt_id=uid,
                seq=len(words) + 1,
                final=True,
                words=list(words),
                utt_start_wall=wall,
            ),
        )


@contextmanager
def owned_server(output, *, port=None):
    """Start the real --demo entrypoint; never attach to another local server."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port or 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, APP_PORT=str(port))
    # --demo is the isolation boundary. Empty credentials also prevent accidental
    # use by this helper; dotenv cannot overwrite these explicit values.
    env.update(TWITCH_CLIENT_ID="", DEEPGRAM_API_KEY="")
    base = f"http://127.0.0.1:{port}"
    with (output / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "checkpoint.server", "--demo"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            with httpx.Client(timeout=0.3, trust_env=False) as http:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Owned demo server exited during startup")
                    try:
                        if http.get(base + "/mic").is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Owned demo server did not become ready")
                    time.sleep(0.05)
            yield SimpleNamespace(base=base, process=process, port=port)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


class Replay:
    def __init__(self, browser, server, scenario, output):
        self.scenario, self.output = scenario, Path(output)
        self.events, self.checks, self.errors, self.ids = [], [], [], []
        self.context = browser.new_context()
        self.context.route(
            "**/*",
            lambda route: (
                route.continue_()
                if route.request.url.startswith(server.base + "/")
                else route.abort()
            ),
        )
        self.pages = {}
        for name in ("dock", "overlay"):
            page = self.context.new_page()
            page.set_viewport_size(VIEWPORTS[name])
            page.set_default_timeout(4000)
            page.on("pageerror", lambda error: self.errors.append(str(error)))
            page.on(
                "websocket",
                lambda ws, audience=name: ws.on(
                    "framereceived",
                    lambda body: self.events.append((audience, json.loads(body))),
                ),
            )
            page.goto(server.base + "/" + name)
            page.wait_for_function("window.checkpointReady === true")
            self.pages[name] = page
        self.dock, self.overlay = self.pages["dock"], self.pages["overlay"]
        self.http = httpx.Client(base_url=server.base, timeout=3, trust_env=False)
        self.wait(lambda: self.event("snapshot") is not None)
        stats = self.event("snapshot")["stats"]
        self.grace, self.cancel = stats["answer_grace_s"], stats["cancel_window_s"]
        if (self.grace, self.cancel) != (2.5, 5):
            raise ValueError("Replay requires unchanged default recording timings")

    def event(self, kind, *, id=None, after=0, audience="dock"):
        return next(
            (
                event
                for target, event in reversed(self.events[after:])
                if target == audience
                and event["type"] == kind
                and (id is None or event.get("id") == id)
            ),
            None,
        )

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("Timed out waiting for the expected replay beat")
            self.dock.wait_for_timeout(25)

    def pause_until(self, target):
        while time.monotonic() < target:
            self.dock.wait_for_timeout(min(250, (target - time.monotonic()) * 1000))

    def check(self, name, condition):
        self.checks.append(dict(check=name, passed=bool(condition)))
        print(f"{'PASS' if condition else 'FAIL'} {name}", flush=True)

    def post(self, path, body):
        response = self.http.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def button(self, name):
        route = name.lower().split()[0]
        with self.dock.expect_response(
            lambda response: response.url.endswith("/dock/" + route)
        ) as response:
            self.dock.get_by_role("button", name=name, exact=True).click()
        if not response.value.ok:
            raise AssertionError(f"Dock action {route} failed")

    def shot(self, beat):
        for name in ("overlay", "dock"):
            capture(self.pages[name], name, self.output / f"{beat}-{name}.png")

    def speak(self, text, uid, *, final=True):
        target, wall = time.monotonic(), time.time()
        for delay, body in hypotheses(text, uid, wall, final=final):
            target += delay
            self.pause_until(target)
            self.post("/stt/hypothesis", body)
        self.dock.wait_for_timeout(30)
        return time.monotonic()

    def inject(self, entries, *, beat):
        previous = time.monotonic()
        for index, entry in enumerate(entries, 1):
            self.pause_until(previous + entry.delay_s)
            result = self.post(
                "/dock/debug/chat",
                dict(
                    user=self.scenario.users[entry.account],
                    text=entry.text,
                ),
            )
            previous = time.monotonic()
            self.ids.append(result["id"])
            self.check(
                f"{beat} message {index} kept by intake/noise filter",
                result["queued"] is True,
            )
            self.wait(lambda: self.event("queued", id=result["id"]) is not None)
            self.shot(f"{beat}-queued-{index}")

    def open_card(self, index):
        uid = self.ids[index]
        self.wait(lambda: self.event("card_open", id=uid) is not None)
        self.overlay.locator("#question").wait_for(state="visible")
        self.wait(
            lambda: (
                self.overlay.locator("#question").inner_text()
                == self.scenario.part1[index].text
            )
        )
        self.overlay.wait_for_function("""() => {
          const card = document.querySelector('.card');
          return card && getComputedStyle(card).opacity === '1';
        }""")
        self.check(
            f"card {index + 1} order and text",
            (
                self.event("card_open", id=uid)["index"] == index + 1
                and self.dock.locator("#card-question").inner_text()
                == self.scenario.part1[index].text
            ),
        )
        self.shot(f"card-{index + 1}-open")

    def auto_close(self, index):
        uid = self.ids[index]
        answer = self.event("answer", id=uid)
        deadline = answer["grace_until"]
        self.check(f"card {index + 1} grace starts after final", deadline is not None)
        self.shot(f"card-{index + 1}-grace")
        self.pause_until(time.monotonic() + max(0, deadline - time.time() - 0.12))
        self.check(
            f"card {index + 1} stays open before 2.5 s",
            (self.event("card_close", id=uid) is None),
        )
        self.wait(lambda: self.event("card_close", id=uid) is not None, timeout=2)
        self.overlay.wait_for_function("""() => {
          const stamp = document.getElementById('stamp');
          return stamp && !stamp.hidden && getComputedStyle(stamp).opacity === '1';
        }""")
        self.shot(f"card-{index + 1}-stamp")
        pending = self.event("reply_pending", after=0)
        self.check(
            f"card {index + 1} closes at grace deadline",
            (abs(pending["send_at"] - self.cancel - deadline) < 0.2),
        )
        expected = f'🎙 answered on stream: "{self.scenario.answers[index]}"'
        self.check(
            f"card {index + 1} exact quote and parent",
            (
                pending["message_id"] == uid
                and pending["kind"] == "quote"
                and pending["text"] == expected
            ),
        )
        return pending["send_at"] - self.cancel

    def run(self):
        self.button("Focus")
        self.shot("focus")
        self.inject(self.scenario.part1, beat="part1")
        self.pause_until(time.monotonic() + 5)
        self.button("Checkpoint")
        self.open_card(0)
        self.speak(self.scenario.answers[0], "card-1-answer")
        self.check(
            "card 1 has no echo",
            self.event("answer", id=self.ids[0])["echo_text"] == "",
        )
        first_closed = self.auto_close(0)
        self.open_card(1)
        ended = self.speak(self.scenario.part1[1].text, "card-2-echo")
        answer = self.event("answer", id=self.ids[1])
        self.check(
            "card 2 question is an echo without grace or answer",
            (
                answer["echo_text"] == self.scenario.part1[1].text
                and answer["final_text"] == ""
                and answer["grace_until"] is None
            ),
        )
        self.check(
            "card 2 echo is greyed on overlay",
            self.overlay.evaluate("""() => {
          const echo = getComputedStyle(document.getElementById('echo-text'));
          const answer = getComputedStyle(document.getElementById('final-text'));
          return echo.color !== answer.color && Number(echo.opacity) < 1;
        }"""),
        )
        self.shot("card-2-echo")
        self.pause_until(ended + 1)
        self.speak(self.scenario.answers[1], "card-2-answer")
        self.auto_close(1)
        self.open_card(2)
        self.button("Skip")
        self.wait(lambda: self.event("card_close", id=self.ids[2]) is not None)
        self.check(
            "card 3 skipped without a reply",
            (
                self.event("card_close", id=self.ids[2])["result"] == "skipped"
                and not any(
                    e.get("message_id") == self.ids[2]
                    for _, e in self.events
                    if e["type"] == "reply_pending"
                )
            ),
        )
        self.shot("card-3-skipped")
        self.open_card(3)
        self.speak(self.scenario.answers[3], "card-4-answer", final=False)
        self.shot("card-4-partial")
        self.button("Next")
        self.wait(lambda: self.event("reply_pending")["message_id"] == self.ids[3])
        pending = self.event("reply_pending")
        self.check(
            "card 4 Next quotes the complete live partial",
            (pending["text"] == f'🎙 answered on stream: "{self.scenario.answers[3]}"'),
        )
        self.button("Cancel quote")
        self.wait(
            lambda: (
                self.event("snapshot")["pending"]
                and any(
                    p["id"] == pending["id"] and p["kind"] == "plain"
                    for p in self.event("snapshot")["pending"]
                )
            )
        )
        receipt = next(
            p for p in self.event("snapshot")["pending"] if p["id"] == pending["id"]
        )
        self.check(
            "card 4 Cancel quote becomes plain receipt",
            (
                receipt["text"]
                == "🎙 answered on stream, @"
                + self.scenario.users[self.scenario.part1[3].account]
            ),
        )
        self.shot("card-4-cancelled-and-summary")
        summary = self.event("summary")
        self.check(
            "receipt counts as answered; summary is 3 answered · 1 skipped",
            (
                summary == dict(type="summary", answered=3, earlier=0, skipped=1)
                and self.overlay.locator("#summary-counts").inner_text()
                == "3 answered · 1 skipped"
                and self.dock.locator("#summary").inner_text().splitlines()
                == ["CHECKPOINT SAVED ✓", "3 answered · 1 skipped"]
            ),
        )
        self.wait(
            lambda: self.event("reply_sent", id=pending["id"]) is not None,
            timeout=self.cancel + 2,
        )
        deliveries = [
            e for a, e in self.events if a == "dock" and e["type"] == "reply_sent"
        ]
        self.check(
            "two quotes and one receipt suppressed in demo",
            (
                len(deliveries) == 3
                and all(not e["is_sent"] for e in deliveries)
                and [e["kind"] for e in deliveries] == ["quote", "quote", "plain"]
            ),
        )
        self.shot("part1-delivered-locally")
        print(
            "Waiting three real minutes from card 1 close (no injected clock).",
            flush=True,
        )
        while time.time() < first_closed + 180:
            self.pause_until(
                time.monotonic() + min(20, first_closed + 180 - time.time())
            )
            print("Three-minute earlier-answer wait in progress.", flush=True)
        self.inject(self.scenario.part2, beat="part2")
        chip = self.dock.get_by_role(
            "button", name="Send answer from 3 min ago", exact=True
        )
        chip.wait_for(state="visible")
        row = self.event("queued", id=self.ids[4])
        self.check(
            "part 2 chip matches card 1 exact quote at 3 min",
            (row["earlier"] == dict(quote=self.scenario.answers[0], ago_min=3)),
        )
        self.shot("part2-earlier-chip")
        chip.click()
        self.wait(lambda: self.event("removed", id=self.ids[4]) is not None)
        self.dock.wait_for_timeout(300)
        self.check(
            "earlier click removes row without claiming a live success",
            (
                not self.dock.locator(".earlier").count()
                and self.event("earlier_sent") is None
                and not self.overlay.locator("#earlier-toast").is_visible()
            ),
        )
        self.shot("part2-earlier-clicked")
        self.check("both pages have no JavaScript errors", not self.errors)

    def close(self):
        self.http.close()
        self.context.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=ROOT / "docs/private/demo")
    parser.add_argument("--output", type=Path, default=ROOT / "data/shots/dry-run")
    args = parser.parse_args(argv)
    scenario = load_scenario(args.scenario)
    from playwright.sync_api import sync_playwright

    replay = None
    try:
        with owned_server(args.output) as server, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                replay = Replay(browser, server, scenario, args.output)
                replay.run()
            finally:
                if replay is not None:
                    replay.close()
                browser.close()
    except Exception as error:
        if replay is None:
            raise
        replay.check(f"replay stopped: {type(error).__name__}", False)
        print(str(error), flush=True)
    finally:
        if replay is not None:
            (args.output / "report.json").write_text(
                json.dumps(replay.checks, indent=2)
            )
            (args.output / "events.json").write_text(
                json.dumps(replay.events, indent=2)
            )
    return 0 if replay and all(check["passed"] for check in replay.checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
