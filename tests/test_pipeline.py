import io
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import pipeline
from app.config import get_settings
from app.models import AIUsage, AccountStrategy, Base, ContentPost, DurableJob, InstagramAccount, MemeCandidate
from app.rendering import RenderError, render_meme
from integrations.ai import GeneratedMeme, OllamaClient
from integrations.discovery import DiscoveryClient, DiscoveryError
from integrations.local_storage import LocalStorageClient


MEME = {"concept": "Rapat terlalu banyak", "template_type": "terminal", "top_text": "Rapat ini bisa jadi email",
        "bottom_text": "Emailnya malah undangan rapat", "caption": "Hari ini produktif mengatur rapat.",
        "hashtags": ["#kerja"], "alt_text": "Dua panel teks tentang rapat kantor", "safe": True, "rights_clear": True}
SCORE = {"niche_relevance": 90, "humor_relatability": 90, "freshness": 80, "uniqueness": 90,
         "quality_safety": 90, "safe": True, "rights_clear": True, "topic_allowed": True, "reason": "Relevan"}


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        account = InstagramAccount(username="office", niche="office", active=True, approval_mode="AUTO")
        db.add(account)
        db.flush()
        db.add(AccountStrategy(account_id=account.id, tone="dry", audience="workers", prompt_version="v1"))
        feed = {"name": "permitted", "url": "https://feeds.example.test/rss", "account_ids": [str(account.id)], "ideas_use_permitted": True}
        monkeypatch.setenv("DISCOVERY_FEEDS", json.dumps([feed]))
        monkeypatch.setenv("ENABLE_AUTOAPPROVAL", "true")
        get_settings.cache_clear()
        state = SimpleNamespace(calls=[], flagged=False, score=dict(SCORE))
        def rss(request):
            return httpx.Response(200, text="<rss><channel><item><title>Rapat kantor lagi!</title></item><item><title>RAPAT kantor lagi</title></item></channel></rss>")
        resolver = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        monkeypatch.setattr(pipeline, "discovery_client", lambda: DiscoveryClient(transport=httpx.MockTransport(rss), resolver=resolver))
        def chat(request):
            body = json.loads(request.content)
            operation = json.loads(body["messages"][1]["content"])["operation"]
            state.calls.append(operation)
            value = {"flagged": state.flagged, "reason": "Policy check"} if operation == "moderate" else state.score if operation == "score" else MEME
            return httpx.Response(200, json={"done": True, "message": {"content": json.dumps(value)},
                                             "prompt_eval_count": 12, "eval_count": 20})
        monkeypatch.setattr(pipeline, "ai_client", lambda: OllamaClient(get_settings(), transport=httpx.MockTransport(chat)))
        state.store = LocalStorageClient(tmp_path, "key")
        monkeypatch.setattr(pipeline.svc, "storage_client", lambda: state.store)
        discovery = DurableJob(account_id=account.id, kind="discovery", idempotency_key="discovery")
        generation = DurableJob(account_id=account.id, kind="generation", idempotency_key="generation")
        db.add_all([discovery, generation])
        db.commit()
        yield db, account, discovery, generation, state
    engine.dispose()


def test_discovery_dedup_generation_and_replay_without_paid_calls(pipeline_env):
    db, account, discovery, generation, state = pipeline_env
    assert pipeline.run_discovery(db, discovery, account)["discovered"] == 1
    assert pipeline.run_discovery(db, discovery, account)["discovered"] == 1
    assert len(db.scalars(select(MemeCandidate)).all()) == 1
    result = pipeline.run_generation(db, generation, account)
    assert result["generated"] == 1 and result["status"] == "REVIEW"
    post = db.scalar(select(ContentPost))
    with Image.open(io.BytesIO(state.store.path(post.media_key).read_bytes())) as image:
        assert image.size == (1080, 1350) and image.format == "JPEG"
    assert post.generation_metadata["moderation_provider"] == "ollama"
    assert len(state.calls) == 4
    pipeline.run_generation(db, generation, account)
    assert len(state.calls) == 4
    usage = db.scalars(select(AIUsage)).all()
    assert len(usage) == 4 and all(row.estimated_cost_usd == 0 for row in usage)


def test_hard_safety_rejection_before_render(pipeline_env):
    db, account, discovery, generation, state = pipeline_env
    pipeline.run_discovery(db, discovery, account)
    state.flagged = True
    assert pipeline.run_generation(db, generation, account)["rejected"] == "moderation_flagged"
    assert db.scalar(select(ContentPost)) is None


def test_source_permission_cannot_be_inferred(pipeline_env):
    db, account, discovery, generation, state = pipeline_env
    pipeline.run_discovery(db, discovery, account)
    candidate = db.scalar(select(MemeCandidate))
    candidate.discovery_metadata = {}
    db.commit()
    assert pipeline.run_generation(db, generation, account)["rejected"] == "source_permission_required"
    assert state.calls == []


@pytest.mark.parametrize("template", ["terminal", "split_panel", "geometric"])
def test_original_templates_and_overflow(template):
    meme = GeneratedMeme.model_validate({**MEME, "template_type": template})
    image = render_meme(meme)
    assert Image.open(io.BytesIO(image)).size == (1080, 1350)
    meme.top_text = "W" * 180
    with pytest.raises(RenderError):
        render_meme(meme)


def test_feed_private_addresses_and_unsafe_xml_rejected():
    feed = SimpleNamespace(url="https://feed.example.test/rss", ideas_use_permitted=True)
    resolver = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    with DiscoveryClient(resolver=resolver) as client:
        with pytest.raises(DiscoveryError):
            client.fetch(feed)
    resolver = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    payload = '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///private">]><rss><item><title>&xxe;</title></item></rss>'
    with DiscoveryClient(resolver=resolver, transport=httpx.MockTransport(lambda req: httpx.Response(200, text=payload))) as client:
        with pytest.raises(DiscoveryError):
            client.fetch(feed)
