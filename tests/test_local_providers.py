import io
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import get_settings
from app.main import app
from integrations.ai import AIError, AIRejected, OllamaClient
from integrations.local_storage import LocalStorageClient
from integrations.storage import StorageError


def local_settings(**kwargs):
    return SimpleNamespace(ollama_url="http://127.0.0.1:11434", ollama_model="qwen2.5:7b",
                           ollama_timeout_seconds=2, **kwargs)


def test_ollama_real_protocol_and_validation():
    calls, records = [], []
    def request(req):
        body = json.loads(req.content)
        calls.append(body)
        assert req.url.path == "/api/chat" and body["stream"] is False
        assert "format" in body and body["format"]["additionalProperties"] is False
        value = {"niche_relevance": 90, "humor_relatability": 80, "freshness": 70, "uniqueness": 85,
                 "quality_safety": 100, "safe": True, "rights_clear": True, "topic_allowed": True, "reason": "Original"}
        return httpx.Response(200, json={"done": True, "message": {"content": json.dumps(value)},
                                         "prompt_eval_count": 10, "eval_count": 20})
    with OllamaClient(local_settings(), transport=httpx.MockTransport(request)) as client:
        score = client.structured("score", {"idea": "Rapat lagi"}, lambda response, status: records.append(status))
    assert score.weighted_score == 83.75 and records == ["VALID"] and len(calls) == 1


@pytest.mark.parametrize("url,model", [("https://api.example.com", "local"), ("http://ollama.com", "local"),
                                     ("http://localhost:11434", "model:cloud"), ("http://localhost:11434/path", "local")])
def test_ollama_cloud_and_external_hosts_rejected(url, model):
    settings = local_settings()
    settings.ollama_url, settings.ollama_model = url, model
    with pytest.raises(AIError):
        OllamaClient(settings)


@pytest.mark.parametrize("payload", [{"done": False}, {"done": True, "message": {"content": "not JSON"}},
                                   {"done": True, "done_reason": "length", "message": {"content": "{}"}}])
def test_ollama_invalid_or_truncated_outputs_fail_closed(payload):
    with OllamaClient(local_settings(), transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        with pytest.raises(AIError):
            client.structured("score", {}, lambda *args: None)


def test_local_safety_flags_reject():
    payload = {"done": True, "message": {"content": json.dumps({"flagged": True, "reason": "Unsafe"})}}
    with OllamaClient(local_settings(), transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as client:
        with pytest.raises(AIRejected):
            client.moderate("untrusted content", lambda *args: None)


def test_local_storage_replay_immutability_and_traversal(tmp_path):
    store = LocalStorageClient(tmp_path, "secret", "https://media.example.test")
    store.upload("account/post.jpg", b"jpeg")
    store.ensure_upload("account/post.jpg", b"jpeg")
    with pytest.raises(StorageError):
        store.upload("account/post.jpg", b"jpeg")
    with pytest.raises(StorageError):
        store.ensure_upload("account/post.jpg", b"different")
    for key in ("../escape.jpg", "/absolute.jpg", "a/../../escape.jpg", "a\\escape.jpg", "a/%2e%2e.jpg"):
        with pytest.raises(StorageError):
            store.path(key)


def test_signed_media_endpoint_tampering_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_MEDIA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_PUBLIC_BASE_URL", "https://media.example.test")
    get_settings.cache_clear()
    settings = get_settings()
    store = LocalStorageClient(tmp_path, settings.token_encryption_key.get_secret_value(), settings.media_public_base_url)
    buffer = io.BytesIO()
    Image.new("RGB", (1080, 1350)).save(buffer, "JPEG")
    store.upload("account/post.jpg", buffer.getvalue())
    url = urlsplit(store.signed_url("account/post.jpg"))
    with TestClient(app) as client:
        result = client.get(url.path + "?" + url.query)
        assert result.status_code == 200 and result.headers["content-type"] == "image/jpeg"
        assert result.headers["cache-control"] == "no-store"
        params = parse_qs(url.query)
        assert client.get(url.path, params={"expires": params["expires"][0], "signature": "0" * 64}).status_code == 404
        assert client.get(url.path, params={"expires": 1, "signature": params["signature"][0]}).status_code == 404
        assert client.get(url.path).status_code == 422


def test_storage_without_https_allows_private_upload_not_public_signing(tmp_path):
    store = LocalStorageClient(tmp_path, "secret")
    store.upload("a.jpg", b"test")
    with pytest.raises(StorageError):
        store.signed_url("a.jpg")


def test_authenticated_private_image_preview_without_public_host(tmp_path, monkeypatch):
    import uuid
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app import services
    from app.models import Base, ContentPost, InstagramAccount, MemeCandidate
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(services, "get_engine", lambda: engine)
    monkeypatch.setenv("LOCAL_MEDIA_DIR", str(tmp_path))
    get_settings.cache_clear()
    with Session(engine) as db:
        account = InstagramAccount(username="reviewer", niche="office")
        db.add(account)
        db.flush()
        candidate = MemeCandidate(account_id=account.id, source_name="manual", title="Original", idea="Idea", fingerprint="d" * 64)
        db.add(candidate)
        db.flush()
        post = ContentPost(account_id=account.id, candidate_id=candidate.id, media_key="review.jpg")
        db.add(post)
        db.commit()
        account_id, post_id = str(account.id), str(post.id)
    with services.storage_client() as store:
        store.upload("review.jpg", b"image-bytes")
    with TestClient(app) as client:
        path = f"/api/v1/content/{post_id}/image"
        headers = {"X-API-Key": get_settings().internal_api_key.get_secret_value()}
        assert client.get(path, params={"account_id": account_id}).status_code == 401
        result = client.get(path, params={"account_id": account_id}, headers=headers)
        assert result.status_code == 200 and result.content == b"image-bytes"
        assert client.get(path, params={"account_id": str(uuid.uuid4())}, headers=headers).status_code == 404
    engine.dispose()
