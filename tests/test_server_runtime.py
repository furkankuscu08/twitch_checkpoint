import json
import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from checkpoint.core.config import Config
from checkpoint.server.app import create_app


def make_app(now, sent, **kwargs):
    async def sender(intent):
        sent.append(intent)
        return {"is_sent": True}

    return create_app(
        clock=lambda: now[0],
        sender=sender,
        debug=True,
        blocklist=("blockedword",),
        **kwargs,
    )


def receive(ws, kind):
    for _ in range(40):
        event = ws.receive_json()
        if event["type"] == kind:
            return event
    pytest.fail(f"No {kind} event")


def advance(client, app, now, value):
    now[0] = value
    client.portal.call(app.state.runtime.tick)

    async def idle():
        await app.state.runtime.delivery.join()

    client.portal.call(idle)


def inject(client, user="viewer", text="Which editor do you use?"):
    response = client.post("/dock/debug/chat", json={"user": user, "text": text})
    assert response.status_code == 200
    return response.json()["id"]


def test_debug_three_messages_hypotheses_cancel_and_threaded_deadlines(speech):
    now, sent = [1000.0], []
    app = make_app(now, sent)
    with TestClient(app) as client, client.websocket_connect("/ws/dock") as dock:
        assert dock.receive_json()["type"] == "snapshot"
        client.post("/dock/focus", json={"on": True})
        ids = [inject(client, f"viewer{i}") for i in range(3)]
        client.post("/dock/checkpoint")
        assert receive(dock, "card_open")["id"] == ids[0]
        body = speech("Use the editor you enjoy.", final=True, wall=1000.0)
        assert client.post("/stt/hypothesis", json=body).is_success
        assert receive(dock, "answer")["final_text"] == "Use the editor you enjoy."
        advance(client, app, now, 1002.5)
        assert receive(dock, "card_close")["result"] == "answered"
        first = receive(dock, "reply_pending")
        assert first["message_id"] == ids[0] and not sent
        client.post("/dock/typed", json={"text": "Second exact answer."})
        second = receive(dock, "reply_pending")
        assert client.post("/dock/cancel", json={"id": first["id"]}).is_success
        advance(client, app, now, 1007.5)
        assert [i.reply.reply_parent_message_id for i in sent] == ids[:2]
        assert sent[0].reply.kind == "plain" and sent[1].reply.kind == "quote"
        assert first["id"] != second["id"]
        advance(client, app, now, 1010)
        assert len(sent) == 2


def test_overlay_snapshot_redacts_queue_and_pending_and_reconnect_restores(speech):
    now, sent = [1000.0], []
    app = make_app(now, sent)
    with TestClient(app) as client:
        client.post("/dock/focus", json={"on": True})
        first = inject(client, text="Private first question?")
        inject(client, "other", "Private waiting question?")
        with client.websocket_connect("/ws/overlay") as ws:
            snap = ws.receive_json()
            assert "Private" not in json.dumps(snap) and snap["pending"] == []
        client.post("/dock/checkpoint")
        client.post("/dock/typed", json={"text": "Private exact reply."})
        with client.websocket_connect("/ws/overlay") as ws:
            snap = ws.receive_json()
            assert snap["card"]["text"] == "Private waiting question?"
            assert "Private exact reply." not in json.dumps(snap)
            assert "Private first question?" not in json.dumps(snap)
        with client.websocket_connect("/ws/dock") as ws:
            snap = ws.receive_json()
            assert snap["pending"][0]["message_id"] == first
            assert "Private exact reply." in snap["pending"][0]["text"]


def test_delete_queued_and_open_silent_no_reply():
    now, sent = [1000.0], []
    app = make_app(now, sent)
    with TestClient(app) as client:
        client.post("/dock/focus", json={"on": True})
        ids = [inject(client, f"v{i}") for i in range(3)]
        client.post("/dock/checkpoint")
        client.portal.call(app.state.runtime.delete, {"message_id": ids[2]})
        client.portal.call(app.state.runtime.delete, {"message_id": ids[0]})
        assert app.state.runtime.core.card.message.message_id == ids[1]
        advance(client, app, now, 1020)
        assert not sent and not app.state.runtime.core.pending


def test_intake_dedup_abort_and_next_boundary(speech):
    now, sent = [1000.0], []
    app = make_app(now, sent)
    with TestClient(app) as client:
        client.post("/dock/focus", json={"on": True})
        inject(client)
        inject(client, "second")
        client.post("/dock/checkpoint")
        body = speech("old new", seq=2, wall=1000, starts=[0.1, 1.1])
        client.post("/stt/hypothesis", json=body)
        client.post("/stt/hypothesis", json={**body, "seq": 1, "words": []})
        assert app.state.runtime.core.card.partial_text == "old new"
        now[0] = 1001
        client.post("/dock/next")
        client.post("/stt/hypothesis", json=body)
        assert app.state.runtime.core.card.partial_text == ""
        client.post("/stt/hypothesis", json={**body, "seq": 3})
        assert app.state.runtime.core.card.partial_text == "new"
        client.post("/stt/abort", json={"utt_id": body["utt_id"]})
        client.post("/stt/hypothesis", json={**body, "seq": 4, "final": True})
        assert app.state.runtime.core.card.answer == ""


def test_debug_production_delivery_never_calls_twitch_and_status_expires(caplog):
    now = [1000.0]
    app = create_app(clock=lambda: now[0], debug=True)
    with caplog.at_level(logging.INFO), TestClient(app) as client:
        client.post("/dock/focus", json={"on": True})
        inject(client)
        client.post("/dock/checkpoint")
        client.post("/dock/typed", json={"text": "Secret exact answer."})
        client.post(
            "/stt/status",
            json=dict(
                ok=True,
                backend="flux",
                mic="connected",
                deepgram="connected",
                lag_ms_p95=0.0,
                reconnects=0,
            ),
        )
        assert app.state.runtime.status["engine"] == "connected"
        advance(client, app, now, 1006)
        assert app.state.runtime.status["engine"] == "disconnected"
        assert "Secret exact answer." not in caplog.text
        assert "is_sent=False" in caplog.text


@pytest.mark.parametrize(
    "path,body",
    [
        ("focus", {"on": "yes"}),
        ("cancel", {}),
        ("typed", {"text": ""}),
        ("earlier", {"id": 1}),
        ("debug/chat", {"user": "", "text": "x"}),
    ],
)
def test_dock_bad_input_rejected(path, body):
    with TestClient(create_app(debug=True)) as client:
        assert client.post("/dock/" + path, json=body).status_code == 422


def test_debug_off_is_404():
    with TestClient(create_app()) as client:
        assert (
            client.post(
                "/dock/debug/chat", json={"user": "x", "text": "hello"}
            ).status_code
            == 404
        )


def test_earlier_only_success_counts_and_updates_chips():
    now, sent = [1000.0], []
    outcomes = iter([True, False, True])

    async def sender(intent):
        sent.append(intent)
        return {"is_sent": next(outcomes)}

    app = create_app(clock=lambda: now[0], debug=True, sender=sender)
    with TestClient(app) as client:
        client.post("/dock/focus", json={"on": True})
        inject(client)
        client.post("/dock/checkpoint")
        client.post("/dock/typed", json={"text": "I use my favourite editor."})
        advance(client, app, now, 1005)
        fail_id = inject(client, "v2", "What editor is that?")
        with client.websocket_connect("/ws/dock") as ws:
            assert (
                ws.receive_json()["queue"][0]["earlier"]["quote"]
                == "I use my favourite editor."
            )
        client.post("/dock/earlier", json={"id": fail_id})
        advance(client, app, now, 1006)
        ok_id = inject(client, "v3", "What editor is that?")
        client.post("/dock/earlier", json={"id": ok_id})
        advance(client, app, now, 1007)
        inject(client, "v4", "Another question?")
        client.post("/dock/checkpoint")
        client.post("/dock/skip")
        assert app.state.runtime.core.summary["earlier"] == 1
        assert client.post("/dock/earlier", json={"id": ok_id}).status_code == 409


def test_send_failure_does_not_stop_next_delivery(caplog):
    now, sent = [1000.0], []

    async def sender(intent):
        sent.append(intent)
        if len(sent) == 1:
            raise OSError("private transcript and credential")
        return {"is_sent": True}

    app = create_app(clock=lambda: now[0], debug=True, sender=sender)
    with caplog.at_level(logging.INFO), TestClient(app) as client:
        client.post("/dock/focus", json={"on": True})
        inject(client)
        inject(client, "v2")
        client.post("/dock/checkpoint")
        client.post("/dock/typed", json={"text": "First answer."})
        client.post("/dock/typed", json={"text": "Second answer."})
        advance(client, app, now, 1005)
        assert len(sent) == 2
        assert "private transcript" not in caplog.text


def test_delete_after_answer_still_notifies_overlay_to_purge_exit():
    now, sent = [1000.0], []
    app = make_app(now, sent)
    with TestClient(app) as client:
        client.post("/dock/focus", json={"on": True})
        message_id = inject(client)
        client.post("/dock/checkpoint")
        client.post("/dock/typed", json={"text": "Answer on the closing card."})
        events = []
        app.state.runtime.hub.broadcast = lambda event, audience: events.append(
            (event, audience)
        )
        client.portal.call(app.state.runtime.delete, {"message_id": message_id})
        assert ({"type": "removed", "id": message_id}, "overlay") in events


def test_binary_browser_input_is_ignored_without_losing_subscription():
    with TestClient(create_app()) as client, client.websocket_connect("/ws/dock") as ws:
        ws.receive_json()
        ws.send_bytes(b"not a browser command")
        client.post("/dock/focus", json={"on": True})
        assert receive(ws, "snapshot")["focus"] is True


def test_server_tick_runs_timers_without_an_http_trigger():
    import time

    app = create_app(
        debug=True, config=replace(Config(), CANCEL_WINDOW_S=0.03, SERVER_TICK_S=0.005)
    )
    with TestClient(app) as client:
        client.post("/dock/focus", json={"on": True})
        inject(client)
        client.post("/dock/checkpoint")
        client.post("/dock/typed", json={"text": "Exact timer answer."})
        deadline = time.monotonic() + 1
        while app.state.runtime.core.pending and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not app.state.runtime.core.pending


def test_entrypoint_loads_blocklist_and_config_outside_core(monkeypatch):
    from checkpoint.server import __main__ as entrypoint

    captured = []
    monkeypatch.setattr(entrypoint, "load_dotenv", lambda *args: None)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "")
    monkeypatch.setenv("TWITCH_BOT_LOGIN", "checkpoint_bott")
    monkeypatch.setenv("TWITCH_BROADCASTER_LOGIN", "izleyici0808")
    monkeypatch.setenv("CHECKPOINT_DEBUG", "1")
    monkeypatch.setattr(
        entrypoint.uvicorn, "run", lambda app, **kw: captured.append(app)
    )
    entrypoint.main([])
    runtime = captured[0].state.runtime
    assert "shit" in runtime.core.blocklist
    assert runtime.core.queue.excluded == {"checkpoint_bott", "izleyici0808"}
