import hashlib
import io
import uuid
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app import services as svc, publishing
from app.config import get_settings
from app.main import app
from app.models import Base, InstagramAccount, OAuthState, ContentPost, PublishingJob, DurableJob
from app.security import decrypt_token
from integrations.meta import MetaClient
from integrations.storage import StorageClient


@pytest.fixture
def env(tmp_path, monkeypatch, settings):
    engine = create_engine(f"sqlite:///{tmp_path / 'm2.db'}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(svc, "get_engine", lambda: engine)
    monkeypatch.setattr(publishing, "get_engine", lambda: engine)
    monkeypatch.setenv("ENABLE_AUTOPUBLISH", "true")
    get_settings.cache_clear()
    state = SimpleNamespace(engine=engine, calls=[], profile="501", status="FINISHED", quota=0,
                            publish_timeout=False, bucket_public=False, upstream_error=False,
                            broker_calls=[], storage_calls=[])

    def meta_request(request):
        state.calls.append(request)
        path = request.url.path
        if state.upstream_error:
            return httpx.Response(500, json={"error": {"message": "DO-NOT-EXPOSE-SECRET"}})
        if path == "/oauth/access_token":
            return httpx.Response(200, json={"access_token": "short-secret", "user_id": state.profile})
        if path in ("/access_token", "/refresh_access_token"):
            return httpx.Response(200, json={"access_token": "long-secret", "expires_in": 5184000})
        if path.endswith("/me"):
            return httpx.Response(200, json={"user_id": state.profile, "username": "owner"})
        if path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json={"data": [{"quota_usage": state.quota, "config": {"quota_total": 100, "quota_duration": 86400}}]})
        if path.endswith("/media"):
            return httpx.Response(200, json={"id": "601"})
        if path.endswith("/media_publish"):
            with Session(engine) as db:
                publication = db.scalar(select(PublishingJob).where(PublishingJob.container_id == "601"))
                assert publication.status == "UNCERTAIN"
            if state.publish_timeout:
                raise httpx.ReadTimeout("secret-url", request=request)
            return httpx.Response(200, json={"id": "701"})
        if path.endswith("/601"):
            return httpx.Response(200, json={"status_code": state.status})
        raise AssertionError(f"Unexpected Meta endpoint {path}")

    def storage_request(request):
        state.storage_calls.append(request)
        if request.url.path == "/storage/v1/bucket/originals":
            return httpx.Response(200, json={"public": state.bucket_public})
        if "/object/sign/" in request.url.path:
            return httpx.Response(200, json={"signedURL": "/object/sign/originals/item.jpg?token=storage-secret"})
        return httpx.Response(200, json={"Key": "originals/item.jpg"})

    monkeypatch.setattr(svc, "meta_client", lambda: MetaClient("123", "app-secret", "https://example.com/callback",
                                                             transport=httpx.MockTransport(meta_request)))
    monkeypatch.setattr(svc, "storage_client", lambda: StorageClient("https://example.supabase.co", "storage-key", "originals",
                                                                    transport=httpx.MockTransport(storage_request)))
    from app.worker import publish_task
    monkeypatch.setattr(publish_task, "apply_async", lambda args, **kwargs: state.broker_calls.append(args))
    with TestClient(app) as client:
        state.client = client
        state.headers = {"X-API-Key": get_settings().internal_api_key.get_secret_value()}
        yield state
    engine.dispose()


def create_account(env):
    response = env.client.post("/api/v1/accounts", headers=env.headers,
                               json={"username": "pending", "niche": "Programming", "active": True,
                                     "strategy": {"tone": "dry", "audience": "developers"}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def authorize(env, account_id):
    response = env.client.get("/api/v1/oauth/instagram/start", headers=env.headers, params={"account_id": account_id})
    assert response.status_code == 200
    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    callback = env.client.get("/api/v1/oauth/instagram/callback", params={"state": state, "code": "private-code"})
    assert callback.status_code == 200, callback.text
    return state


def jpeg(size=(1080, 1350), fmt="JPEG"):
    data = io.BytesIO()
    Image.new("RGB", size, "orange").save(data, fmt)
    return data.getvalue()


def upload(env, account_id, **kwargs):
    return env.client.post(f"/api/v1/accounts/{account_id}/test-image", params={"account_id": account_id},
                           headers=env.headers, data={"rights_attested": kwargs.get("rights", "true"), "caption": "Original test"},
                           files={"image": ("test.jpg", kwargs.get("data", jpeg()), "image/jpeg")})


def ready(env):
    account_id = create_account(env)
    authorize(env, account_id)
    response = upload(env, account_id)
    assert response.status_code == 201, response.text
    post_id = response.json()["id"]
    response = env.client.patch(f"/api/v1/content/{post_id}", params={"account_id": account_id},
                               headers=env.headers, json={"status": "APPROVED"})
    assert response.status_code == 200, response.text
    return account_id, post_id


def request_publish(env, account_id, post_id):
    return env.client.post(f"/api/v1/content/{post_id}/publish", params={"account_id": account_id}, headers=env.headers)


def due(env):
    with Session(env.engine) as db:
        for row in db.scalars(select(PublishingJob)):
            row.next_attempt_at = svc.now() - timedelta(seconds=1)
        db.commit()


def job_status(env, account_id, job_id):
    return env.client.get(f"/api/v1/jobs/{job_id}", params={"account_id": account_id}, headers=env.headers).json()


def test_complete_api_workflow_persisted_boundary_and_idempotency(env):
    account_id, post_id = ready(env)
    response = request_publish(env, account_id, post_id)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert request_publish(env, account_id, post_id).json()["job_id"] == job_id
    publishing.run_publish(job_id)
    assert job_status(env, account_id, job_id)["container_id"] == "601"
    publishing.run_publish(job_id)  # not yet due
    assert not any(r.url.path.endswith("media_publish") for r in env.calls)
    due(env)
    publishing.run_publish(job_id)
    assert job_status(env, account_id, job_id)["status"] == "SUCCEEDED"
    publishing.run_publish(job_id)
    assert sum(r.url.path.endswith("media_publish") for r in env.calls) == 1
    assert sum(r.url.path.endswith("/media") for r in env.calls) == 1
    response = env.client.get(f"/api/v1/content/{post_id}", params={"account_id": account_id}, headers=env.headers)
    assert response.json()["instagram_media_id"] == "701"
    assert env.storage_calls[1].headers["x-upsert"] == "false"


def test_oauth_state_hashed_single_use_and_encrypted_token(env):
    account_id = create_account(env)
    state = authorize(env, account_id)
    with Session(env.engine) as db:
        oauth = db.scalar(select(OAuthState))
        assert oauth.state_hash == hashlib.sha256(state.encode()).hexdigest()
        assert oauth.consumed_at is not None
        account = db.get(InstagramAccount, uuid.UUID(account_id))
        assert b"long-secret" not in account.encrypted_token
        assert decrypt_token(account.encrypted_token) == "long-secret"
        assert account.token_acquired_at is not None
    response = env.client.get("/api/v1/oauth/instagram/callback", params={"state": state, "code": "again"})
    assert response.status_code == 400
    assert env.client.get("/api/v1/oauth/instagram/start", params={"account_id": account_id}).status_code == 401
    assert "long-secret" not in env.client.get("/api/v1/accounts", headers=env.headers).text


def test_oauth_expiry_rejects_without_http(env):
    account_id = create_account(env)
    with Session(env.engine) as db:
        db.add(OAuthState(account_id=uuid.UUID(account_id), state_hash=hashlib.sha256(b"expired").hexdigest(),
                          expires_at=svc.now() - timedelta(seconds=1)))
        db.commit()
    assert env.client.get("/api/v1/oauth/instagram/callback", params={"state": "expired", "code": "secret"}).status_code == 400
    assert not env.calls


def test_concurrent_oauth_callbacks_consume_once(env):
    from concurrent.futures import ThreadPoolExecutor
    account_id = create_account(env)
    response = env.client.get("/api/v1/oauth/instagram/start", params={"account_id": account_id}, headers=env.headers)
    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    def callback(_):
        return env.client.get("/api/v1/oauth/instagram/callback", params={"state": state, "code": "private-code"}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(callback, range(2))) == [200, 400]
    assert sum(r.url.path == "/oauth/access_token" for r in env.calls) == 1


def test_reauthorization_cannot_change_existing_identity(env):
    account_id = create_account(env)
    authorize(env, account_id)
    env.profile = "999"
    response = env.client.get("/api/v1/oauth/instagram/start", params={"account_id": account_id}, headers=env.headers)
    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    assert env.client.get("/api/v1/oauth/instagram/callback", params={"state": state, "code": "private-code"}).status_code == 409
    with Session(env.engine) as db:
        assert db.get(InstagramAccount, uuid.UUID(account_id)).ig_user_id == "501"


def test_refresh_age_and_expiry_gates(env):
    account_id = create_account(env)
    authorize(env, account_id)
    url = f"/api/v1/accounts/{account_id}/refresh-token?account_id={account_id}"
    assert env.client.post(url, headers=env.headers).status_code == 409
    with Session(env.engine) as db:
        db.get(InstagramAccount, uuid.UUID(account_id)).token_acquired_at = svc.now() - timedelta(hours=25)
        db.commit()
    assert env.client.post(url, headers=env.headers).status_code == 200
    with Session(env.engine) as db:
        account = db.get(InstagramAccount, uuid.UUID(account_id))
        account.token_acquired_at = svc.now() - timedelta(days=61)
        account.token_expires_at = svc.now() - timedelta(days=1)
        db.commit()
    assert env.client.post(url, headers=env.headers).status_code == 409


@pytest.mark.parametrize("data", [b"invalid", jpeg((319, 400)), jpeg((1441, 1000)), jpeg((500, 1000)), jpeg(fmt="PNG"), b"x" * (8 * 1024 * 1024 + 1)])
def test_invalid_jpeg_never_stored(env, data):
    account_id = create_account(env)
    assert upload(env, account_id, data=data).status_code == 422
    assert not env.storage_calls


def test_rights_review_and_private_storage_gates(env):
    account_id = create_account(env)
    authorize(env, account_id)
    assert upload(env, account_id, rights="false").status_code == 422
    env.bucket_public = True
    assert upload(env, account_id).status_code == 502
    env.bucket_public = False
    response = upload(env, account_id)
    assert response.json()["status"] == "REVIEW"
    assert request_publish(env, account_id, response.json()["id"]).status_code == 409
    assert upload(env, account_id).status_code == 409


def test_three_account_isolation_and_strategy_patch(env):
    ids = []
    for profile in ("501", "502", "503"):
        env.profile = profile
        account_id = create_account(env)
        authorize(env, account_id)
        ids.append(account_id)
    post_id = upload(env, ids[0]).json()["id"]
    assert env.client.get("/api/v1/content", params={"account_id": ids[1]}, headers=env.headers).json() == []
    assert env.client.get(f"/api/v1/content/{post_id}", params={"account_id": ids[1]}, headers=env.headers).status_code == 404
    assert request_publish(env, ids[1], post_id).status_code == 404
    assert env.client.patch(f"/api/v1/content/{post_id}?account_id={ids[0]}", headers=env.headers,
                            json={"status": "APPROVED", "account_id": ids[1]}).status_code == 409
    assert env.client.patch(f"/api/v1/accounts/{ids[0]}?account_id={ids[1]}", headers=env.headers,
                            json={"active": False}).status_code == 409
    response = env.client.patch(f"/api/v1/accounts/{ids[0]}", headers=env.headers,
                                json={"strategy": {"tone": "friendly"}})
    assert response.status_code == 200
    assert response.json()["strategy"]["tone"] == "friendly"
    assert response.json()["strategy"]["audience"] == "developers"
    assert env.client.post(f"/api/v1/content/{post_id}/publish?account_id={ids[0]}", headers=env.headers,
                           json={"account_id": ids[1]}).status_code == 409
    assert env.client.post(f"/api/v1/accounts/{ids[0]}/refresh-token?account_id={ids[0]}", headers=env.headers,
                           json={"account_id": ids[1]}).status_code == 409


@pytest.mark.parametrize("gate", ["kill", "pause", "expired", "rights", "quota"])
def test_worker_rechecks_safety_gates_before_external_side_effect(env, monkeypatch, gate):
    account_id, post_id = ready(env)
    job_id = request_publish(env, account_id, post_id).json()["job_id"]
    if gate == "kill":
        monkeypatch.setenv("ENABLE_AUTOPUBLISH", "false")
        get_settings.cache_clear()
    elif gate == "quota":
        env.quota = 25
    else:
        with Session(env.engine) as db:
            account = db.get(InstagramAccount, uuid.UUID(account_id))
            if gate == "pause":
                account.active = False
            elif gate == "expired":
                account.token_expires_at = svc.now() - timedelta(seconds=1)
            else:
                db.get(ContentPost, uuid.UUID(post_id)).status = "REJECTED"
            db.commit()
    publishing.run_publish(job_id)
    assert not any(r.url.path.endswith("/media") or r.url.path.endswith("media_publish") for r in env.calls)


def test_timeout_restart_reconcile_never_reposts(env):
    account_id, post_id = ready(env)
    job_id = request_publish(env, account_id, post_id).json()["job_id"]
    publishing.run_publish(job_id)
    due(env)
    env.publish_timeout = True
    publishing.run_publish(job_id)
    assert job_status(env, account_id, job_id)["publishing_status"] == "UNCERTAIN"
    publishing.run_publish(job_id)
    assert publishing.recover_pending() == 0
    url = f"/api/v1/content/{post_id}/reconcile?account_id={account_id}"
    response = env.client.post(url, headers=env.headers, json={})
    assert response.json()["container_status"] == "FINISHED"
    assert response.json()["publishing_status"] == "UNCERTAIN"
    due(env)
    env.status = "PUBLISHED"
    response = env.client.post(url, headers=env.headers, json={})
    assert response.json()["error"] == "published_missing_media_id"
    due(env)
    assert env.client.post(url, headers=env.headers, json={"verified_media_id": "701"}).status_code == 422
    response = env.client.post(url, headers=env.headers, json={"verified_media_id": "701", "operator_verified": True})
    assert response.json()["publishing_status"] == "SUCCEEDED"
    publishing.run_publish(job_id)
    assert sum(r.url.path.endswith("media_publish") for r in env.calls) == 1


def test_broker_failure_is_durable_and_recovered(env, monkeypatch):
    account_id, post_id = ready(env)
    from app.worker import publish_task
    def unavailable(**kwargs):
        raise ConnectionError("redis://private:secret@server")
    monkeypatch.setattr(publish_task, "apply_async", unavailable)
    response = request_publish(env, account_id, post_id)
    assert response.status_code == 202
    assert response.json()["dispatch"] == "pending_recovery"
    assert "secret" not in response.text
    monkeypatch.setattr(publish_task, "apply_async", lambda **kwargs: env.broker_calls.append(kwargs))
    assert publishing.recover_pending() == 1
    assert job_status(env, account_id, response.json()["job_id"])["status"] == "PENDING"


def test_bounded_polling_and_container_reuse(env):
    account_id, post_id = ready(env)
    job_id = request_publish(env, account_id, post_id).json()["job_id"]
    env.status = "IN_PROGRESS"
    for _ in range(7):
        due(env)
        publishing.run_publish(job_id)
    assert job_status(env, account_id, job_id)["status"] == "FAILED"
    assert sum(r.url.path.endswith("/media") for r in env.calls) == 1
    assert not any(r.url.path.endswith("media_publish") for r in env.calls)


@pytest.mark.parametrize("status,expected", [("ERROR", "FAILED"), ("EXPIRED", "FAILED"), ("PUBLISHED", "UNCERTAIN")])
def test_terminal_container_status_never_publishes(env, status, expected):
    account_id, post_id = ready(env)
    job_id = request_publish(env, account_id, post_id).json()["job_id"]
    publishing.run_publish(job_id)
    due(env)
    env.status = status
    publishing.run_publish(job_id)
    assert job_status(env, account_id, job_id)["publishing_status"] == expected
    assert not any(r.url.path.endswith("media_publish") for r in env.calls)


def test_pause_after_container_creation_halts_publish(env):
    account_id, post_id = ready(env)
    job_id = request_publish(env, account_id, post_id).json()["job_id"]
    publishing.run_publish(job_id)
    due(env)
    response = env.client.patch(f"/api/v1/accounts/{account_id}", headers=env.headers, json={"active": False})
    assert response.status_code == 200
    publishing.run_publish(job_id)
    assert not any(r.url.path.endswith("media_publish") for r in env.calls)
    assert env.client.patch(f"/api/v1/content/{post_id}?account_id={account_id}", headers=env.headers,
                            json={"status": "REJECTED"}).status_code == 409


def test_cross_account_job_status_hidden(env):
    account_id, post_id = ready(env)
    job_id = request_publish(env, account_id, post_id).json()["job_id"]
    other = create_account(env)
    assert env.client.get(f"/api/v1/jobs/{job_id}?account_id={other}", headers=env.headers).status_code == 404


def test_error_and_validation_responses_are_redacted(env):
    account_id = create_account(env)
    env.upstream_error = True
    response = env.client.get("/api/v1/oauth/instagram/start", params={"account_id": account_id}, headers=env.headers)
    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    response = env.client.get("/api/v1/oauth/instagram/callback", params={"state": state, "code": "DO-NOT-EXPOSE-SECRET"})
    assert response.status_code == 502
    assert "SECRET" not in response.text
    response = env.client.post("/api/v1/accounts", headers=env.headers, json={"secret": "DO-NOT-EXPOSE-SECRET"})
    assert response.status_code == 422 and "SECRET" not in response.text


@pytest.mark.parametrize("method,path", [("get", "/accounts"), ("post", "/accounts"), ("get", "/content"),
    ("patch", "/content/{id}"), ("post", "/content/{id}/publish"), ("post", "/content/{id}/reconcile"),
    ("get", "/jobs/{id}"), ("post", "/accounts/{id}/test-image"), ("post", "/accounts/{id}/refresh-token")])
def test_internal_routes_require_key(env, method, path):
    path = path.replace("{id}", str(uuid.uuid4()))
    assert getattr(env.client, method)("/api/v1" + path).status_code == 401
