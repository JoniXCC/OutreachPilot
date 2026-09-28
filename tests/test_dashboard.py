"""Headless smoke test: every dashboard page renders without exceptions."""

import pytest
from streamlit.testing.v1 import AppTest

from app.config import settings as settings_module

APP = str(settings_module.PROJECT_ROOT / "dashboard" / "streamlit_app.py")


@pytest.fixture
def demo_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'dash.db').as_posix()}")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("AI_PROVIDER", "mock")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "")
    monkeypatch.setenv("MIN_SECONDS_BETWEEN_EMAILS", "0")
    monkeypatch.setenv("DEMO_MAILBOX_PATH", str(tmp_path / "box.json"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


def test_all_pages_render(demo_env):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception

    # Populate data through the UI itself.
    run_demo = next(b for b in at.button if b.label == "Run complete demo")
    run_demo.click().run()
    assert not at.exception, at.exception
    assert any("Demo done" in s.value for s in at.success)

    labels = at.sidebar.radio[0].options
    for label in labels:
        at.sidebar.radio[0].set_value(label).run()
        assert not at.exception, f"{label}: {at.exception}"
        assert at.title, f"{label}: no title rendered"


def test_approve_from_queue(demo_env):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    next(b for b in at.button if b.label == "Run complete demo").click().run()
    queue = next(o for o in at.sidebar.radio[0].options if "Email Queue" in o)
    at.sidebar.radio[0].set_value(queue).run()
    approve = [b for b in at.button if b.label == "Approve"]
    assert approve, "expected suggested replies awaiting approval"
    approve[0].click().run()
    assert not at.exception, at.exception
    assert any("Approved" in s.value for s in at.success)
