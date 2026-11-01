"""Save Checkpoint demo steps, or capture a running local page with --live."""

import argparse
import functools
import http.server
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEWPORTS = {
    "overlay": {"width": 1920, "height": 1080},
    "dock": {"width": 400, "height": 900},
    "mic": {"width": 1000, "height": 900},
}


def live_url(name):
    return f"http://127.0.0.1:{os.environ.get('APP_PORT', '8200')}/{name}"


def demo_url(base, name, step):
    return f"{base}/{name}/?demo=1&step={step}"


@contextmanager
def static_server():
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    handler = functools.partial(QuietHandler, directory=str(ROOT / "web"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def capture(page, name, target):
    page.evaluate("document.fonts.ready")
    if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
        raise RuntimeError(f"{name} overflows its viewport")
    summary = page.evaluate("""() => {
      const node = document.getElementById('summary');
      return node && !node.hidden ? node.innerText : '';
    }""")
    if re.search(r"\b0 (answered|from earlier|skipped)\b", summary):
        raise RuntimeError(f"{name} summary contains a zero count")
    page.screenshot(
        path=str(target), full_page=name != "overlay", omit_background=name == "overlay"
    )
    print(f"Saved {target}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page", choices=tuple(VIEWPORTS))
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    from playwright.sync_api import sync_playwright

    output = ROOT / "data/shots"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport=VIEWPORTS[args.page])
            if args.live or args.page == "mic":
                page.goto(live_url(args.page))
                if args.page != "mic":
                    page.wait_for_function("window.checkpointReady === true")
                capture(page, args.page, output / f"{args.page}.png")
            else:
                with static_server() as base:
                    page.goto(demo_url(base, args.page, 0))
                    page.wait_for_function("window.checkpointReady === true")
                    count = page.evaluate("window.checkpointDemoStepCount")
                    for step in range(count):
                        page.goto(demo_url(base, args.page, step))
                        page.wait_for_function("window.checkpointReady === true")
                        capture(page, args.page, output / f"{args.page}-{step:02}.png")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
