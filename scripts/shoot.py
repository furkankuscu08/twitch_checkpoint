"""Save browser screenshots; overlay demo needs no running game server."""

import argparse
import base64
import functools
import http.server
import os
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def webcam_background(page):
    """Own synthetic room/subject; screenshot-only, never part of the overlay."""
    page.evaluate("""() => {
        const backdrop = document.createElement('div');
        backdrop.id = 'shoot-background';
        backdrop.style.cssText = 'position:fixed;inset:0;z-index:0';
        backdrop.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 1920 1080" width="1920" height="1080">
          <defs>
            <linearGradient id="room"><stop stop-color="#292637"/>
              <stop offset="1" stop-color="#c6bfb1"/></linearGradient>
            <radialGradient id="skin"><stop stop-color="#e5b797"/>
              <stop offset="1" stop-color="#9f6853"/></radialGradient>
            <linearGradient id="shirt"><stop stop-color="#365c62"/>
              <stop offset="1" stop-color="#17282f"/></linearGradient>
          </defs>
          <path fill="url(#room)" d="M0 0h1920v1080H0z"/>
          <path fill="#eee9dd" d="M1420 120h430v650h-430z"/>
          <path stroke="#8c8984" stroke-width="16"
            d="M1635 120v650m-215-325h430"/>
          <path fill="#181e29" d="M80 220h440v660H80z"/>
          <path stroke="#6e5b50" stroke-width="20"
            d="M80 420h440M80 640h440M80 860h440"/>
          <path fill="#626d59" d="M110 300h60v110h-60zm105-30h35v140h-35z"/>
          <path fill="#9e8061" d="M270 330h100v80H270z"/>
          <path fill="url(#shirt)" d="M520 1080v-85q15-240 280-270h320
            q265 30 280 270v85z"/>
          <path fill="#ad785d" d="M880 620h160v180q-80 70-160 0z"/>
          <ellipse cx="960" cy="445" rx="177" ry="242" fill="#30231f"/>
          <ellipse cx="960" cy="490" rx="151" ry="201" fill="url(#skin)"/>
          <path fill="#30231f" d="M805 408q-30-240 155-210q200-5 162 230
            q-40-70-75-90q-150 55-242 70z"/>
          <path stroke="#42302a" stroke-width="10" fill="none"
            d="M858 458q35-20 66 0m70 0q35-20 66 0"/>
          <ellipse cx="894" cy="474" rx="9" ry="12" fill="#292626"/>
          <ellipse cx="1028" cy="474" rx="9" ry="12" fill="#292626"/>
          <path stroke="#825142" stroke-width="7" fill="none"
            d="M962 480l-15 63h25m-55 48q44 22 89-2"/>
          <text x="960" y="1050" text-anchor="middle" fill="#c6d6d8"
            font-family="Arial" font-size="18">SYNTHETIC WEBCAM PREVIEW</text>
        </svg>`;
        document.body.prepend(backdrop);
        document.getElementById('stage').style.zIndex = '1';
    }""")


def shoot_overlay(browser, output):
    with static_server() as base:
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(f"{base}/overlay/?demo=1&step=0&latency=1")
        page.wait_for_function("window.overlayReady === true")
        count = page.evaluate("window.overlayDemoSteps.length")
        shots = []
        webcam_shots = []
        for step in range(count):
            page.goto(f"{base}/overlay/?demo=1&step={step}&latency=1")
            page.wait_for_function("window.overlayReady === true")
            page.evaluate("document.fonts.ready")
            image = page.screenshot(
                path=str(output / f"overlay-{step:02}.png"), omit_background=True
            )
            label = page.evaluate(
                "[...new Set(window.overlayDemoSteps["
                f"{step}].map(event => event.type))].join(' + ')"
            )
            if step in page.evaluate("window.overlayDemoCompactSteps"):
                label = "compact card"
            webcam_background(page)
            preview = page.screenshot(
                path=str(output / f"overlay-webcam-{step:02}.png")
            )
            page.evaluate("""() => {
                document.getElementById('shoot-background').remove();
                document.getElementById('stage').style.zIndex = '';
            }""")
            webcam_encoded = base64.b64encode(preview).decode("ascii")
            webcam_shots.append(
                f'<figure><img src="data:image/png;base64,{webcam_encoded}">'
                f"<figcaption>{step:02} · {label}</figcaption></figure>"
            )
            encoded = base64.b64encode(image).decode("ascii")
            shots.append(
                f'<figure><img src="data:image/png;base64,{encoded}">'
                f"<figcaption>{step:02} · {label}</figcaption></figure>"
            )
        page.locator("#scoreboard").screenshot(path=str(output / "overlay-panel.png"))

        # Exercise the actual timed demo, then sample its CSS shrink halfway.
        motion = browser.new_page(viewport={"width": 1920, "height": 1080})
        motion.clock.install()
        motion.clock.pause_at(motion.evaluate("Date.now() + 1000"))
        motion.goto(f"{base}/overlay/?demo=1&latency=1")
        motion.wait_for_function("window.overlayReady === true")
        until_shrink = motion.evaluate("""() => {
            const first = window.overlayDemoSteps.findIndex(
                step => step.some(event => event.type === 'boom'));
            let time = 3000; // BANNER_FULL_S = 3; browser tests check this boundary.
            for (let i = 0; i < first; i++) time += window.overlayDemoHolds[i] || 1700;
            return time;
        }""")
        motion.clock.run_for(until_shrink)
        motion.locator("#boom-banner").evaluate("""el => {
            el.getAnimations({subtree: true}).forEach(animation => {
                animation.pause(); animation.currentTime = 150;
            });
        }""")
        webcam_background(motion)
        motion.screenshot(path=str(output / "overlay-shrink-midpoint.png"))
        motion.close()

        page.set_viewport_size({"width": 1500, "height": 1200})
        for name, title, figures in [
            ("overlay-gallery.png", "transparent background", shots),
            ("overlay-webcam-gallery.png", "synthetic webcam background", webcam_shots),
        ]:
            page.set_content(
                "<style>body{margin:0;padding:24px;background:#11131a;color:#eee;"
                "font:16px Arial}h1{font-size:24px;margin:0 0 20px}"
                "main{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}"
                "figure{margin:0;border:1px solid #42444e;background:"
                "repeating-conic-gradient(#242731 0% 25%,#20232b 0% 50%) 0/24px 24px}"
                "img{display:block;width:100%}figcaption{padding:12px;background:#191b23}"
                "</style><h1>Minefield · all demo steps · " + title + "</h1>"
                "<main>" + "".join(figures) + "</main>"
            )
            page.screenshot(path=str(output / name), full_page=True)
        page.close()
        print(
            f"Saved {count} transparent/webcam steps, galleries "
            f"and shrink midpoint to {output}"
        )


def shoot_page(browser, name, output):
    port = (
        os.environ.get("PLANT_PORT", "8101")
        if name == "plant"
        else (os.environ.get("GAME_PORT", "8100"))
    )
    path = "/" if name == "plant" else f"/{name}"
    width = {"dock": 400, "plant": 390, "mic": 1000}[name]
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.goto(f"http://127.0.0.1:{port}{path}")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(300)
    if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
        raise RuntimeError(f"{name} overflows its {width}px viewport")
    target = output / f"{name}.png"
    page.screenshot(path=str(target), full_page=True)
    page.close()
    print(f"Saved {target}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page", choices=("overlay", "dock", "plant", "mic"))
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    output = ROOT / "data" / "shots"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            if args.page == "overlay":
                shoot_overlay(browser, output)
            else:
                shoot_page(browser, args.page, output)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
