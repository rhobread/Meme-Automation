"""Ideas-only discovery and checkpointed, safety-gated original generation."""
import hashlib
import json
import uuid

from sqlalchemy import select

from app import services as svc
from app.config import get_settings
from app.models import AccountStrategy, AIUsage, ContentPost, MemeCandidate
from app.rendering import RENDER_VERSION, RenderError, render_meme
from integrations.ai import AIClient, OllamaClient, AIRejected, GeneratedMeme, IdeaScore
from integrations.discovery import DiscoveryClient, normalized_idea


def ai_client():
    settings = get_settings()
    return OllamaClient(settings) if settings.ai_provider == "ollama" else AIClient(settings)


def discovery_client():
    return DiscoveryClient()


def permitted_feeds(account_id):
    return [feed for feed in get_settings().discovery_feeds
            if feed.ideas_use_permitted and account_id in feed.account_ids]


def run_discovery(db, job, account):
    feeds = permitted_feeds(account.id)
    if not feeds:
        raise ValueError("No operator-permitted feeds configured")
    completed = list(job.payload.get("_completed_feeds", []))
    discovered = list(job.payload.get("_candidate_ids", []))
    with discovery_client() as client:
        for feed in feeds:
            feed_key = hashlib.sha256((feed.name + "\n" + feed.url).encode()).hexdigest()
            if feed_key in completed:
                continue
            for idea in client.fetch(feed):
                existing = db.scalar(select(MemeCandidate.id).where(MemeCandidate.account_id == account.id,
                                                                     MemeCandidate.fingerprint == idea.fingerprint))
                if existing:
                    continue
                candidate = MemeCandidate(account_id=account.id, source_name=feed.name, source_url=idea.source_url,
                    source_id=idea.source_id, title=idea.title, idea=idea.title, fingerprint=idea.fingerprint,
                    rights_status="UNKNOWN", status="DISCOVERED",
                    discovery_metadata={"feed_url": feed.url, "permission": "operator_permitted_ideas_only",
                                        "media_reused": False})
                db.add(candidate)
                db.flush()
                discovered.append(str(candidate.id))
            completed.append(feed_key)
            job.payload = {**job.payload, "_completed_feeds": completed, "_candidate_ids": discovered}
            db.commit()
    return {"candidate_ids": discovered, "discovered": len(discovered), "feeds": len(completed)}


def strategy_data(account, strategy):
    return {"niche": account.niche, "language": account.language, "tone": strategy.tone,
            "audience": strategy.audience, "topic_allowlist": strategy.topic_allowlist,
            "topic_blocklist": strategy.topic_blocklist, "threshold": strategy.threshold,
            "prompt_version": get_settings().ai_prompt_version + ":" + strategy.prompt_version}


def strategy_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def topics_allowed(text, strategy):
    normalized = " " + normalized_idea(text) + " "
    matches = lambda topic: bool(normalized_idea(topic)) and " " + normalized_idea(topic) + " " in normalized
    return (not any(matches(topic) for topic in strategy.topic_blocklist)
            and (not strategy.topic_allowlist or any(matches(topic) for topic in strategy.topic_allowlist)))


def metered(db, job, candidate, operation, prompt_version):
    settings = get_settings()
    local = settings.ai_provider == "ollama"
    usage = AIUsage(account_id=job.account_id, job_id=job.id, candidate_id=candidate.id, operation=operation,
                    model=settings.ollama_model if local else settings.openai_moderation_model if operation.startswith("moderation") else settings.openai_model,
                    prompt_version=prompt_version,
                    estimated_cost_usd=0.0 if local else None,
                    pricing={"provider": settings.ai_provider, "input_usd_per_million": settings.ai_input_usd_per_million,
                             "cached_input_usd_per_million": settings.ai_cached_input_usd_per_million,
                             "output_usd_per_million": settings.ai_output_usd_per_million})
    db.add(usage)
    db.commit()

    def record(response, status):
        usage.status = status
        if response is not None:
            usage.response_id = getattr(response, "id", None)
            usage.model = getattr(response, "model", None) or usage.model
            tokens = getattr(response, "usage", None)
            if tokens is not None:
                input_tokens = getattr(tokens, "input_tokens", None)
                output_tokens = getattr(tokens, "output_tokens", None)
                details = getattr(tokens, "input_tokens_details", None)
                cached = getattr(details, "cached_tokens", 0) if details is not None else 0
                if (type(input_tokens) is int and input_tokens >= 0 and type(output_tokens) is int and output_tokens >= 0
                        and type(cached) is int and 0 <= cached <= input_tokens):
                    usage.input_tokens, usage.output_tokens, usage.cached_input_tokens = input_tokens, output_tokens, cached
                    p = usage.pricing
                    if (not local and p["input_usd_per_million"] is not None and p["output_usd_per_million"] is not None
                            and (not cached or p["cached_input_usd_per_million"] is not None)):
                        usage.estimated_cost_usd = ((input_tokens - cached) * p["input_usd_per_million"]
                            + cached * (p["cached_input_usd_per_million"] or 0)
                            + output_tokens * p["output_usd_per_million"]) / 1_000_000
        db.commit()
    return record


def reject(db, candidate, reason):
    candidate.status = "REJECTED"
    candidate.scores = {**candidate.scores, "rejection": reason}
    post = db.scalar(select(ContentPost).where(ContentPost.account_id == candidate.account_id,
                                              ContentPost.candidate_id == candidate.id))
    if post:
        post.status, post.error = "REJECTED", reason
    svc.audit(db, candidate.account_id, "generation_rejected", candidate.id, reason=reason)
    db.commit()
    return {"candidate_id": str(candidate.id), "rejected": reason}


def run_generation(db, job, account):
    selected = job.payload.get("candidate_id") or job.payload.get("_candidate_id")
    query = select(MemeCandidate).where(MemeCandidate.account_id == account.id)
    if selected:
        query = query.where(MemeCandidate.id == uuid.UUID(selected))
    else:
        query = query.where(MemeCandidate.status.in_(("DISCOVERED", "SCORED", "SELECTED", "GENERATED"))).order_by(MemeCandidate.created_at)
    candidate = db.scalar(query.limit(1).with_for_update())
    if candidate is None:
        return {"generated": 0}
    job.payload = {**job.payload, "_candidate_id": str(candidate.id)}
    db.commit()
    existing = db.scalar(select(ContentPost).where(ContentPost.account_id == account.id, ContentPost.candidate_id == candidate.id))
    if existing and existing.status in ("REVIEW", "APPROVED", "SCHEDULED", "PUBLISHED", "ANALYZED"):
        return {"generated": 1, "post_id": str(existing.id), "status": existing.status}
    if candidate.status in ("REJECTED", "FAILED"):
        return {"candidate_id": str(candidate.id), "rejected": candidate.status.lower()}
    metadata = candidate.discovery_metadata
    permitted = any(feed.name == candidate.source_name and feed.url == metadata.get("feed_url") for feed in permitted_feeds(account.id))
    if (not permitted or metadata.get("permission") != "operator_permitted_ideas_only" or metadata.get("media_reused") is not False
            or candidate.rights_status == "REJECTED"):
        return reject(db, candidate, "source_permission_required")
    strategy = db.get(AccountStrategy, account.id)
    if strategy is None or account.language != "id":
        return reject(db, candidate, "indonesian_strategy_required")
    data = strategy_data(account, strategy)
    signature = strategy_hash(data)
    if candidate.scores.get("strategy_hash") not in (None, signature):
        return reject(db, candidate, "strategy_changed")
    idea = candidate.idea
    if not topics_allowed(idea, strategy):
        return reject(db, candidate, "topic_policy")
    prompt_version = data["prompt_version"]
    try:
        with ai_client() as ai:
            if not candidate.scores.get("input_moderated"):
                ai.moderate(idea, metered(db, job, candidate, "moderation_input", prompt_version))
                candidate.scores = {**candidate.scores, "input_moderated": True, "strategy_hash": signature}
                db.commit()
            if "assessment" not in candidate.scores:
                score = ai.structured("score", {"idea": idea, "strategy": data}, metered(db, job, candidate, "score", prompt_version))
                candidate.scores = {**candidate.scores, "assessment": score.model_dump()}
                candidate.final_score, candidate.status = score.weighted_score, "SCORED"
                db.commit()
            score = IdeaScore.model_validate(candidate.scores["assessment"])
            if not (score.safe and score.rights_clear and score.topic_allowed) or score.weighted_score < strategy.threshold:
                return reject(db, candidate, "score_or_hard_safety_gate")
            candidate.status = "SELECTED"
            db.commit()
            if existing is None:
                meme = ai.structured("generate", {"idea": idea, "strategy": data}, metered(db, job, candidate, "generate", prompt_version))
                if not (meme.safe and meme.rights_clear) or not topics_allowed(" ".join((meme.concept, meme.top_text, meme.bottom_text, meme.publish_caption, meme.alt_text)), strategy):
                    return reject(db, candidate, "generated_safety_or_topic_policy")
                ai.moderate(json.dumps(meme.model_dump(), ensure_ascii=False), metered(db, job, candidate, "moderation_output", prompt_version))
                existing = ContentPost(account_id=account.id, candidate_id=candidate.id, concept=meme.concept,
                    template_type=meme.template_type, top_text=meme.top_text, bottom_text=meme.bottom_text,
                    joke=meme.top_text + "\n" + meme.bottom_text, caption=meme.publish_caption,
                    hashtags=meme.hashtags, alt_text=meme.alt_text, status="GENERATED",
                    generation_metadata={"output": meme.model_dump(), "prompt_version": prompt_version,
                         "strategy_hash": signature, "moderated": True, "moderation_provider": get_settings().ai_provider, "original_design": True,
                        "render_version": RENDER_VERSION, "source_media_reused": False})
                db.add(existing)
                candidate.status, candidate.rights_status = "GENERATED", "ORIGINAL"
                db.commit()
            else:
                if (existing.status != "GENERATED" or existing.generation_metadata.get("strategy_hash") != signature
                        or existing.generation_metadata.get("moderated") is not True
                        or existing.generation_metadata.get("render_version") != RENDER_VERSION):
                    return reject(db, candidate, "invalid_generation_checkpoint")
                meme = GeneratedMeme.model_validate(existing.generation_metadata["output"])
    except AIRejected:
        return reject(db, candidate, "moderation_flagged")
    try:
        jpeg = render_meme(meme)
    except RenderError:
        return reject(db, candidate, "render_overflow_or_unsupported_text")
    digest = hashlib.sha256(jpeg).hexdigest()
    if db.scalar(select(ContentPost.id).where(ContentPost.account_id == account.id,
                                             ContentPost.media_hash == digest, ContentPost.id != existing.id)):
        return reject(db, candidate, "duplicate_render")
    if existing.media_hash and existing.media_hash != digest:
        return reject(db, candidate, "render_changed")
    existing.media_hash = digest
    existing.media_key = f"{account.id}/{existing.id}/{digest}.jpg"
    db.commit()
    with svc.storage_client() as storage:
        storage.ensure_upload(existing.media_key, jpeg)
    # Recheck live account and policy after external effects, before approval.
    account = svc.account(db, account.id, lock=True)
    db.refresh(strategy)
    if strategy_hash(strategy_data(account, strategy)) != signature or not any(
            feed.name == candidate.source_name and feed.url == metadata.get("feed_url") for feed in permitted_feeds(account.id)):
        return reject(db, candidate, "policy_changed")
    existing.status = ("APPROVED" if get_settings().ai_provider != "ollama" and get_settings().enable_autoapproval and account.approval_mode == "AUTO"
                       and account.active else "REVIEW")
    candidate.status = existing.status
    svc.audit(db, account.id, "original_generated", existing.id, media_hash=digest, approval=existing.status)
    db.commit()
    return {"generated": 1, "post_id": str(existing.id), "status": existing.status}
