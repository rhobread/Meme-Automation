from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import scheduling
from app.models import AccountStrategy, Base, ContentPost, DurableJob, InstagramAccount, MemeCandidate, PublishingJob
from app.security import encrypt_token


@pytest.fixture
def schedule_db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 28, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduling.svc, "now", lambda: now)
    with Session(engine, expire_on_commit=False) as db:
        accounts = []
        for niche in ("office", "gaming", "it"):
            account = InstagramAccount(username=niche, niche=niche, active=True, ig_user_id=str(100 + len(accounts)),
                                       encrypted_token=encrypt_token("test-token"), token_expires_at=now + timedelta(days=30))
            db.add(account)
            db.flush()
            accounts.append(account)
            for index in range(6):
                candidate = MemeCandidate(account_id=account.id, source_name="original", title=str(index), idea="idea",
                                          fingerprint=str(index) * 64, rights_status="ORIGINAL")
                db.add(candidate)
                db.flush()
                db.add(ContentPost(account_id=account.id, candidate_id=candidate.id, status="APPROVED",
                                   media_key=f"{account.id}/{index}.jpg", media_hash=str(index) * 64))
        db.commit()
        @contextmanager
        def account_session(account_id):
            yield db
        monkeypatch.setattr(scheduling.publishing, "account_session", account_session)
        monkeypatch.setattr(scheduling.publishing, "get_engine", lambda: engine)
        yield db, accounts, now
    engine.dispose()


def test_three_accounts_six_posts_per_day_without_duplicates(schedule_db):
    db, accounts, now = schedule_db
    for account in accounts:
        assert scheduling.schedule(account.id)["scheduled"] == 4
        assert scheduling.schedule(account.id)["scheduled"] == 0
    posts = db.scalars(select(ContentPost).where(ContentPost.status == "SCHEDULED")).all()
    zone = ZoneInfo("Asia/Jakarta")
    for day in (28, 29):
        daily = [row for row in posts if scheduling.svc.utc(row.scheduled_at).astimezone(zone).day == day]
        assert len(daily) == 6
        assert len({(row.account_id, row.scheduled_at) for row in daily}) == 6
    office = [row for row in posts if row.account_id == accounts[0].id]
    assert {scheduling.svc.utc(row.scheduled_at).astimezone(zone).strftime("%H:%M") for row in office} == {"12:00", "19:00"}


def test_pause_is_account_isolated(schedule_db):
    db, accounts, now = schedule_db
    accounts[0].active = False
    db.commit()
    assert scheduling.schedule(accounts[0].id) == {"scheduled": 0, "paused": True}
    assert scheduling.schedule(accounts[1].id)["scheduled"] == 4


def test_manual_publication_counts_toward_daily_target(schedule_db):
    db, accounts, now = schedule_db
    manual = db.scalar(select(ContentPost).where(ContentPost.account_id == accounts[0].id))
    manual.status, manual.published_at, manual.instagram_media_id = "PUBLISHED", now - timedelta(hours=1), "12345"
    db.commit()
    assert scheduling.schedule(accounts[0].id)["scheduled"] == 3
    posts = db.scalars(select(ContentPost).where(ContentPost.account_id == accounts[0].id,
                                               ContentPost.scheduled_at.is_not(None))).all()
    assert sum(scheduling.svc.utc(row.scheduled_at).astimezone(ZoneInfo("Asia/Jakarta")).day == 28 for row in posts) == 1


def test_invalid_and_insufficient_slots_rejected(schedule_db):
    db, accounts, now = schedule_db
    account = accounts[0]
    strategy = AccountStrategy(account_id=account.id, tone="friendly", audience="office", prompt_version="v1",
                               posting_times=["25:00", "12:00"])
    db.add(strategy)
    db.commit()
    with pytest.raises(HTTPException, match="HH:MM"):
        scheduling.schedule(account.id)
    strategy.posting_times = ["12:00", "12:00"]
    db.commit()
    with pytest.raises(HTTPException, match="enough distinct"):
        scheduling.schedule(account.id)


def test_zero_daily_target_and_rejected_media(schedule_db):
    db, accounts, now = schedule_db
    accounts[0].posts_per_day = 0
    db.commit()
    assert scheduling.schedule(accounts[0].id)["scheduled"] == 0
    for row in db.scalars(select(MemeCandidate).where(MemeCandidate.account_id == accounts[1].id)):
        row.rights_status = "UNKNOWN"
    db.commit()
    assert scheduling.schedule(accounts[1].id)["scheduled"] == 0


def test_missed_slot_returns_to_review_without_catchup(schedule_db, monkeypatch):
    db, accounts, now = schedule_db
    post = db.scalar(select(ContentPost).where(ContentPost.account_id == accounts[0].id))
    post.status, post.scheduled_at = "SCHEDULED", now - timedelta(hours=2)
    db.commit()
    monkeypatch.setattr(scheduling.publishing, "dispatch", lambda id: pytest.fail("Must not publish stale content"))
    assert scheduling.publish_due(accounts[0].id)["accepted"] == 0
    assert post.status == "REVIEW" and post.scheduled_at is None
    assert post.error == "missed_schedule_requires_review"


def test_due_outbox_is_idempotent(schedule_db, monkeypatch):
    db, accounts, now = schedule_db
    post = db.scalar(select(ContentPost).where(ContentPost.account_id == accounts[0].id))
    post.status, post.scheduled_at = "SCHEDULED", now - timedelta(minutes=1)
    db.commit()
    calls = []
    monkeypatch.setattr(scheduling.publishing, "gate", lambda *args: None)
    monkeypatch.setattr(scheduling.publishing, "dispatch", lambda id: calls.append(id) or True)
    assert scheduling.publish_due(accounts[0].id)["accepted"] == 1
    assert scheduling.publish_due(accounts[0].id)["accepted"] == 1
    jobs = db.scalars(select(DurableJob).where(DurableJob.kind == "publish")).all()
    assert len(jobs) == 1 and calls[0] == calls[1]


def test_due_publication_uses_real_safety_gate(schedule_db, monkeypatch):
    from app.config import get_settings
    db, accounts, now = schedule_db
    monkeypatch.setenv("ENABLE_AUTOPUBLISH", "true")
    get_settings.cache_clear()
    post = db.scalar(select(ContentPost).where(ContentPost.account_id == accounts[0].id))
    post.status, post.scheduled_at = "SCHEDULED", now - timedelta(minutes=1)
    db.commit()
    monkeypatch.setattr(scheduling.publishing, "dispatch", lambda id: True)
    assert scheduling.publish_due(accounts[0].id)["accepted"] == 1
    post.scheduled_at = now + timedelta(hours=1)
    db.commit()
    with pytest.raises(HTTPException, match="not due"):
        scheduling.publishing.gate(db, accounts[0], post)
    post.scheduled_at = now - timedelta(hours=2)
    with pytest.raises(HTTPException, match="Missed schedule"):
        scheduling.publishing.gate(db, accounts[0], post)
    post.scheduled_at = now - timedelta(minutes=1)
    monkeypatch.setenv("ENABLE_AUTOPUBLISH", "false")
    get_settings.cache_clear()
    with pytest.raises(HTTPException, match="kill switch"):
        scheduling.publish_due(accounts[0].id)


def test_queued_backlog_cannot_publish_after_worker_restart(schedule_db, monkeypatch):
    db, accounts, now = schedule_db
    post = db.scalar(select(ContentPost).where(ContentPost.account_id == accounts[0].id))
    post.status, post.scheduled_at = "SCHEDULED", now - timedelta(hours=2)
    job = DurableJob(account_id=accounts[0].id, kind="publish", idempotency_key=str(post.id), payload={"post_id": str(post.id)})
    db.add_all([job, PublishingJob(post_id=post.id)])
    db.commit()
    monkeypatch.setattr(scheduling.svc, "meta_client", lambda: pytest.fail("Stale backlog must not call Meta"))
    scheduling.publishing.run_publish(job.id)
    db.refresh(job)
    assert job.status == "FAILED" and job.error == "missed_schedule"
    assert post.status == "FAILED"
