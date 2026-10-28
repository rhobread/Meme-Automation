from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import jobs, publishing, services as svc
from app.config import get_settings
from app.main import app
from app.models import Base, DurableJob, InstagramAccount


@pytest.fixture
def job_env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    for module in (jobs, publishing, svc):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(jobs, "dispatch_job", lambda id: False)
    with Session(engine, expire_on_commit=False) as db:
        account = InstagramAccount(username="office", niche="office", active=True)
        db.add(account)
        db.commit()
        yield engine, db, account
    engine.dispose()


def test_job_api_contract_idempotency_and_scope(job_env):
    engine, db, account = job_env
    headers = {"X-API-Key": get_settings().internal_api_key.get_secret_value()}
    with TestClient(app) as client:
        body = {"account_id": str(account.id), "idempotency_key": "n8n:schedule:123"}
        first = client.post("/api/v1/jobs/schedule", headers=headers, json=body)
        second = client.post("/api/v1/jobs/schedule", headers=headers, json=body)
        assert first.status_code == second.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]
        assert first.json()["dispatch"] == "pending_recovery"
        assert client.post("/api/v1/jobs/schedule", json=body).status_code == 401
        result = client.get("/api/v1/jobs/" + first.json()["job_id"], headers=headers, params={"account_id": str(account.id)})
        assert result.status_code == 200 and result.json()["kind"] == "schedule"
    assert len(db.scalars(select(DurableJob)).all()) == 1


def test_runner_schedule_reuses_account_lock_session(job_env):
    engine, db, account = job_env
    job = jobs.enqueue_job(db, account.id, "schedule", "once")
    jobs.run_job(job.id)
    db.expire_all()
    assert db.get(DurableJob, job.id).status == "SUCCEEDED"
    assert db.get(DurableJob, job.id).result["scheduled"] == 0
    jobs.run_job(job.id)
    db.refresh(job)
    assert job.attempts == 1


def test_bounded_failure_restart_and_manual_recovery(job_env, monkeypatch):
    engine, db, account = job_env
    job = jobs.enqueue_job(db, account.id, "discovery", "fail")
    def failing(db, job, account):
        job.payload = {"_checkpoint": True}
        db.commit()
        raise RuntimeError("sensitive upstream error")
    for attempt in range(3):
        jobs.run_job(job.id, {"discovery": failing})
        db.expire_all()
        row = db.get(DurableJob, job.id)
        assert "sensitive" not in (row.error or "")
        if attempt < 2:
            assert row.status == "PENDING"
            row.next_attempt_at = svc.now() - timedelta(seconds=1)
            db.commit()
    assert row.status == "FAILED" and row.payload["_checkpoint"] is True
    with TestClient(app) as client:
        response = client.post(f"/api/v1/jobs/{job.id}/retry", params={"account_id": str(account.id)},
                               headers={"X-API-Key": get_settings().internal_api_key.get_secret_value()})
        assert response.status_code == 202
    db.expire_all()
    assert db.get(DurableJob, job.id).attempts == 0


def test_runner_pause_does_not_consume_attempt_budget(job_env):
    engine, db, account = job_env
    account.active = False
    db.commit()
    job = jobs.enqueue_job(db, account.id, "schedule", "pause")
    jobs.run_job(job.id)
    db.refresh(job)
    assert job.attempts == 0 and job.status == "PENDING"


def test_recovery_skips_paused_accounts_without_blocking_others(job_env, monkeypatch):
    engine, db, account = job_env
    paused = InstagramAccount(username="paused", niche="gaming", active=False)
    db.add(paused)
    db.commit()
    first = jobs.enqueue_job(db, paused.id, "schedule", "paused")
    second = jobs.enqueue_job(db, account.id, "schedule", "active")
    calls = []
    monkeypatch.setattr(jobs, "dispatch_job", lambda id: calls.append(id) or True)
    assert jobs.recover_jobs() == 1 and calls == [second.id]


def test_publish_job_cannot_be_manually_retried(job_env):
    engine, db, account = job_env
    job = DurableJob(account_id=account.id, kind="publish", idempotency_key="unsafe", status="FAILED")
    db.add(job)
    db.commit()
    with TestClient(app) as client:
        response = client.post(f"/api/v1/jobs/{job.id}/retry", params={"account_id": str(account.id)},
                               headers={"X-API-Key": get_settings().internal_api_key.get_secret_value()})
        assert response.status_code == 409


def test_weekly_housekeeping_is_idempotent_and_persists_report(job_env, monkeypatch):
    from app import maintenance
    engine, db, account = job_env
    monkeypatch.setattr(maintenance, "get_engine", lambda: engine)
    calls = []
    monkeypatch.setattr(jobs, "dispatch_job", lambda id: calls.append(id) or True)
    assert maintenance.enqueue_housekeeping() == 1
    assert maintenance.enqueue_housekeeping() == 1
    assert calls[0] == calls[1]
    report_job = db.scalar(select(DurableJob).where(DurableJob.kind == "weekly-report"))
    jobs.run_job(report_job.id)
    db.refresh(report_job)
    assert report_job.status == "SUCCEEDED" and report_job.result["report_version"] == "age-cohorts-v1"
    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/weekly", params={"account_id": str(account.id)},
                              headers={"X-API-Key": get_settings().internal_api_key.get_secret_value()})
        assert response.status_code == 200 and response.json()["report"]["cohorts"][0]["small_sample"]
