"""Age-bucketed, missingness-preserving Instagram insight snapshots."""
from datetime import timedelta
from contextlib import nullcontext

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import services as svc
from app.models import AIUsage, ContentPost, DurableJob, InstagramAccount, MemeCandidate, PostMetric, PublishingJob
from app.publishing import account_session
from app.security import decrypt_token


AGES = (24, 72, 168)
METRICS = {"views": "views", "reach": "reach", "likes": "likes", "comments": "comments",
           "shares": "shares", "saves": "saved"}


def age_bucket(published_at, at):
    elapsed = (svc.utc(at) - svc.utc(published_at)).total_seconds() / 3600
    return next((age for age in reversed(AGES) if elapsed >= age), None)


def rates(metrics):
    """A missing numerator is unknown, never implicitly zero."""
    reach = metrics.get("reach")
    result = {"share_rate": None, "save_rate": None, "engagement_rate": None}
    if reach is None or reach <= 0:
        return result
    if metrics.get("shares") is not None:
        result["share_rate"] = metrics["shares"] / reach
    if metrics.get("saves") is not None:
        result["save_rate"] = metrics["saves"] / reach
    values = [metrics.get(key) for key in ("likes", "comments", "shares", "saves")]
    if all(value is not None for value in values):
        result["engagement_rate"] = sum(values) / reach
    return result


def collect(account_id, db=None):
    with (nullcontext(db) if db is not None else account_session(account_id)) as db:
        if db is None:
            raise HTTPException(409, "Account operation already running")
        account = svc.account(db, account_id)
        if not account.active:
            raise HTTPException(409, "Account is paused")
        if not account.encrypted_token or not account.token_expires_at or svc.utc(account.token_expires_at) <= svc.now():
            raise HTTPException(409, "Valid token required for insights")
        at = svc.now()
        posts = db.scalars(select(ContentPost).where(
            ContentPost.account_id == account_id, ContentPost.status.in_(("PUBLISHED", "ANALYZED")),
            ContentPost.instagram_media_id.is_not(None), ContentPost.published_at <= at - timedelta(hours=24),
        ).order_by(ContentPost.published_at)).all()
        collected = 0
        with svc.meta_client() as meta:
            for post in posts:
                age = age_bucket(post.published_at, at)
                if age is None:
                    continue
                # The natural key is the target age. The actual observation time is
                # recorded separately; missed historical windows are never invented.
                target = svc.utc(post.published_at) + timedelta(hours=age)
                if db.scalar(select(PostMetric.id).where(PostMetric.post_id == post.id, PostMetric.collected_at == target)):
                    continue
                values = meta.media_insights(post.instagram_media_id, decrypt_token(account.encrypted_token))
                mapped = {key: values.get(remote) for key, remote in METRICS.items()}
                if any(value is not None and (type(value) is not int or value < 0) for value in mapped.values()):
                    raise ValueError("Invalid count metrics")
                db.add(PostMetric(post_id=post.id, collected_at=target, **mapped,
                                  raw={"metrics": values, "age_hours": age, "observed_at": svc.now().isoformat(),
                                       "actual_age_hours": (svc.now() - svc.utc(post.published_at)).total_seconds() / 3600}))
                if age == 168:
                    post.status = "ANALYZED"
                    db.get(MemeCandidate, post.candidate_id).status = "ANALYZED"
                svc.audit(db, account_id, "insights_collected", post.id, age_hours=age)
                db.commit()
                collected += 1
                if collected >= 10:
                    break
        return {"collected": collected}


def summary(db: Session, account_id, days=14):
    svc.account(db, account_id)
    rows = db.execute(select(PostMetric, ContentPost, MemeCandidate).join(
        ContentPost, PostMetric.post_id == ContentPost.id).join(
        MemeCandidate, ContentPost.candidate_id == MemeCandidate.id).where(
        ContentPost.account_id == account_id, ContentPost.published_at >= svc.now() - timedelta(days=days),
    ).order_by(ContentPost.published_at.desc())).all()
    cohorts = []
    for age in AGES:
        group = [(metric, post, candidate) for metric, post, candidate in rows if metric.raw.get("age_hours") == age]
        # Restrict comparisons to observations within 12h of the age target.
        comparable = [row for row in group if age <= row[0].raw.get("actual_age_hours", float("inf")) < age + 12]
        totals = {}
        for key in METRICS:
            values = [getattr(row[0], key) for row in comparable]
            totals[key] = sum(values) if values and all(value is not None for value in values) else None
        cohorts.append({"age_hours": age, "snapshots": len(group), "comparable_snapshots": len(comparable),
                        "totals": totals, "rates": rates(totals), "small_sample": len(comparable) < 20})
    usage = db.scalars(select(AIUsage).where(AIUsage.account_id == account_id,
                                           AIUsage.created_at >= svc.now() - timedelta(days=days))).all()
    costs = [row.estimated_cost_usd for row in usage]
    return {"account_id": str(account_id), "window_days": days, "cohorts": cohorts,
            "ai_usage": {"requests": len(usage), "known_api_cost_usd": sum(value for value in costs if value is not None),
                         "unknown_cost_requests": sum(value is None for value in costs),
                         "note": "Local API cost excludes hardware, electricity and model downloads."},
            "guidance": "Compare only same-age cohorts. Fewer than 20 posts is exploratory; do not auto-change strategy.",
            "experiments": ["Test one topic or posting slot at a time while holding other factors fixed.",
                            "Keep manual approval until rights, humor and audience fit have been reviewed."]}


def monitoring(db: Session):
    at = svc.now()
    def count(model, *conditions):
        return db.scalar(select(func.count()).select_from(model).where(*conditions))
    failed = count(DurableJob, DurableJob.status == "FAILED", DurableJob.created_at >= at - timedelta(days=7))
    stale = db.scalar(select(func.count()).select_from(DurableJob).join(InstagramAccount).where(
        InstagramAccount.active.is_(True), DurableJob.status.in_(("PENDING", "RUNNING")),
        DurableJob.created_at < at - timedelta(minutes=30)))
    uncertain = count(PublishingJob, PublishingJob.status == "UNCERTAIN")
    expiring = count(InstagramAccount, InstagramAccount.active.is_(True), InstagramAccount.token_expires_at <= at + timedelta(days=7))
    unauthorized = count(InstagramAccount, InstagramAccount.active.is_(True), InstagramAccount.encrypted_token.is_(None))
    return {"healthy": not any((failed, stale, uncertain, expiring, unauthorized)), "failed_jobs": failed,
            "stale_jobs": stale, "uncertain_publications": uncertain, "expiring_tokens": expiring,
            "unauthorized_accounts": unauthorized, "checked_at": at.isoformat()}
