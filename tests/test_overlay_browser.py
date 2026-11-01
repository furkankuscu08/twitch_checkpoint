"""Overlay acceptance with local assets and a fake WebSocket; no providers."""

import pytest

from scripts.shoot import static_server

pytestmark = pytest.mark.browser


@pytest.fixture
def overlay():
    from playwright.sync_api import sync_playwright

    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.add_init_script("""
            window.__sockets = [];
            window.WebSocket = class {
                constructor(url) {
                    this.url = url; window.__sockets.push(this);
                    queueMicrotask(() => this.onopen?.({}));
                }
                close() { this.onclose?.({}); }
            };
            window.__emit = event => window.__sockets.at(-1).onmessage({
                data: JSON.stringify(event)
            });
        """)
        page.clock.install()
        response = page.goto(f"{base}/overlay/")
        assert response.status == 200
        page.wait_for_function("window.checkpointReady === true")
        yield page, base
        browser.close()


def emit(page, event):
    page.evaluate("event => window.__emit(event)", event)


def snapshot(**changes):
    return (
        dict(
            type="snapshot",
            focus=True,
            queue=[],
            card=None,
            pending=[],
            stats=dict(
                server_now=1000,
                answer_grace_s=2.5,
                cancel_window_s=5,
                summary_s=5,
                summary=None,
                summary_until=None,
            ),
        )
        | changes
    )


def card(message_id="m1", **changes):
    return (
        dict(
            type="card_open",
            id=message_id,
            user_name="Ana",
            color="#69e3ca",
            kind="question",
            text="What editor and theme do you use?",
            index=1,
            total=4,
        )
        | changes
    )


def answer(message_id="m1", **changes):
    return (
        dict(
            type="answer",
            id=message_id,
            echo_text="",
            final_text="",
            partial_text="",
            grace_until=None,
        )
        | changes
    )


def test_text_only_appears_in_open_card_and_is_never_html(overlay):
    from playwright.sync_api import expect

    page, _ = overlay
    emit(page, snapshot())
    emit(
        page,
        dict(
            type="queued",
            id="m1",
            user_name="Ana",
            color="#69e3ca",
            kind="question",
            text="secret queued text",
        ),
    )
    assert "secret queued text" not in page.locator("body").text_content()
    emit(page, card())
    expect(page.locator("#question")).to_have_text("What editor and theme do you use?")
    emit(page, answer(partial_text="<img src=x onerror=alert(1)> exact words"))
    expect(page.locator("#partial-text")).to_have_text(
        "<img src=x onerror=alert(1)> exact words"
    )
    assert page.locator("img").count() == 0
    assert "exact words" not in page.locator("#badge").text_content()
    emit(page, dict(type="reply_pending", id="r1", text="private reply"))
    assert "private reply" not in page.locator("body").text_content()
    emit(page, dict(type="card_close", id="m1", result="skipped"))
    assert "What editor" not in page.locator("body").text_content()
    assert "exact words" not in page.locator("body").text_content()


def test_answered_stamp_keeps_text_then_purges_it_in_under_one_second(overlay):
    from playwright.sync_api import expect

    page, _ = overlay
    emit(page, snapshot())
    emit(page, card())
    emit(page, answer(final_text="It's my own editor theme."))
    emit(page, dict(type="card_close", id="m1", result="answered"))
    page.clock.run_for(180)
    expect(page.locator("#stamp")).to_be_visible()
    assert "What editor" in page.locator("body").text_content()
    assert "It's my own editor theme." in page.locator("body").text_content()
    page.clock.run_for(600)
    assert "What editor" not in page.locator("body").text_content()
    assert "It's my own editor theme." not in page.locator("body").text_content()


def test_moderation_purges_open_and_answered_exiting_text_immediately(overlay):
    page, _ = overlay
    for exiting in (False, True):
        emit(page, snapshot())
        emit(page, card())
        emit(page, answer(partial_text="Private spoken words"))
        if exiting:
            emit(page, dict(type="card_close", id="m1", result="answered"))
        emit(page, dict(type="removed", id="m1"))
        assert "What editor" not in page.locator("body").text_content()
        assert "Private spoken words" not in page.locator("body").text_content()
        page.clock.run_for(900)
        assert "Private spoken words" not in page.locator("body").text_content()


def test_echo_partial_grace_summary_and_success_feedback(overlay):
    from playwright.sync_api import expect

    page, _ = overlay
    emit(page, snapshot())
    emit(page, card())
    emit(page, answer(echo_text="What editor and theme do you use?"))
    expect(page.locator("#echo-text")).to_have_text("What editor and theme do you use?")
    assert page.locator("#grace").is_hidden()
    emit(
        page,
        answer(
            echo_text="What editor and theme do you use?",
            final_text="I use a custom theme.",
            partial_text="The font is",
            grace_until=1002.5,
        ),
    )
    expect(page.locator("#grace")).to_be_visible()
    before = float(page.locator("#grace").get_attribute("data-remaining"))
    page.clock.run_for(1000)
    after = float(page.locator("#grace").get_attribute("data-remaining"))
    assert after < before
    emit(
        page,
        dict(type="reply_sent", id="r1", kind="quote", is_sent=False, user_name="Ana"),
    )
    assert "in @Ana's thread" not in page.locator("#badge").text_content()
    emit(
        page,
        dict(type="reply_sent", id="r1", kind="quote", is_sent=True, user_name="Ana"),
    )
    expect(page.locator("#thread-tick")).to_contain_text("in @Ana's thread")
    emit(page, dict(type="earlier_sent", id="m2", user_name="Mehmet", ago_min=12))
    expect(page.locator("#earlier-toast")).to_contain_text("12 min ago")
    emit(page, dict(type="summary", answered=3, earlier=1, skipped=1))
    expect(page.locator("#summary")).to_contain_text(
        "3 answered · 1 from earlier · 1 skipped"
    )
    page.clock.run_for(5100)
    expect(page.locator("#summary")).to_be_hidden()
    expect(page.locator("#earlier-toast")).to_be_hidden()


def test_next_card_waits_for_stamp_and_snapshot_reconnect_restores_without_exit(
    overlay,
):
    from playwright.sync_api import expect

    page, _ = overlay
    emit(page, snapshot())
    emit(page, card())
    emit(page, answer(final_text="My own theme."))
    emit(page, dict(type="card_close", id="m1", result="answered"))
    emit(page, card("m2"))
    emit(page, answer("m2", partial_text="Second answer"))
    page.clock.run_for(700)
    expect(page.locator("#partial-text")).to_have_text("Second answer")
    page.evaluate("window.__sockets.at(-1).close()")
    page.clock.run_for(600)
    assert page.evaluate("window.__sockets.length") == 2
    data = snapshot()
    data["card"] = {**card("m3"), **answer("m3", final_text="Restored exactly.")}
    emit(page, data)
    expect(page.locator("#final-text")).to_have_text("Restored exactly.")
    assert page.locator(".exiting").count() == 0
    assert page.locator("#stamp").is_hidden()


def test_demo_geometry_theme_and_network_independence(overlay):
    page, base = overlay
    page.goto(f"{base}/overlay/?demo=1&step=5")
    page.wait_for_function("window.checkpointReady === true")
    assert page.evaluate("window.__sockets.length") == 0
    assert page.evaluate("window.checkpointDemoStepCount") >= 18
    assert page.locator('link[href="/assets/theme.css"]').count() == 1
    box = page.locator(".card").bounding_box()
    assert box["width"] <= 1920 * 0.45
    assert box["x"] + box["width"] < 1920 - 300
    assert box["y"] > 540
    assert (
        page.evaluate("getComputedStyle(document.body).backgroundColor")
        == "rgba(0, 0, 0, 0)"
    )
    assert page.evaluate("document.documentElement.scrollWidth") == 1920


def test_frozen_demo_keeps_finished_words_bright_for_screenshots(overlay):
    page, base = overlay
    page.goto(f"{base}/overlay/?demo=1&step=5")
    page.wait_for_function("window.checkpointReady === true")
    assert (
        page.locator("#final-text .word").first.evaluate(
            "node => getComputedStyle(node).opacity"
        )
        == "1"
    )
    assert (
        float(
            page.locator("#partial-text").evaluate(
                "node => getComputedStyle(node).opacity"
            )
        )
        < 0.7
    )


def test_snapshot_whitelists_queue_and_pending_and_last_three_answer_lines(overlay):
    from playwright.sync_api import expect

    page, _ = overlay
    data = snapshot()
    data["queue"] = [
        dict(
            id="waiting",
            user_name="Safe viewer",
            color="#69e3ca",
            kind="question",
            text="NEVER ON STREAM",
            earlier=dict(quote="PRIVATE EARLIER QUOTE", ago_min=12),
        )
    ]
    data["pending"] = [dict(id="reply", text="PRIVATE PENDING REPLY")]
    data["card"] = {
        **card(),
        **answer(final_text="First words " + "word " * 100 + "last words"),
    }
    emit(page, data)
    expect(page.locator("#final-text")).to_contain_text("last words")
    body = page.locator("body").text_content()
    for secret in ("NEVER ON STREAM", "PRIVATE EARLIER QUOTE", "PRIVATE PENDING REPLY"):
        assert secret not in body
    clip = page.locator(".answer-clip")
    assert clip.evaluate("node => node.scrollHeight > node.clientHeight")
    assert clip.evaluate(
        "node => node.scrollTop + node.clientHeight >= node.scrollHeight - 1"
    )
    assert clip.bounding_box()["height"] <= 117
    emit(page, snapshot())
    assert "First words" not in page.locator("body").text_content()


def test_timed_demo_refreshes_grace_time_for_each_frame(overlay):
    from playwright.sync_api import expect

    page, base = overlay
    page.goto(f"{base}/overlay/?demo=1")
    page.wait_for_function("window.checkpointReady === true")
    # Steps 0 through 5 hold for 11.2 seconds before the final-answer frame.
    page.clock.run_for(11250)
    expect(page.locator("#grace")).to_be_visible()
    assert float(page.locator("#grace").get_attribute("data-remaining")) > 2
    assert page.evaluate("window.__sockets.length") == 0


def test_all_demo_steps_render_without_errors_or_provider_connections(overlay):
    page, base = overlay
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    requests = []
    page.on("request", lambda request: requests.append(request))
    for step in range(19):
        page.goto(f"{base}/overlay/?demo=1&step={step}")
        page.wait_for_function("window.checkpointReady === true")
        assert page.evaluate("window.__sockets.length") == 0
    assert not errors
    assert not [request for request in requests if request.method == "POST"]
