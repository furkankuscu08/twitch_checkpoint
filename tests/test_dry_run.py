"""Offline rehearsal tooling: real timings, private inputs, owned processes only."""

import json
import os
import socket
import sys

import pytest


def scenario_files(path):
    first = [
        {"account": "test2", "delay_s": 6, "text": "What editor is that?"},
        {"account": "test3", "delay_s": 8, "text": "How long did building take?"},
        {"account": "test2", "delay_s": 10, "text": "Where do you live?"},
        {"account": "test3", "delay_s": 10, "text": "Please stream on weekends"},
    ]
    second = [{"account": "test3", "delay_s": 3, "text": "Which editor do you use?"}]
    metadata = {
        "users": {"test2": "river", "test3": "sage"},
        "answers": ["My favourite editor.", "Two days of work.", None, "Maybe later."],
    }
    for name, value in (("part1", first), ("part2", second), ("replay", metadata)):
        (path / f"{name}.json").write_text(json.dumps(value))
    return metadata


def test_loads_private_scenario_and_preserves_relative_delays(tmp_path):
    from scripts.dry_run import load_scenario

    scenario_files(tmp_path)
    scenario = load_scenario(tmp_path)
    assert [entry.delay_s for entry in scenario.part1] == [6, 8, 10, 10]
    assert scenario.users == {"test2": "river", "test3": "sage"}
    assert scenario.answers[2] is None


@pytest.mark.parametrize("bad", ["test", "unknown"])
def test_rejects_unapproved_account_before_starting_server(tmp_path, bad):
    from scripts.dry_run import load_scenario

    scenario_files(tmp_path)
    path = tmp_path / "part1.json"
    data = json.loads(path.read_text())
    data[0]["account"] = bad
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="account"):
        load_scenario(tmp_path)


def test_hypotheses_use_cumulative_realistic_word_times_and_exact_words():
    from scripts.dry_run import hypotheses

    events = list(hypotheses("Hello, this is spoken exactly.", "turn-one", 1000.0))
    assert len(events) == 6
    assert all(delay > 0 for delay, _ in events)
    previous = []
    for seq, (_, body) in enumerate(events, 1):
        assert body["seq"] == seq and body["utt_id"] == "turn-one"
        assert body["utt_start_wall"] == 1000.0
        assert body["words"][: len(previous)] == previous
        previous = body["words"]
        assert all(0.1 <= w["end"] - w["start"] <= 0.5 for w in previous)
        assert all(a["end"] <= b["start"] for a, b in zip(previous, previous[1:]))
    assert [body["final"] for _, body in events] == [False] * 5 + [True]
    assert " ".join(w["text"] for w in previous) == "Hello, this is spoken exactly."


def test_partial_only_next_never_synthesizes_a_final():
    from scripts.dry_run import hypotheses

    events = list(hypotheses("Please keep these words.", "next-turn", 10, final=False))
    assert len(events) == 4
    assert all(not body["final"] for _, body in events)


@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt"])
def test_owned_server_stops_and_releases_port(tmp_path, outcome):
    from scripts.dry_run import owned_server

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    pid = None
    try:
        with owned_server(tmp_path, port=port) as server:
            pid = server.process.pid
            assert server.process.args[:4] == [
                sys.executable,
                "-m",
                "checkpoint.server",
                "--demo",
            ]
            assert server.process.poll() is None
            if outcome == "failure":
                raise RuntimeError("simulated replay failure")
            if outcome == "interrupt":
                raise KeyboardInterrupt
    except (RuntimeError, KeyboardInterrupt):
        assert outcome != "success"
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))


@pytest.mark.browser
def test_replay_stamp_capture_waits_for_the_visible_checkmark(tmp_path):
    from playwright.sync_api import sync_playwright

    from scripts.dry_run import Replay, load_scenario, owned_server

    scenario_files(tmp_path)
    scenario = load_scenario(tmp_path)
    with owned_server(tmp_path) as server, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        replay = Replay(browser, server, scenario, tmp_path)
        original_shot = replay.shot

        def inspect_stamp(beat):
            if beat.endswith("-stamp"):
                assert (
                    replay.overlay.locator("#stamp").evaluate(
                        "node => getComputedStyle(node).opacity"
                    )
                    == "1"
                ), "Stamp screenshot was taken before the checkmark landed"
            original_shot(beat)

        replay.shot = inspect_stamp
        try:
            replay.button("Focus")
            entry = scenario.part1[0]
            result = replay.post(
                "/dock/debug/chat",
                {
                    "user": scenario.users[entry.account],
                    "text": entry.text,
                },
            )
            replay.ids.append(result["id"])
            replay.wait(lambda: replay.event("queued", id=result["id"]) is not None)
            replay.button("Checkpoint")
            replay.open_card(0)
            replay.speak(scenario.answers[0], "first-answer")
            replay.auto_close(0)
            assert all(check["passed"] for check in replay.checks)
        finally:
            replay.close()
            browser.close()
