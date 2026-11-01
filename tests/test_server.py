import logging

import pytest
from fastapi.testclient import TestClient

from checkpoint.server.app import create_app


def hypothesis(**changes):
    return dict(
        utt_id="engine-s1-t0", seq=0, final=False,
        words=[dict(text="Untouched,", start=0.0, end=0.4, prob=0.95)],
        utt_start_wall=1000.0, emitted_wall=1001.0, lag_ms=100.0, **changes,
    )


def test_intake_accepts_engine_payloads_and_logs_only_at_debug(caplog):
    with TestClient(create_app()) as client:
        with caplog.at_level(logging.INFO):
            assert client.post("/stt/hypothesis", json=hypothesis()).json() == {"ok": True}
        assert "Untouched," not in caplog.text
        with caplog.at_level(logging.DEBUG, logger="checkpoint.server.app"):
            assert client.post("/stt/hypothesis", json=hypothesis()).is_success
            assert client.post("/stt/status", json=dict(
                ok=True, backend="flux", deepgram="connected", mic="connected",
                lag_ms_p95=123.0, reconnects=0,
            )).is_success
            assert client.post("/stt/abort", json={"utt_id": "engine-s1-t0"}).is_success
        records = [r for r in caplog.records if r.name == "checkpoint.server.app"]
        assert "Untouched," in caplog.text
        assert all(r.levelno == logging.DEBUG for r in records)


@pytest.mark.parametrize("changes", [
    {"seq": -1}, {"seq": True}, {"final": "false"}, {"utt_id": ""},
    {"words": [{"text": "private", "start": 1, "end": 0, "prob": 0.8}]},
    {"words": [{"text": "private", "start": 0, "end": 1, "prob": 1.1}]},
    {"words": [{"text": "", "start": 0, "end": 1, "prob": 0.8}]},
    {"utt_start_wall": "nan"},
])
def test_malformed_hypothesis_rejected_without_stopping_intake(changes):
    body = hypothesis()
    body.update(changes)
    with TestClient(create_app()) as client:
        assert client.post("/stt/hypothesis", json=body).status_code == 422
        assert client.post("/stt/hypothesis", json=hypothesis()).is_success


@pytest.mark.parametrize("path", ["hypothesis", "status", "abort"])
def test_malformed_json_and_missing_fields_are_rejected(path):
    with TestClient(create_app()) as client:
        assert client.post(f"/stt/{path}", content="not json").status_code == 422
        assert client.post(f"/stt/{path}", json={}).status_code == 422
