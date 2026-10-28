from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import analytics
from app.config import get_settings
from app.main import app
from app.maintenance import refresh_account
from app.models import Base, ContentPost, DurableJob, InstagramAccount, MemeCandidate, PostMetric, PublishingJob
from app.security import encrypt_token, decrypt_token


@pytest.fixture
def analytics_db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 28, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(analytics.svc, "now", lambda: now)
    monkeypatch.setattr(analytics.svc, "get_engine", lambda: engine)
    with Session(engine, expire_on_commit=False) as db:
        account = InstagramAccount(username="office", niche="work", active=True, ig_user_id="123",
                                   encrypted_token=encrypt_token("private-token"), token_expires_at=now + timedelta(days=30))
        db.add(account)
        db.flush()
        candidate = MemeCandidate(account_id=account.id, source_name="original", title="Meeting",
                                  idea="An original joke", rights_status="ORIGINAL", fingerprint="b" * 64)
        db.add(candidate)
        db.flush()
        post = ContentPost(account_id=account.id, candidate_id=candidate.id, status="PUBLISHED",
                           published_at=now - timedelta(hours=25), instagram_media_id="456")
        db.add(post)
        db.commit()
        @contextmanager
        def account_session(account_id):
            yield db
        monkeypatch.setattr(analytics, "account_session", account_session)
        yield db, account, post, now
    engine.dispose()


def test_age_buckets_do_not_invent_history():
    at = datetime(2026, 10, 28, tzinfo=timezone.utc)
    assert analytics.age_bucket(at - timedelta(hours=23), at) is None
    assert analytics.age_bucket(at - timedelta(hours=25), at) == 24
    assert analytics.age_bucket(at - timedelta(hours=73), at) == 72
    assert analytics.age_bucket(at - timedelta(days=8), at) == 168


def test_rate_missingness_and_zero_reach():
    assert analytics.rates({"reach": 0, "shares": 3})["share_rate"] is None
    assert analytics.rates({"reach": 10})["share_rate"] is None
    result = analytics.rates({"reach": 100, "shares": 5, "saves": 2, "likes": 10, "comments": 3})
    assert result == {"share_rate": .05, "save_rate": .02, "engagement_rate": .20}


def test_collection_is_idempotent_and_preserves_missing(analytics_db, monkeypatch):
    db, account, post, at = analytics_db
    calls = []
    class Meta:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def media_insights(self, media_id, token):
            calls.append(media_id)
            return {"reach": 100, "saved": 2, "shares": 5}
    monkeypatch.setattr(analytics.svc, "meta_client", Meta)
    assert analytics.collect(account.id) == {"collected": 1}
    assert analytics.collect(account.id) == {"collected": 0}
    assert calls == ["456"]
    metric = db.scalar(select(PostMetric))
    assert metric.likes is None and metric.views is None
    assert metric.saves == 2 and metric.raw["age_hours"] == 24
    report = analytics.summary(db, account.id)
    cohort = report["cohorts"][0]
    assert cohort["comparable_snapshots"] == 1 and cohort["small_sample"]
    assert cohort["rates"]["engagement_rate"] is None
    assert cohort["rates"]["save_rate"] == .02


def test_late_collection_is_not_comparable(analytics_db, monkeypatch):
    db, account, post, at = analytics_db
    db.add(PostMetric(post_id=post.id, collected_at=at, reach=100,
                      raw={"age_hours": 24, "actual_age_hours": 60}))
    db.commit()
    cohort = analytics.summary(db, account.id)["cohorts"][0]
    assert cohort["snapshots"] == 1 and cohort["comparable_snapshots"] == 0
    assert cohort["totals"]["reach"] is None


def test_collection_respects_pause_and_token_expiry(analytics_db):
    db, account, post, at = analytics_db
    account.active = False
    db.commit()
    with pytest.raises(HTTPException, match="Account is paused"):
        analytics.collect(account.id)
    account.active = True
    account.token_expires_at = at - timedelta(seconds=1)
    db.commit()
    with pytest.raises(HTTPException, match="Valid token"):
        analytics.collect(account.id)


def test_monitoring_does_not_expose_credentials(analytics_db):
    db, account, post, at = analytics_db
    assert analytics.monitoring(db)["healthy"]
    db.add(DurableJob(account_id=account.id, kind="generation", idempotency_key="failed", status="FAILED", created_at=at))
    db.add(PublishingJob(post_id=post.id, status="UNCERTAIN"))
    db.commit()
    result = analytics.monitoring(db)
    assert not result["healthy"] and result["failed_jobs"] == 1 and result["uncertain_publications"] == 1
    assert "private-token" not in str(result)


def test_summary_and_monitoring_api_require_authentication(analytics_db):
    db, account, post, at = analytics_db
    with TestClient(app) as client:
        assert client.get("/api/v1/monitoring").status_code == 401
        headers = {"X-API-Key": get_settings().internal_api_key.get_secret_value()}
        response = client.get("/api/v1/analytics/summary", headers=headers, params={"account_id": str(account.id)})
        assert response.status_code == 200 and response.json()["window_days"] == 14
        assert client.get("/api/v1/monitoring", headers=headers).json()["healthy"]
        assert client.get("/api/v1/analytics/summary", headers=headers, params={"account_id": str(uuid.uuid4())}).status_code == 404


def test_token_maintenance_refresh_and_identity_guard(analytics_db, monkeypatch):
    db, account, post, at = analytics_db
    account.token_acquired_at = at - timedelta(days=30)
    account.token_expires_at = at + timedelta(days=3)
    account.ig_user_id = "123"
    db.commit()
    class Meta:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def refresh(self, token):
            assert token == "private-token"
            return SimpleNamespace(access_token="renewed-token", expires_in=5184000)
        def get_profile(self, token): return SimpleNamespace(user_id="123")
    monkeypatch.setattr(analytics.svc, "meta_client", Meta)
    assert refresh_account(db, None, account)["refreshed"]
    assert decrypt_token(account.encrypted_token) == "renewed-token"
    assert refresh_account(db, None, account) == {"refreshed": False}


def test_late_insights_do_not_invent_three_snapshots(analytics_db, monkeypatch):
    db, account, post, at = analytics_db
    post.published_at = at - timedelta(days=8)
    db.commit()
    class Meta:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def media_insights(self, *args): return {}
    monkeypatch.setattr(analytics.svc, "meta_client", Meta)
    assert analytics.collect(account.id)["collected"] == 1
    assert db.scalar(select(PostMetric)).raw["age_hours"] == 168
    assert post.status == "ANALYZED"
    assert analytics.summary(db, account.id)["cohorts"][2]["snapshots"] == 1
