"""Gallery renders use local pages and fictional fixtures, never providers."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def renderer():
    path = ROOT / "docs/gallery/render.py"
    assert path.exists(), "WP7 gallery renderer has not been implemented"
    spec = importlib.util.spec_from_file_location("gallery_render", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.browser
def test_gallery_renders_three_local_images_with_real_components(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    gallery = renderer()
    with gallery.static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport=gallery.SIZE, device_scale_factor=1)
            errors, sockets, mutations = [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("websocket", lambda socket: sockets.append(socket.url))
            page.on(
                "request",
                lambda request: (
                    mutations.append(request.method)
                    if request.method != "GET"
                    else None
                ),
            )
            blocked = gallery.prepare(page, base)
            for name in gallery.SHOTS:
                target = tmp_path / f"{name}.png"
                gallery.capture(page, base, name, target)
                assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
                assert gallery.png_size(target) == (1920, 1280)
                expect(page.locator("h1")).to_be_visible()
                assert (
                    page.evaluate(
                        "getComputedStyle(document.documentElement).colorScheme"
                    )
                    == "dark"
                )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth && "
                    "document.documentElement.scrollHeight <= innerHeight"
                )
                text = page.locator("body").inner_text()
                assert not any(
                    login in text
                    for login in (
                        "pixelmoth01",
                        "mossy_otter",
                        "minefield_test",
                        "izleyici0808",
                    )
                )
                if name == "overlay-answer":
                    frame = page.frame_locator("iframe")
                    expect(frame.locator(".card.exiting #stamp")).to_be_visible()
                    expect(frame.locator("#final-text")).to_have_text(gallery.ANSWER)
                elif name == "dock-queue":
                    frame = page.frame_locator("iframe")
                    expect(frame.locator(".queue-row")).to_have_count(3)
                    expect(
                        frame.get_by_role("button", name="Send answer from 3 min ago")
                    ).to_be_visible()
                else:
                    for label in (
                        "Mic page",
                        "STT engine",
                        "Deepgram Flux",
                        "Server",
                        "Overlay + dock",
                        "Twitch",
                        "EventSub",
                        "Threaded reply",
                        "no LLM · quotes only",
                    ):
                        expect(page.get_by_text(label, exact=True)).to_be_visible()
            assert errors == []
            assert blocked == []
            assert sockets == []
            assert mutations == []
        finally:
            browser.close()
