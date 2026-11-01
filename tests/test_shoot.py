import pytest

from scripts import shoot


def test_capture_rejects_visible_zero_summary_before_saving():
    class Page:
        def evaluate(self, expression):
            if expression == "document.fonts.ready":
                return None
            if "scrollWidth" in expression:
                return False
            return "CHECKPOINT SAVED ✓ 3 answered · 0 from earlier · 1 skipped"

        def screenshot(self, **kwargs):
            pytest.fail("A zero-count summary was saved")

    with pytest.raises(RuntimeError, match="zero"):
        shoot.capture(Page(), "overlay", "unused.png")


def test_checkpoint_capture_targets_and_default_port(monkeypatch):
    monkeypatch.delenv("APP_PORT", raising=False)
    assert shoot.live_url("dock") == "http://127.0.0.1:8200/dock"
    monkeypatch.setenv("APP_PORT", "18200")
    assert shoot.live_url("mic") == "http://127.0.0.1:18200/mic"
    assert shoot.VIEWPORTS["overlay"] == {"width": 1920, "height": 1080}
    assert shoot.VIEWPORTS["dock"]["width"] == 400


def test_demo_url_uses_checkpoint_steps():
    assert (
        shoot.demo_url("http://local", "dock", 3) == "http://local/dock/?demo=1&step=3"
    )
