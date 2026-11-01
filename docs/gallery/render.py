"""Render original 3:2 Devpost compositions from local Checkpoint components."""

import argparse
import functools
import http.server
import json
import struct
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
SIZE = {"width": 1920, "height": 1280}
SHOTS = ("overlay-answer", "dock-queue", "architecture")
ANSWER = "I use a dark theme and a large font so you can read it on stream."


@contextmanager
def static_server():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            route = urlparse(path).path
            if route.startswith("/gallery/"):
                route = "/docs" + route
            else:
                route = "/web" + route
            return super().translate_path(route)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(ROOT))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def prepare(page, base):
    """Keep real UI code; replace only its local sample data for this capture."""
    blocked = []

    def local_only(route):
        if not route.request.url.startswith(base + "/"):
            blocked.append(route.request.url)
            route.abort()
            return
        path = urlparse(route.request.url).path
        if path == "/overlay/demo.js":
            code = (ROOT / "web/overlay/demo.js").read_text()
            code = code.replace(
                "['Ana', 'Bob', 'Cy', 'Dee']",
                "['River', 'Sage', 'Ember', 'Fern']",
            )
            code = code.replace(
                "const text = \"It's VS Code with my own dark theme, "
                'and the font is a system monospace.";',
                f"const text = {json.dumps(ANSWER)};",
            )
            route.fulfill(body=code, content_type="text/javascript")
        elif path == "/dock/app.js":
            code = (ROOT / "web/dock/app.js").read_text()
            original = (
                "if (step === 6) event.queue = [{...demoRows[0], id: 'mehmet', "
                "user_name: 'mehmet', text: 'Which editor do you use?', "
                "earlier: {quote: 'It’s VS Code with the system font.', "
                "ago_min: 12}}];"
            )
            rows = [
                dict(
                    id="river",
                    user_name="River",
                    color="var(--cp-accent)",
                    kind="question",
                    text="Which editor do you use?",
                    earlier=dict(quote=ANSWER, ago_min=3),
                ),
                dict(
                    id="sage",
                    user_name="Sage",
                    color="var(--cp-accent-muted)",
                    kind="question",
                    text="How do you keep your focus while coding?",
                    earlier=None,
                ),
                dict(
                    id="ember",
                    user_name="Ember",
                    color="var(--cp-text)",
                    kind="other",
                    text="This is such a calm stream.",
                    earlier=None,
                ),
            ]
            assert original in code, "Dock fixture changed; update gallery adapter"
            code = code.replace(
                original, "if (step === 6) event.queue = " + json.dumps(rows) + ";"
            )
            route.fulfill(body=code, content_type="text/javascript")
        else:
            route.continue_()

    page.route("**/*", local_only)
    return blocked


def capture(page, base, name, target):
    if name not in SHOTS:
        raise ValueError("Unknown gallery image")
    page.goto(f"{base}/gallery/index.html?image={name}")
    page.wait_for_function("window.galleryReady === true")
    page.evaluate("document.fonts.ready")
    for frame in page.frames[1:]:
        frame.wait_for_function("window.checkpointReady === true")
        frame.evaluate("document.fonts.ready")
    if name == "overlay-answer":
        page.frames[1].add_style_tag(
            content="#stage { transform: scale(1.3); transform-origin: bottom right; }"
        )
        page.frame_locator("iframe").locator("#stamp").wait_for(state="visible")
    page.screenshot(path=str(target), animations="disabled")


def png_size(path):
    return struct.unpack(">II", Path(path).read_bytes()[16:24])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/gallery")
    args = parser.parse_args(argv)
    from playwright.sync_api import sync_playwright

    args.output.mkdir(parents=True, exist_ok=True)
    with static_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport=SIZE, device_scale_factor=1)
            blocked = prepare(page, base)
            for name in SHOTS:
                target = args.output / f"{name}.png"
                capture(page, base, name, target)
                if png_size(target) != (1920, 1280):
                    raise RuntimeError("Unexpected image dimensions")
                print(f"Saved {target}")
            if blocked:
                raise RuntimeError("Gallery attempted a nonlocal request")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
