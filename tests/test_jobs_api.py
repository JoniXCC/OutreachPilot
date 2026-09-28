"""Jobs, scheduler locking, demo flow and the optional REST API."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config.settings import Settings
from app.database.database import get_engine, init_db, session_scope
from app.demo import DEMO_CAMPAIGN, reset_demo, run_demo_flow
from app.scheduler.jobs import JobLockedError, job_lock, run_job


@pytest.fixture
def file_settings(settings, tmp_path) -> Settings:
    return settings.model_copy(update={"database_url": f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
                                       "max_emails_per_day": 20})


def test_demo_flow_end_to_end(file_settings):
    engine = init_db(get_engine(file_settings))
    with session_scope(engine) as session:
        report = run_demo_flow(session, file_settings)
    assert report.sent == 6 and report.replies == 5
    cats = report.summary["reply_categories"]
    assert cats["UNSUBSCRIBE"] == 1 and cats["INTERESTED"] == 1
    assert report.summary["pending_approvals"] == 2   # suggested replies wait for a human
    with session_scope(engine) as session:
        reset_demo(session, file_settings)
        report = run_demo_flow(session, file_settings)  # repeatable after reset
    assert report.sent == 6


def test_demo_refuses_without_demo_mode(file_settings):
    engine = init_db(get_engine(file_settings))
    with session_scope(engine) as session, pytest.raises(RuntimeError):
        run_demo_flow(session, file_settings.model_copy(update={"demo_mode": False}))


def test_run_job_all_is_safe_on_empty_db(file_settings):
    summary = run_job("all", file_settings, wait=False)
    assert "inbox: tracked=0" in summary
    assert "research: nothing to do" in summary
    assert "unexpected error" not in summary


def test_unknown_job(file_settings):
    assert "unknown job" in run_job("nope", file_settings)


def test_job_lock_prevents_overlap(file_settings):
    with job_lock("send", file_settings):
        with pytest.raises(JobLockedError):
            with job_lock("send", file_settings):
                pass
    with job_lock("send", file_settings):  # released afterwards
        pass


def test_api_endpoints_and_token(file_settings, monkeypatch):
    import app.main as main
    protected = file_settings.model_copy(update={"api_token": SecretStr("s3cret")})
    main.app.dependency_overrides[main.settings_dep] = lambda: protected
    engine = init_db(get_engine(protected))
    with session_scope(engine) as session:
        run_demo_flow(session, protected)
    try:
        client = TestClient(main.app)
        assert client.get("/health").json()["demo_mode"] is True
        assert client.get("/stats").status_code == 401
        headers = {"X-API-Token": "s3cret"}
        stats = client.get("/stats", headers=headers).json()
        assert stats["emails_sent"] == 6
        campaigns = client.get("/campaigns", headers=headers).json()
        assert campaigns[0]["name"] == DEMO_CAMPAIGN
        assert len(client.get("/leads", headers=headers).json()) == 6
        assert client.post("/jobs/nope", headers=headers).status_code == 404
    finally:
        main.app.dependency_overrides.clear()
