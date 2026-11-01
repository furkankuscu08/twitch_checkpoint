"""WP5 offline acceptance, using only messages captured from Windows TTS."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from minefield.core.config import Config
from minefield.stt.audio import AudioInput, wav_chunks
from minefield.stt.backends import Mapper, connection_url
from minefield.stt.engine import Engine

FIXTURES = Path(__file__).parent / "fixtures/deepgram"


def recording(backend):
    return [
        json.loads(line)
        for line in (FIXTURES / f"{backend}.jsonl").read_text().splitlines()
    ]


@pytest.mark.parametrize("backend", ["flux", "nova3"])
def test_recorded_tts_maps_three_complete_utterances(backend):
    mapper = Mapper(backend, "process", 1, 1000)
    hypotheses = [
        hyp
        for row in recording(backend)
        for hyp in mapper.process(row["message"], row["received_wall"])
    ]
    finals = [hyp for hyp in hypotheses if hyp["final"]]
    assert len(finals) == 3
    for hyp, word in zip(finals, ("pineapple", "volcano", "banana"), strict=True):
        assert word in " ".join(w["text"] for w in hyp["words"]).lower()
        assert all(0 <= w["start"] <= w["end"] for w in hyp["words"])
        assert all(0 <= w["prob"] <= 1 for w in hyp["words"])
    assert len({hyp["utt_id"] for hyp in finals}) == 3
    for utt_id in {hyp["utt_id"] for hyp in hypotheses}:
        group = [hyp for hyp in hypotheses if hyp["utt_id"] == utt_id]
        assert [hyp["seq"] for hyp in group] == list(range(len(group)))
        assert len({hyp["utt_start_wall"] for hyp in group}) == 1
        assert sum(hyp["final"] for hyp in group) == 1
    banana = next(w for w in finals[-1]["words"] if "banana" in w["text"].lower())
    assert 1010 < finals[-1]["utt_start_wall"] + banana["start"] < 1013


def test_flux_empty_updates_duplicates_and_unchanged_words():
    rows = recording("flux")
    mapper = Mapper("flux", "p", 1, 1000)
    empty = next(
        r["message"]
        for r in rows
        if r["message"].get("event") == "Update" and not r["message"]["words"]
    )
    assert mapper.process(empty, 1001) == []
    update = next(
        r["message"]
        for r in rows
        if r["message"].get("event") == "Update" and r["message"]["words"]
    )
    first = mapper.process(update, 1002)
    assert len(first) == 1 and not first[0]["final"]
    assert mapper.process(update, 1002) == []
    repeated = dict(update, sequence_id=update["sequence_id"] + 1)
    second = mapper.process(repeated, 1003)
    assert second[0]["words"] == first[0]["words"]
    assert second[0]["seq"] == first[0]["seq"] + 1


def test_abort_does_not_finalize_and_ids_change_across_sessions_and_processes():
    update = next(
        r["message"]
        for r in recording("flux")
        if r["message"].get("event") == "Update" and r["message"]["words"]
    )
    ids = []
    for process, session, anchor in (
        ("one", 1, 1000),
        ("one", 2, 2000),
        ("two", 1, 3000),
    ):
        mapper = Mapper("flux", process, session, anchor)
        hyp = mapper.process(update, anchor + 1)[0]
        ids.append(hyp["utt_id"])
        assert mapper.abort() == hyp["utt_id"]
        assert mapper.abort() is None
        assert mapper.process(dict(update, sequence_id=999), anchor + 2) == []
    assert len(set(ids)) == 3


def test_nova_late_utterance_end_does_not_finalize_new_speech():
    rows = recording("nova3")
    marker = next(
        r["message"] for r in rows if r["message"].get("type") == "UtteranceEnd"
    )
    mapper = Mapper("nova3", "p", 1, 1000)
    for row in rows:
        message = row["message"]
        out = mapper.process(message, row["received_wall"])
        if out and any("volcano" in w["text"] for w in out[-1]["words"]):
            break
    assert mapper.process(marker, 1008) == []
    assert mapper.utt_id is not None


def test_nova_exact_duplicate_cannot_confirm_but_distinct_same_words_can():
    from minefield.core.deck import Deck
    from minefield.core.game import Game
    from minefield.core.matcher import Matcher

    message = next(
        row["message"]
        for row in recording("nova3")
        if row["message"].get("type") == "Results"
    )
    game = Game(
        Deck([{"word": "pineapple", "variants": []}]),
        replace(Config(), ARM_DELAY_S=0),
    )
    game.start(999)
    game.plant("viewer", "pineapple", 999)
    game.arm(1000)
    matcher = Matcher(game)
    mapper = Mapper("nova3", "process", 1, 1000)
    first = mapper.process(message, 1002)
    assert [event["type"] for event in matcher.process(first[0], 1002)] == ["spark"]

    duplicate = json.loads(json.dumps(message))
    assert mapper.process(duplicate, 1002.1) == []

    distinct = dict(message, duration=message["duration"] + 0.1)
    second = mapper.process(distinct, 1002.2)
    assert second[0]["words"] == first[0]["words"]
    assert second[0]["seq"] == first[0]["seq"] + 1
    assert [event["type"] for event in matcher.process(second[0], 1002.2)] == ["boom"]


@pytest.mark.parametrize("backend,version", [("flux", 2), ("nova3", 1)])
def test_urls_have_required_options_and_no_biasing(backend, version):
    from urllib.parse import parse_qs, urlsplit

    url = urlsplit(connection_url(replace(Config(), STT_BACKEND=backend)))
    assert url.netloc == "api.eu.deepgram.com"
    assert url.path == f"/v{version}/listen"
    query = parse_qs(url.query)
    assert query["mip_opt_out"] == ["true"]
    assert query["sample_rate"] == ["16000"]
    assert query["encoding"] == ["linear16"]
    assert (
        not {"keyterm", "keyterms", "keywords", "prompt", "eager_eot_threshold"}
        & query.keys()
    )


def test_audio_aggregation_overflow_and_interrupt_reanchor():
    audio = AudioInput(Config())
    audio.start(1000)
    audio.activate()
    for _ in range(4):
        audio.feed(bytes(640), 1000)
    wall, pcm = audio.queue.get_nowait()
    assert wall == 1000 and len(pcm) == 2560
    previous = audio.generation
    for _ in range(4 * (Config().STT_AUDIO_QUEUE_SIZE + 1)):
        audio.feed(bytes(640), 1001)
    assert audio.generation > previous
    assert audio.queue.empty()
    audio.activate()
    for _ in range(4):
        audio.feed(bytes(640), 2000)
    assert audio.queue.get_nowait()[0] == 2000
    audio.stop()
    assert not audio.connected and audio.queue.empty()


def test_wav_pcm_chunks_and_invalid_format(tmp_path):
    import wave

    path = tmp_path / "input.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(3200))
    assert [(offset, len(data)) for offset, data in wav_chunks(path)] == [
        (0, 2560),
        (0.08, 640),
    ]
    with wave.open(str(path), "wb") as wav:
        wav.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(3200))
    with pytest.raises(ValueError, match="16 kHz mono"):
        list(wav_chunks(path))


def test_engine_disconnect_explicitly_aborts_and_reanchors(monkeypatch):
    import httpx

    async def scenario():
        posts = []
        engines = []
        row = next(
            r
            for r in recording("flux")
            if r["message"].get("event") == "Update" and r["message"]["words"]
        )

        async def handle(request):
            posts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"ok": True})

        class Socket:
            def __init__(self):
                self.incoming = asyncio.Queue()
                self.sent = False

            async def send(self, raw):
                if isinstance(raw, bytes) and not self.sent:
                    self.sent = True
                    self.incoming.put_nowait(json.dumps(row["message"]))

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.incoming.get()
                if item is None:
                    raise OSError("test dropped connection")
                return item

            async def close(self):
                pass

        class Connect:
            async def __aenter__(self):
                socket = Socket()
                engines.append(socket)
                return socket

            async def __aexit__(self, *args):
                pass

        config = replace(
            Config(),
            STT_RECONNECT_MIN_S=0.001,
            STT_RECONNECT_MAX_S=0.002,
            STT_HEARTBEAT_S=0.01,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            engine = Engine(
                "private-key", config, http, connector=lambda *a, **k: Connect()
            )
            engine.audio.start(1000)
            task = asyncio.create_task(engine.run())

            async def until(predicate):
                async with asyncio.timeout(2):
                    while not predicate():
                        await asyncio.sleep(0.001)

            try:
                await until(lambda: engine.audio.accepting)
                for _ in range(4):
                    engine.audio.feed(bytes(640), 1000)
                await until(
                    lambda: any(path.endswith("hypothesis") for path, _ in posts)
                )
                engines[0].incoming.put_nowait(None)
                await until(lambda: any(path.endswith("abort") for path, _ in posts))
                await until(lambda: len(engines) == 2 and engine.audio.accepting)
                for _ in range(4):
                    engine.audio.feed(bytes(640), 2000)
                await until(
                    lambda: sum(path.endswith("hypothesis") for path, _ in posts) == 2
                )
                hypotheses = [
                    body for path, body in posts if path.endswith("hypothesis")
                ]
                assert not any(hyp["final"] for hyp in hypotheses)
                assert hypotheses[0]["utt_id"] != hypotheses[1]["utt_id"]
                assert hypotheses[1]["utt_start_wall"] >= 2000
                assert engine.status()["reconnects"] == 1
                assert engine.status()["backend"] == "flux"
                assert "lag_ms_p95" in engine.status()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["flux", "nova3"])
def test_malformed_messages_cannot_become_hypotheses(backend):
    row = next(
        r["message"]
        for r in recording(backend)
        if r["message"].get("words") or r["message"].get("type") == "Results"
    )
    malformed = json.loads(json.dumps(row))
    if backend == "flux":
        malformed["turn_index"] = True
    else:
        malformed["is_final"] = "false"
    with pytest.raises(ValueError):
        Mapper(backend, "p", 1, 1000).process(malformed, 1001)


def test_missing_flux_end_of_turn_is_not_confirmation_for_next_turn():
    rows = recording("flux")
    first = next(
        r["message"]
        for r in rows
        if r["message"].get("event") == "Update" and r["message"]["words"]
    )
    following = next(
        r["message"]
        for r in rows
        if r["message"].get("event") == "Update"
        and r["message"].get("turn_index") == 1
        and r["message"]["words"]
    )
    mapper = Mapper("flux", "p", 1, 1000)
    mapper.process(first, 1001)
    with pytest.raises(ValueError):
        mapper.process(following, 1006)


def test_cli_never_reads_real_dotenv_and_reports_missing_key(monkeypatch, capsys):
    from minefield.stt import __main__ as cli

    calls = []
    monkeypatch.setattr(cli, "load_dotenv", lambda path: calls.append(path))
    monkeypatch.setattr(cli.os, "environ", {})
    with pytest.raises(SystemExit) as exc:
        cli.main(["--mic"])
    assert exc.value.code == 1
    assert calls
    assert "configuration" in capsys.readouterr().err


def test_failed_hypothesis_post_aborts_instead_of_retrying():
    import httpx

    from minefield.stt.engine import SourceChanged

    async def scenario():
        posts = []

        def handle(request):
            posts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(
                503 if request.url.path.endswith("hypothesis") else 200
            )

        class Socket:
            def __aiter__(self):
                return self.messages()

            async def messages(self):
                for row in recording("flux"):
                    yield json.dumps(row["message"])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            engine = Engine("not-a-real-key", Config(), http)
            engine.mapper = Mapper("flux", "p", 1, 1000)
            with pytest.raises(SourceChanged):
                await engine._receive(Socket(), engine.audio.generation)
            await engine._abort()
        assert sum(path.endswith("hypothesis") for path, _ in posts) == 1
        assert sum(path.endswith("abort") for path, _ in posts) == 1
        assert not posts[0][1]["final"]

    asyncio.run(scenario())


@pytest.mark.parametrize("abort_response", [200, 503, "timeout"])
def test_failed_final_aborts_its_own_id_once_before_session_cleanup(abort_response):
    import httpx

    from minefield.stt.engine import SourceChanged

    async def scenario():
        posts = []
        messages = [row["message"] for row in recording("flux")]
        interim = next(
            message
            for message in messages
            if message.get("event") == "Update" and message["words"]
        )
        final = next(
            message for message in messages if message.get("event") == "EndOfTurn"
        )

        def handle(request):
            body = json.loads(request.content)
            posts.append((request.url.path, body))
            if request.url.path.endswith("abort"):
                if abort_response == "timeout":
                    raise httpx.ReadTimeout("test timeout", request=request)
                return httpx.Response(abort_response)
            return httpx.Response(503 if body["final"] else 200)

        class Socket:
            def __aiter__(self):
                return self.messages()

            async def messages(self):
                yield json.dumps(interim)
                yield json.dumps(final)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            engine = Engine("not-a-real-key", Config(), http)
            engine.mapper = Mapper("flux", "p", 1, 1000)
            with pytest.raises(SourceChanged):
                await engine._receive(Socket(), engine.audio.generation)
            assert [path for path, _ in posts] == [
                "/stt/hypothesis",
                "/stt/hypothesis",
                "/stt/abort",
            ]
            failed_hypothesis = posts[1][1]
            assert failed_hypothesis["final"]
            assert posts[-1][1] == {"utt_id": failed_hypothesis["utt_id"]}
            await engine._abort()
            assert sum(path.endswith("abort") for path, _ in posts) == 1

    asyncio.run(scenario())


def test_mic_replacement_and_bad_frames_are_isolated():
    import httpx

    async def scenario():
        class Socket:
            request = type("Request", (), {"path": "/audio"})()

            def __init__(self, frames):
                self.frames = frames
                self.closed = []

            async def recv(self):
                return json.dumps(
                    dict(type="hello", sample_rate=16000, client_start_wall=999)
                )

            def __aiter__(self):
                return self.messages()

            async def messages(self):
                for frame in self.frames:
                    yield frame

            async def close(self, code):
                self.closed.append(code)

        async with httpx.AsyncClient() as http:
            engine = Engine("fake", Config(), http)
            old = Socket([])
            engine.mic_socket = old
            bad = Socket(["not PCM"])
            await engine.mic(bad)
            assert old.closed == [1012]
            assert bad.closed == [1008]
            assert engine.mic_socket is None
            assert not engine.audio.connected
            await engine.mic(Socket([bytes(640)]))
            assert not engine.audio.connected

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["flux", "nova3"])
def test_recorded_hypotheses_trigger_real_matcher_without_leaking_other_mines(backend):
    from minefield.core.deck import Deck
    from minefield.core.game import Game
    from minefield.core.matcher import Matcher

    words = ("pineapple", "volcano", "banana")
    game = Game(
        Deck([{"word": word, "variants": []} for word in words]),
        replace(Config(), ARM_DELAY_S=0),
    )
    game.start(999)
    for index, word in enumerate(words):
        game.plant(f"viewer{index}", word, 999)
    game.arm(1000)
    matcher = Matcher(game)
    mapper = Mapper(backend, "p", 1, 1000)
    events = []
    for row in recording(backend):
        for hypothesis in mapper.process(row["message"], row["received_wall"]):
            events.extend(matcher.process(hypothesis, row["received_wall"]))
    booms = [event for event in events if event["type"] == "boom"]
    assert [event["word"] for event in booms] == list(words)
    assert all(event["source"] == "stt" for event in booms)
    assert not any(word in json.dumps(game.snapshot(1020, None)) for word in words)


def test_audio_gap_invalidates_session_without_buffering_backlog():
    import time

    audio = AudioInput(Config())
    audio.start(1000)
    audio.activate()
    audio.feed(bytes(640), 1000)
    generation = audio.generation
    audio.last_activity = time.monotonic() - Config().ENGINE_TIMEOUT_S - 1
    audio.feed(bytes(640), 2000)
    assert audio.generation > generation
    assert not audio.accepting and not audio.buffer and audio.queue.empty()


def test_short_audio_gaps_accumulate_and_abort_before_clock_drifts():
    audio = AudioInput(Config())
    audio.start(1000)
    audio.activate()
    generation = audio.generation
    # Each gap is only 100 ms, so an inactivity-only check misses every one.
    for index in range(240):
        wall = 1000 + index * 0.02 + (index // 4) * 0.1
        audio.feed(bytes(640), wall)
        if audio.generation != generation:
            break
        if not audio.queue.empty():
            audio.queue.get_nowait()
    assert audio.generation > generation
    assert not audio.accepting and not audio.buffer and audio.queue.empty()
    audio.activate()
    for index in range(4):
        audio.feed(bytes(640), 2000 + index * 0.02)
    assert audio.queue.get_nowait()[0] == 2000


def test_continuous_audio_with_bounded_arrival_jitter_keeps_its_clock():
    audio = AudioInput(Config())
    audio.start(1000)
    audio.activate()
    generation = audio.generation
    for index in range(400):
        jitter = 0.1 if index % 8 < 4 else 0
        audio.feed(bytes(640), 1000 + index * 0.02 + jitter)
        if not audio.queue.empty():
            audio.queue.get_nowait()
    assert audio.generation == generation
    assert audio.accepting


@pytest.mark.parametrize("backend", ["flux", "nova3"])
def test_utterance_storage_is_bounded_without_inventing_a_final(backend):
    config = replace(Config(), STT_MAX_UTTERANCE_WORDS=10)
    mapper = Mapper(backend, "p", 1, 1000, config=config)
    if backend == "flux":
        message = next(
            r["message"] for r in recording(backend) if r["message"].get("words")
        )
        message = json.loads(json.dumps(message))
        message["words"] *= 11
        with pytest.raises(ValueError, match="Utterance word limit"):
            mapper.process(message, 1001)
    else:
        message = next(
            r["message"]
            for r in recording(backend)
            if r["message"].get("is_final") and r["message"].get("speech_final")
        )
        message = json.loads(json.dumps(message))
        message["speech_final"] = False
        assert not mapper.process(message, 1003)[0]["final"]
        message["start"] += 3
        for word in message["channel"]["alternatives"][0]["words"]:
            word["start"] += 3
            word["end"] += 3
        with pytest.raises(ValueError, match="Utterance word limit"):
            mapper.process(message, 1006)
        assert mapper.utt_id is not None
        assert len(mapper.committed) <= config.STT_MAX_UTTERANCE_WORDS


def test_engine_instances_have_fresh_process_identifiers():
    assert (
        Engine("fake", Config(), None).engine_id
        != Engine("fake", Config(), None).engine_id
    )


def test_nova_boundary_lag_uses_processed_audio_window_not_last_spoken_word():
    mapper = Mapper("nova3", "p", 1, 1000)
    latest_window_end = 0
    checked = False
    for row in recording("nova3"):
        message = row["message"]
        if message.get("type") == "Results":
            latest_window_end = message["start"] + message["duration"]
        output = mapper.process(message, row["received_wall"])
        if message.get("type") == "UtteranceEnd" and output:
            checked = True
            assert output[0]["lag_ms"] == pytest.approx(
                (row["received_wall"] - 1000 - latest_window_end) * 1000
            )
    assert checked
