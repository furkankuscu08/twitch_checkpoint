"""WP2 offline acceptance, using only messages captured from Windows TTS."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from checkpoint.core.config import Config
from checkpoint.stt.audio import AudioInput, wav_chunks
from checkpoint.stt.backends import Mapper, connection_url
from checkpoint.stt.engine import Engine

FIXTURES = Path(__file__).parent / "fixtures/deepgram"


def recording(backend):
    return [
        json.loads(line)
        for line in (FIXTURES / f"{backend}.jsonl").read_text().splitlines()
    ]


def test_recorded_tts_maps_three_complete_utterances():
    mapper = Mapper("process", 1, 1000)
    hypotheses = [
        hyp
        for row in recording("flux")
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
    mapper = Mapper("p", 1, 1000)
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
        mapper = Mapper(process, session, anchor)
        hyp = mapper.process(update, anchor + 1)[0]
        ids.append(hyp["utt_id"])
        assert mapper.abort() == hyp["utt_id"]
        assert mapper.abort() is None
        assert mapper.process(dict(update, sequence_id=999), anchor + 2) == []
    assert len(set(ids)) == 3


def test_urls_have_required_options_and_no_biasing():
    from urllib.parse import parse_qs, urlsplit

    url = urlsplit(connection_url(Config()))
    assert url.netloc == "api.eu.deepgram.com"
    assert url.path == "/v2/listen"
    query = parse_qs(url.query)
    assert query["model"] == ["flux-general-en"]
    assert query["mip_opt_out"] == ["true"]
    assert query["sample_rate"] == ["16000"]
    assert query["encoding"] == ["linear16"]
    assert (
        not {"keyterm", "keyterms", "keywords", "prompt", "eager_eot_threshold"}
        & query.keys()
    )


@pytest.mark.parametrize("status", [401, 403])
def test_provider_auth_failure_keeps_engine_heartbeat_and_dock_alive(status):
    import httpx
    import websockets
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    from checkpoint.server.app import create_app

    async def scenario():
        attempts = []

        class Rejected:
            async def __aenter__(self):
                attempts.append(status)
                raise websockets.InvalidStatus(Response(status, "Rejected", Headers()))

            async def __aexit__(self, *_):
                pass

        config = replace(
            Config(),
            STT_RECONNECT_MIN_S=0.01,
            STT_RECONNECT_MAX_S=0.02,
            STT_HEARTBEAT_S=0.01,
        )
        app = create_app(debug=True, config=config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as http:
                engine = Engine(
                    "fake", config, http, connector=lambda *a, **k: Rejected()
                )
                engine.audio.start(1000)
                running = asyncio.create_task(engine.run())
                try:
                    await asyncio.sleep(0.05)
                    assert not running.done(), (
                        "Provider auth failure stopped the engine"
                    )
                    assert len(attempts) >= 2
                    assert app.state.runtime.status["engine"] == "connected"
                    assert app.state.runtime.status["deepgram"] == "disconnected"
                    base = "http://127.0.0.1"
                    assert (
                        await http.post(base + "/dock/focus", json={"on": True})
                    ).is_success
                    assert (
                        await http.post(
                            base + "/dock/debug/chat",
                            json={"user": "river", "text": "How do you keep focused?"},
                        )
                    ).json()["queued"]
                    assert (await http.post(base + "/dock/checkpoint")).is_success
                    assert (
                        await http.post(
                            base + "/dock/typed", json={"text": "I keep one task open."}
                        )
                    ).is_success
                    assert len(app.state.runtime.core.pending) == 1
                finally:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


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


def test_malformed_messages_cannot_become_hypotheses():
    row = next(
        r["message"]
        for r in recording("flux")
        if r["message"].get("words") or r["message"].get("type") == "Results"
    )
    malformed = json.loads(json.dumps(row))
    malformed["turn_index"] = True
    with pytest.raises(ValueError):
        Mapper("p", 1, 1000).process(malformed, 1001)


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
    mapper = Mapper("p", 1, 1000)
    mapper.process(first, 1001)
    with pytest.raises(ValueError):
        mapper.process(following, 1006)


def test_cli_never_reads_real_dotenv_and_reports_missing_key(monkeypatch, capsys):
    from checkpoint.stt import __main__ as cli

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

    from checkpoint.stt.engine import SourceChanged

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
            engine.mapper = Mapper("p", 1, 1000)
            with pytest.raises(SourceChanged):
                await engine._receive(Socket())
            await engine._abort()
        assert sum(path.endswith("hypothesis") for path, _ in posts) == 1
        assert sum(path.endswith("abort") for path, _ in posts) == 1
        assert not posts[0][1]["final"]

    asyncio.run(scenario())


@pytest.mark.parametrize("abort_response", [200, 503, "timeout"])
def test_failed_final_aborts_its_own_id_once_before_session_cleanup(abort_response):
    import httpx

    from checkpoint.stt.engine import SourceChanged

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
            engine.mapper = Mapper("p", 1, 1000)
            with pytest.raises(SourceChanged):
                await engine._receive(Socket())
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


@pytest.mark.parametrize("source_change", ["stop", "replacement"])
@pytest.mark.parametrize("frame_count", [1, 5, 17])
def test_healthy_mic_end_drains_queued_pcm_and_subchunk_tail(
    source_change, frame_count
):
    import httpx

    async def scenario():
        providers, posts = [], []
        old_frames_sent = asyncio.Event()
        final = next(
            r["message"]
            for r in recording("flux")
            if r["message"].get("event") == "EndOfTurn"
        )

        class Provider:
            def __init__(self):
                self.incoming = asyncio.Queue()
                self.sent = []

            async def send(self, raw):
                self.sent.append(
                    raw if isinstance(raw, bytes) else json.loads(raw)["type"]
                )
                if isinstance(raw, str) and json.loads(raw)["type"] == "ForceEndTurn":
                    self.incoming.put_nowait(json.dumps(final))

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.incoming.get()

        class Connection:
            async def __aenter__(self):
                provider = Provider()
                providers.append(provider)
                return provider

            async def __aexit__(self, *_):
                pass

        class Mic:
            request = type("Request", (), {"path": "/audio"})()

            def __init__(self, frames, wait_for_replacement=False):
                self.frames, self.wait_for_replacement = frames, wait_for_replacement
                self.closed = asyncio.Event()

            async def recv(self):
                return json.dumps(
                    dict(type="hello", sample_rate=16000, client_start_wall=1)
                )

            def __aiter__(self):
                return self.messages()

            async def messages(self):
                await engine.audio.ready.wait()
                for frame in self.frames:
                    yield frame
                old_frames_sent.set()
                if self.wait_for_replacement:
                    await self.closed.wait()

            async def close(self, code):
                self.closed.set()

        def handle(request):
            posts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200)

        config = replace(
            Config(),
            STT_RECONNECT_MIN_S=0.001,
            STT_RECONNECT_MAX_S=0.002,
            STT_HTTP_TIMEOUT_S=0.05,
        )
        frames = [bytes([i + 1]) * 640 for i in range(frame_count)]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            engine = Engine(
                "fake", config, http, connector=lambda *a, **k: Connection()
            )
            running = asyncio.create_task(engine.run())
            mic = asyncio.create_task(
                engine.mic(Mic(frames, source_change == "replacement"))
            )
            try:
                await asyncio.wait_for(old_frames_sent.wait(), 1)
                if source_change == "replacement":
                    await asyncio.wait_for(engine.mic(Mic([bytes([99]) * 640])), 1)
                await asyncio.wait_for(mic, 1)
                captured = b"".join(
                    raw for raw in providers[0].sent if isinstance(raw, bytes)
                )
                assert captured == b"".join(frames), (
                    "Captured tail disappeared on mic end"
                )
                assert providers[0].sent[-2:] == ["ForceEndTurn", "CloseStream"]
                assert all(isinstance(raw, bytes) for raw in providers[0].sent[:-2])
                finals = [
                    body
                    for path, body in posts
                    if path.endswith("hypothesis") and body["final"]
                ]
                assert len(finals) == (2 if source_change == "replacement" else 1)
                assert len({h["utt_id"] for h in finals}) == len(finals)
                assert not any(path.endswith("abort") for path, _ in posts)
            finally:
                mic.cancel()
                running.cancel()
                await asyncio.gather(mic, running, return_exceptions=True)

    asyncio.run(scenario())


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


def test_stalled_pcm_drain_is_bounded_and_discards_broken_continuity():
    import httpx

    async def scenario():
        controls = []

        class Provider:
            async def send(self, raw):
                if isinstance(raw, bytes):
                    await asyncio.Future()  # Provider stopped accepting captured PCM.
                else:
                    controls.append(json.loads(raw)["type"])

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

        class Connection:
            async def __aenter__(self):
                return Provider()

            async def __aexit__(self, *_):
                pass

        config = replace(Config(), ENGINE_TIMEOUT_S=0.05, STT_HTTP_TIMEOUT_S=0.01)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ) as http:
            engine = Engine(
                "fake", config, http, connector=lambda *a, **k: Connection()
            )
            engine.mic_socket = object()
            engine.audio.start(1000)
            running = asyncio.create_task(engine.run())
            try:
                await asyncio.wait_for(engine.audio.ready.wait(), 0.2)
                engine.audio.feed(bytes(640), 1000)
                engine.mic_socket = None
                assert await asyncio.wait_for(engine._stop_mic(), 0.2) is False
                async with asyncio.timeout(0.2):
                    while engine.connected:
                        await asyncio.sleep(0)
                assert controls == ["CloseStream"]
                assert not running.done()
                assert not engine.audio.buffer and engine.audio.queue.empty()
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


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


def test_utterance_storage_is_bounded_without_inventing_a_final():
    config = replace(Config(), STT_MAX_UTTERANCE_WORDS=10)
    mapper = Mapper("p", 1, 1000, config=config)
    message = next(r["message"] for r in recording("flux") if r["message"].get("words"))
    message = json.loads(json.dumps(message))
    message["words"] *= 11
    with pytest.raises(ValueError, match="Utterance word limit"):
        mapper.process(message, 1001)
    assert mapper.utt_id is None


def test_engine_instances_have_fresh_process_identifiers():
    assert (
        Engine("fake", Config(), None).engine_id
        != Engine("fake", Config(), None).engine_id
    )


@pytest.mark.parametrize("provider_reply", ["final", "no-active-turn"])
def test_mic_drain_after_previous_final_waits_for_delayed_provider_turn(provider_reply):
    import httpx

    async def scenario():
        posts, controls = [], []
        finals = [
            r["message"]
            for r in recording("flux")
            if r["message"].get("event") == "EndOfTurn"
        ]

        class Socket:
            def __init__(self):
                self.incoming = asyncio.Queue()

            async def send(self, raw):
                if isinstance(raw, str):
                    controls.append(json.loads(raw)["type"])
                    response = (
                        finals[1]
                        if provider_reply == "final"
                        else {
                            "type": "Warning",
                            "code": "FORCE_END_TURN_NO_ACTIVE_TURN",
                        }
                    )
                    self.incoming.put_nowait(json.dumps(response))

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.incoming.get()

        def handle(request):
            posts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            engine = Engine("fake", replace(Config(), STT_HTTP_TIMEOUT_S=0.5), http)
            engine.mapper = Mapper("p", 1, 1000)
            engine.mapper.process(finals[0], 1005)
            assert engine.mapper.utt_id is None
            engine.turn_finished.set()  # The previous turn was already delivered.
            # New audio was sent, but no Update for it has reached the mapper.
            socket = Socket()
            await socket.send(bytes(2560))
            reader = asyncio.create_task(engine._receive(socket))
            try:
                await asyncio.wait_for(engine._finish_turn(socket), 0.2)
                assert controls == ["ForceEndTurn"]
                hypotheses = [
                    body for path, body in posts if path.endswith("hypothesis")
                ]
                if provider_reply == "final":
                    assert len(hypotheses) == 1 and hypotheses[0]["final"]
                    assert (
                        "volcano"
                        in " ".join(w["text"] for w in hypotheses[0]["words"]).lower()
                    )
                else:
                    assert not hypotheses
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("source_change", ["stop", "replacement"])
@pytest.mark.parametrize("final_response", ["final", "timeout", "delivery-failure"])
def test_mic_boundary_requests_real_final_before_close(source_change, final_response):
    import httpx

    async def scenario():
        posts, controls = [], []
        messages = [r["message"] for r in recording("flux")]
        update = next(m for m in messages if m.get("event") == "Update" and m["words"])
        final = next(m for m in messages if m.get("event") == "EndOfTurn")
        partial_received = asyncio.Event()

        def handle(request):
            body = json.loads(request.content)
            posts.append((request.url.path, body))
            if request.url.path.endswith("hypothesis"):
                partial_received.set()
                if body["final"] and final_response == "delivery-failure":
                    return httpx.Response(503)
            return httpx.Response(200)

        class Socket:
            def __init__(self):
                self.incoming = asyncio.Queue()

            async def send(self, raw):
                if isinstance(raw, bytes):
                    self.incoming.put_nowait(json.dumps(update))
                else:
                    kind = json.loads(raw)["type"]
                    controls.append(kind)
                    if kind == "ForceEndTurn" and final_response != "timeout":
                        self.incoming.put_nowait(json.dumps(final))

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.incoming.get()

        socket = Socket()

        class Connection:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, *_):
                pass

        config = replace(Config(), STT_HTTP_TIMEOUT_S=0.05)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            engine = Engine(
                "fake", config, http, connector=lambda *a, **k: Connection()
            )
            engine.mic_socket = object()
            engine.audio.start(1000)
            session = asyncio.create_task(engine._session())
            try:
                async with asyncio.timeout(1):
                    while not engine.audio.accepting:
                        await asyncio.sleep(0)
                engine.audio.feed(bytes(2560), 1000)
                await asyncio.wait_for(partial_received.wait(), 1)
                if source_change == "stop":
                    engine.mic_socket = None
                else:
                    engine.mic_socket = object()
                await engine.audio.finish_mic()
                await asyncio.wait_for(session, 1)
                if source_change == "replacement":
                    engine.audio.start(2000)
                await engine._abort()
            finally:
                session.cancel()
                await asyncio.gather(session, return_exceptions=True)
            assert controls == ["ForceEndTurn", "CloseStream"]
            hypotheses = [body for path, body in posts if path.endswith("hypothesis")]
            aborts = [body for path, body in posts if path.endswith("abort")]
            if final_response == "final":
                assert hypotheses[-1]["final"]
                assert hypotheses[-1]["utt_id"] == hypotheses[0]["utt_id"]
                assert hypotheses[-1]["utt_start_wall"] == 1000
                assert [w["text"] for w in hypotheses[-1]["words"]] == [
                    w["word"] for w in final["words"]
                ]
                assert not aborts
            else:
                assert aborts == [{"utt_id": hypotheses[0]["utt_id"]}]
                if final_response == "timeout":
                    assert len(hypotheses) == 1 and not hypotheses[0]["final"]

    asyncio.run(scenario())


def test_closed_mic_transport_is_not_reported_as_bad_pcm(caplog):
    import httpx
    import websockets

    class Socket:
        request = type("Request", (), {"path": "/audio"})()
        close_code = 1006

        async def recv(self):
            return json.dumps(
                dict(type="hello", sample_rate=16000, client_start_wall=1)
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise websockets.ConnectionClosedError(None, None)

        async def close(self, code):
            pytest.fail("A closed transport must not be rejected as malformed PCM")

    async def scenario():
        async with httpx.AsyncClient() as http:
            await Engine("fake", Config(), http).mic(Socket())

    asyncio.run(scenario())
    assert "protocol rejected" not in caplog.text
