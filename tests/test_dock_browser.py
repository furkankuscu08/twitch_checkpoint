"""Dock browser checks: local assets and fake server traffic only."""

import json
from contextlib import contextmanager

import pytest

from scripts.shoot import static_server

pytestmark = pytest.mark.browser

FAKE_SOCKET = """() => {
  window.sockets = [];
  window.WebSocket = class {
    constructor(url) {
      this.url = url; this.readyState = 1; window.sockets.push(this);
      queueMicrotask(() => this.onopen?.({}));
    }
    close() { this.readyState = 3; this.onclose?.({}); }
    emit(data) { this.onmessage?.({data: JSON.stringify(data)}); }
  };
}"""


def snapshot(**changes):
    state = dict(
        type="snapshot", focus=True, queue=[], card=None, pending=[],
        stats=dict(server_now=1000, answer_grace_s=2.5, cancel_window_s=5,
                   summary_s=5, summary=None, summary_until=None),
        status=dict(mic="connected", engine="connected", deepgram="connected",
                    eventsub="connected"),
    )
    state.update(changes)
    return state


def row(message_id="m1", **changes):
    data = dict(id=message_id, user_name="Ana", color="#69e3ca", kind="question",
                text="What editor is that?", earlier=None)
    data.update(changes)
    return data


def card(**changes):
    data = dict(**row(), index=1, total=4, echo_text="", final_text="",
                partial_text="", grace_until=None)
    data.update(changes)
    return data


@contextmanager
def dock(*, demo=None):
    from playwright.sync_api import sync_playwright

    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 400, "height": 900})
        page.set_default_timeout(1500)
        calls = []
        page.add_init_script(FAKE_SOCKET)

        def request(route):
            if route.request.method == "POST":
                calls.append((route.request.url.split("/dock/")[1],
                              route.request.post_data_json))
                route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
            else:
                route.continue_()

        page.route("**/dock/**", request)
        suffix = "" if demo is None else f"?demo=1&step={demo}"
        page.goto(f"{base}/dock/{suffix}")
        page.wait_for_function("window.checkpointReady === true")
        try:
            yield page, calls, base
        finally:
            browser.close()


def emit(page, event):
    page.evaluate("event => window.sockets.at(-1).emit(event)", event)


def test_demo_steps_are_offline_and_fit_400px():
    with dock(demo=0) as (page, calls, base):
        count = page.evaluate("window.checkpointDemoStepCount")
        assert count >= 6
        for step in range(count):
            page.goto(f"{base}/dock/?demo=1&step={step}")
            page.wait_for_function("window.checkpointReady === true")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.evaluate("window.sockets.length") == 0
        assert calls == []


def test_controls_post_exact_routes_and_typed_text():
    from playwright.sync_api import expect

    with dock() as (page, calls, _):
        emit(page, snapshot(queue=[row()]))
        page.get_by_role("button", name="Focus", exact=True).click()
        assert calls.pop() == ("focus", {"on": False})
        page.get_by_role("button", name="Checkpoint", exact=True).click()
        assert calls.pop() == ("checkpoint", {})
        emit(page, dict(type="card_open", **card()))
        expect(page.get_by_role("button", name="Next", exact=True)).to_be_enabled()
        page.get_by_role("button", name="Next", exact=True).click()
        assert calls.pop() == ("next", {})
        page.get_by_role("button", name="Skip", exact=True).click()
        assert calls.pop() == ("skip", {})
        page.get_by_label("Typed answer").fill('Exact words, untouched!')
        page.get_by_role("button", name="Send typed answer").click()
        assert calls.pop() == ("typed", {"text": 'Exact words, untouched!'})
        expect(page.get_by_label("Typed answer")).to_have_value("")
        page.get_by_role("button", name="Clear", exact=True).click()
        assert calls.pop() == ("clear", {})


def test_latest_pending_cancel_by_id_and_failed_requests_recover():
    from playwright.sync_api import expect

    with dock() as (page, calls, _):
        pending = [dict(id=f"r{i}", message_id=f"m{i}", user_name=name,
                        kind="quote", text='🎙 answered on stream: "Exact words"',
                        send_at=1003 + i) for i, name in enumerate(["Ana", "Bob"])]
        emit(page, snapshot(pending=pending, card=card(), queue=[row()]))
        expect(page.locator("#pending-name")).to_have_text("@Bob")
        page.get_by_role("button", name="Cancel quote", exact=True).click()
        assert calls.pop() == ("cancel", {"id": "r1"})
        pending[1]["kind"] = "plain"
        emit(page, snapshot(pending=pending, card=card(), queue=[row()]))
        expect(page.get_by_role("button", name="Cancel quote", exact=True)).to_be_disabled()
        page.route("**/dock/next", lambda route: route.fulfill(status=503, body="failed"))
        page.get_by_role("button", name="Next", exact=True).click()
        expect(page.locator("#notice")).to_contain_text("failed")
        expect(page.get_by_role("button", name="Next", exact=True)).to_be_enabled()
        page.unroute("**/dock/next")
        page.get_by_role("button", name="Next", exact=True).click()
        assert calls.pop() == ("next", {})


def test_snapshot_reconnect_matches_status_and_untrusted_text():
    from playwright.sync_api import expect

    with dock() as (page, calls, _):
        unsafe = '<img src=x onerror="window.injected=true">' + 'x' * 280
        emit(page, snapshot(queue=[row(text=unsafe, user_name='u' * 120)]))
        expect(page.locator("#queue")).to_contain_text(unsafe)
        assert page.locator("#queue img").count() == 0
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        matching = row(earlier=dict(quote="Exact previous words", ago_min=12))
        emit(page, snapshot(queue=[matching]))
        page.get_by_role("button", name="Send answer from 12 min ago").click()
        assert calls.pop() == ("earlier", {"id": "m1"})
        emit(page, dict(type="removed", id="m1"))
        expect(page.locator("#queue-list")).to_be_empty()
        emit(page, dict(type="status", mic="disconnected", engine="connected",
                        deepgram="disconnected", eventsub="disconnected"))
        expect(page.locator('[data-status="mic"]')).to_have_attribute("data-state", "disconnected")
        page.evaluate("window.sockets.at(-1).close()")
        expect(page.locator("#connection")).to_contain_text("Reconnecting")
        page.wait_for_function("window.sockets.length >= 2")
        emit(page, snapshot(focus=False, queue=[row("restored")], card=card(id="restored")))
        expect(page.locator("#connection")).to_have_text("Connected")
        expect(page.locator("#card-question")).to_have_text("What editor is that?")
        expect(page.get_by_role("button", name="Focus", exact=True)).to_have_attribute("aria-pressed", "false")


def test_live_events_update_answer_pending_summary_and_delete():
    from playwright.sync_api import expect

    with dock() as (page, _, _):
        emit(page, snapshot())
        emit(page, dict(type="queued", **row()))
        emit(page, dict(type="card_open", **card()))
        emit(page, dict(type="answer", id="m1", echo_text="Read question",
                        final_text="Exact finished words", partial_text="still speaking",
                        grace_until=1002.5))
        expect(page.locator("#echo")).to_have_text("Read question")
        expect(page.locator("#answer-final")).to_have_text("Exact finished words")
        expect(page.locator("#answer-partial")).to_have_text("still speaking")
        emit(page, dict(type="card_close", id="m1", result="removed"))
        expect(page.locator("#card-question")).to_have_text("")
        emit(page, dict(type="reply_pending", id="r1", message_id="m1", user_name="Ana",
                        kind="quote", text="Exact reply", send_at=1005))
        expect(page.locator("#pending-text")).to_have_text("Exact reply")
        emit(page, dict(type="reply_sent", id="r1", kind="quote", is_sent=False, user_name="Ana"))
        expect(page.locator("#pending")).to_be_hidden()
        expect(page.locator("#notice")).to_contain_text("not sent")
        emit(page, dict(type="summary", answered=2, earlier=1, skipped=1))
        expect(page.locator("#summary")).to_contain_text("2 answered · 1 from earlier · 1 skipped")
