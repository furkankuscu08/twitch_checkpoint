from scripts import shoot


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
