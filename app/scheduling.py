"""Account-isolated, deterministic local-time scheduling and due-post outbox."""
import re
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import or_, select

from app import publishing
from app import services as svc
from app.models import AccountStrategy, ContentPost, DurableJob, MemeCandidate, PublishingJob


DEFAULT_SLOTS = {"office": ["12:00", "19:00"], "work": ["12:00", "19:00"],
                 "gaming": ["13:00", "21:00"], "programming": ["11:30", "20:00"],
                 "it": ["11:30", "20:00"]}


def posting_times(account, strategy):
    times = strategy.posting_times if strategy and strategy.posting_times else DEFAULT_SLOTS.get(account.niche.lower(), ["12:00", "19:00"])
    if any(not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in times):
        raise HTTPException(409, "Posting times must be HH:MM")
    times = sorted(set(times))
    if account.posts_per_day > len(times):
        raise HTTPException(409, "Configure enough distinct posting times for the daily target")
    return times[:account.posts_per_day]


def schedule(account_id, db=None):
    with (nullcontext(db) if db is not None else publishing.account_session(account_id)) as db:
        if db is None:
            raise HTTPException(409, "Account operation already running")
        account = svc.account(db, account_id, lock=True)
        if not account.active:
            return {"scheduled": 0, "paused": True}
        strategy = db.get(AccountStrategy, account_id)
        times = posting_times(account, strategy)
        if not times:
            return {"scheduled": 0}
        zone = ZoneInfo(account.timezone)
        now = svc.now()
        today = now.astimezone(zone).date()
        existing = db.scalars(select(ContentPost).where(
            ContentPost.account_id == account_id,
            or_(ContentPost.scheduled_at >= now - timedelta(days=2), ContentPost.published_at >= now - timedelta(days=2)),
        )).all()
        occupied = {svc.utc(row.scheduled_at) for row in existing if row.scheduled_at}
        daily = {}
        for row in existing:
            timestamp = row.scheduled_at or row.published_at
            day = svc.utc(timestamp).astimezone(zone).date()
            daily[day] = daily.get(day, 0) + 1
        queue = db.scalars(select(ContentPost).join(MemeCandidate, ContentPost.candidate_id == MemeCandidate.id).where(
            ContentPost.account_id == account_id, ContentPost.status == "APPROVED",
            ContentPost.scheduled_at.is_(None), ContentPost.media_key.is_not(None), ContentPost.media_hash.is_not(None),
            MemeCandidate.account_id == account_id, MemeCandidate.rights_status == "ORIGINAL",
        ).order_by(ContentPost.created_at, ContentPost.id).with_for_update()).all()
        assigned = 0
        for offset in range(2):
            date = today + timedelta(days=offset)
            for value in times:
                hour, minute = map(int, value.split(":"))
                local = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone)
                slot = local.astimezone(timezone.utc)
                # Skip nonexistent local wall times during DST transitions.
                if slot.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
                    continue
                if slot <= now or slot in occupied or assigned >= len(queue) or daily.get(date, 0) >= account.posts_per_day:
                    continue
                post = queue[assigned]
                post.status, post.scheduled_at = "SCHEDULED", slot
                db.get(MemeCandidate, post.candidate_id).status = "SCHEDULED"
                occupied.add(slot)
                daily[date] = daily.get(date, 0) + 1
                svc.audit(db, account_id, "post_scheduled", post.id, scheduled_at=slot.isoformat())
                assigned += 1
        db.commit()
        return {"scheduled": assigned, "timezone": account.timezone, "horizon_days": 2}


def publish_due(account_id, db=None):
    ids = []
    with (nullcontext(db) if db is not None else publishing.account_session(account_id)) as db:
        if db is None:
            raise HTTPException(409, "Account operation already running")
        account = svc.account(db, account_id, lock=True)
        if not account.active:
            return {"accepted": 0, "paused": True}
        posts = db.scalars(select(ContentPost).where(
            ContentPost.account_id == account_id, ContentPost.status == "SCHEDULED",
            ContentPost.scheduled_at <= svc.now(),
        ).order_by(ContentPost.scheduled_at).with_for_update()).all()
        for post in posts:
            # A stale backlog must not cause a catch-up publication burst. Content
            # returns to manual review before it can be scheduled again.
            if svc.utc(post.scheduled_at) < svc.now() - timedelta(hours=1):
                post.status, post.error = "REVIEW", "missed_schedule_requires_review"
                post.scheduled_at = None
                db.get(MemeCandidate, post.candidate_id).status = "REVIEW"
                svc.audit(db, account_id, "schedule_missed", post.id)
                continue
            publishing.gate(db, account, post)
            job = db.scalar(select(DurableJob).where(DurableJob.account_id == account_id,
                DurableJob.kind == "publish", DurableJob.idempotency_key == str(post.id)))
            if job is None:
                job = DurableJob(account_id=account_id, kind="publish", idempotency_key=str(post.id),
                                 payload={"post_id": str(post.id)})
                db.add(job)
                db.add(PublishingJob(post_id=post.id, next_attempt_at=svc.now()))
                db.flush()
                svc.audit(db, account_id, "scheduled_publish_requested", post.id)
            ids.append(job.id)
        db.commit()
    dispatched = sum(publishing.dispatch(id) for id in ids)
    return {"accepted": len(ids), "dispatched": dispatched}
