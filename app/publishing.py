"""Durable, fail-closed manual publishing. Never retry media_publish."""
import hashlib
import uuid
from contextlib import contextmanager
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, text, or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.models import ContentPost, DurableJob, MemeCandidate, PublishingJob
from app.security import decrypt_token
from app import services as svc
from integrations.meta import ContainerStatus


def gate(db, account, post):
    if not get_settings().enable_autopublish:
        raise HTTPException(409, "Publishing kill switch is off")
    if not account.active:
        raise HTTPException(409, "Account is paused")
    if not account.ig_user_id or not account.encrypted_token or not account.token_expires_at or svc.utc(account.token_expires_at) <= svc.now() + timedelta(minutes=2):
        raise HTTPException(409, "Account token unavailable or expiring")
    candidate = db.scalar(select(MemeCandidate).where(MemeCandidate.id == post.candidate_id,
                                                     MemeCandidate.account_id == account.id))
    if post.status != "APPROVED" or not candidate or candidate.rights_status != "ORIGINAL" or not post.media_key or not post.media_hash:
        raise HTTPException(409, "Approved original media required")
    uncertain = db.scalar(select(PublishingJob.id).join(ContentPost, PublishingJob.post_id == ContentPost.id).where(
        ContentPost.account_id == account.id, ContentPost.id != post.id, PublishingJob.status == "UNCERTAIN"))
    if uncertain:
        raise HTTPException(409, "Resolve uncertain account publication first")


def quota(meta, account, token):
    limit = meta.publishing_limit(account.ig_user_id, token)
    if limit["quota_usage"] >= min(limit["config"]["quota_total"], get_settings().publishing_quota_cap):
        raise HTTPException(409, "Account publishing quota reached")


def dispatch(job_id) -> bool:
    from app.worker import publish_task
    try:
        publish_task.apply_async(args=[str(job_id)], retry=False)
        return True
    except Exception:
        # The committed DB row, not broker delivery, is the source of truth.
        return False


@contextmanager
def account_session(account_id):
    # Session-level PostgreSQL lock survives the mandatory pre-publish commit.
    # Row locks alone cannot serialize this boundary. Connection remains pinned.
    key = int.from_bytes(hashlib.sha256(account_id.bytes).digest()[:8], "big", signed=True)
    with get_engine().connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        locked = False
        try:
            if postgres:
                locked = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
                connection.commit()
                if not locked:
                    yield None
                    return
            with Session(connection, expire_on_commit=False) as db:
                yield db
        finally:
            if postgres and locked:
                try:
                    connection.rollback()
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    connection.commit()
                except Exception:
                    connection.invalidate()


def complete(db, job, publication, post, media_id):
    post.status = "PUBLISHED"
    post.instagram_media_id = media_id
    post.published_at = post.published_at or svc.now()
    post.error = None
    publication.status, publication.error = "SUCCEEDED", None
    publication.next_attempt_at = None
    job.status, job.error, job.finished_at = "SUCCEEDED", None, svc.now()
    job.result = {"post_id": str(post.id), "instagram_media_id": media_id}
    svc.audit(db, post.account_id, "published", post.id, instagram_media_id=media_id)
    db.commit()


def run_publish(job_id):
    job_id = uuid.UUID(str(job_id))
    with Session(get_engine()) as lookup:
        record = lookup.get(DurableJob, job_id)
        if record is None or record.kind != "publish":
            return
        account_id = record.account_id
    with account_session(account_id) as db:
        if db is None:
            return
        account = svc.account(db, account_id, lock=True)
        job = db.scalar(select(DurableJob).where(DurableJob.id == job_id).with_for_update())
        post = svc.post(db, uuid.UUID(job.payload["post_id"]), account_id, lock=True)
        publication = db.scalar(select(PublishingJob).where(PublishingJob.post_id == post.id).with_for_update())
        if job.status in ("SUCCEEDED", "FAILED") or publication.status in ("UNCERTAIN", "SUCCEEDED", "FAILED"):
            return
        if publication.next_attempt_at and svc.utc(publication.next_attempt_at) > svc.now():
            return
        try:
            gate(db, account, post)
        except HTTPException:
            publication.error = "safety_gate_blocked"
            publication.next_attempt_at = svc.now() + timedelta(minutes=1)
            db.commit()
            return
        if publication.attempts >= 5:
            publication.status, publication.error = "FAILED", "poll_budget_exhausted"
            job.status, job.error, job.finished_at = "FAILED", "poll_budget_exhausted", svc.now()
            db.commit()
            return
        publication.attempts += 1
        job.attempts += 1
        job.status = "RUNNING"
        publication.status = "PROCESSING"
        publication.next_attempt_at = svc.now() + timedelta(minutes=1)
        # Persist attempt budget before HTTP so worker crashes cannot reset it.
        db.commit()
        try:
            account = svc.account(db, account_id, lock=True)
            post = svc.post(db, post.id, account_id, lock=True)
            gate(db, account, post)
            token = decrypt_token(account.encrypted_token)
            with svc.meta_client() as meta:
                quota(meta, account, token)
                if not publication.container_id:
                    with svc.storage_client() as storage:
                        url = storage.signed_url(post.media_key)
                    publication.container_id = meta.create_jpeg_container(account.ig_user_id, token, url, post.caption or "")
                    publication.error = None
                    db.commit()
                    return
                status = meta.get_status(publication.container_id, token)
                if status == ContainerStatus.PUBLISHED:
                    publication.status, publication.error = "UNCERTAIN", "published_missing_media_id"
                    post.status, post.published_at = "PUBLISHED", svc.now()
                    publication.next_attempt_at = None
                    db.commit()
                    return
                if status in (ContainerStatus.ERROR, ContainerStatus.EXPIRED):
                    publication.status, publication.error = "FAILED", "container_failed"
                    job.status, job.error, job.finished_at = "FAILED", "container_failed", svc.now()
                    db.commit()
                    return
                if status != ContainerStatus.FINISHED:
                    db.commit()
                    return
                publication.status, publication.error = "UNCERTAIN", "publish_outcome_unconfirmed"
                publication.next_attempt_at = None
                db.commit()
                # Recheck pause/token/rights after commit; advisory lock still held.
                db.expire_all()
                account = svc.account(db, account_id, lock=True)
                post = svc.post(db, post.id, account_id, lock=True)
                gate(db, account, post)
                token = decrypt_token(account.encrypted_token)
                quota(meta, account, token)
                media_id = meta.publish(account.ig_user_id, token, publication.container_id)
                complete(db, job, publication, post, media_id)
        except Exception:
            # Even known HTTP failures after the boundary remain uncertain.
            # Never retain exception strings, URLs, payloads or token material.
            db.rollback()
            publication = db.scalar(select(PublishingJob).where(PublishingJob.post_id == post.id).with_for_update())
            if publication.status in ("SUCCEEDED", "FAILED"):
                return
            publication.error = "publish_outcome_unconfirmed" if publication.status == "UNCERTAIN" else "upstream_unavailable"
            db.commit()


def recover_pending():
    with Session(get_engine()) as db:
        jobs = db.scalars(select(DurableJob).where(DurableJob.kind == "publish",
                                                  DurableJob.status.in_(("PENDING", "RUNNING"))))
        ids = []
        for job in jobs:
            publication = db.scalar(select(PublishingJob).where(PublishingJob.post_id == uuid.UUID(job.payload["post_id"]),
                PublishingJob.status.in_(("PENDING", "PROCESSING")),
                or_(PublishingJob.next_attempt_at.is_(None), PublishingJob.next_attempt_at <= svc.now())))
            if publication is not None:
                ids.append(job.id)
    return sum(dispatch(id) for id in ids)


def reconcile(db, account_id, post_id, verified_media_id=None, operator_verified=False):
    account = svc.account(db, account_id, lock=True)
    post = svc.post(db, post_id, account_id, lock=True)
    publication = db.scalar(select(PublishingJob).where(PublishingJob.post_id == post_id).with_for_update())
    if not publication or publication.status != "UNCERTAIN" or not publication.container_id:
        raise HTTPException(409, "Only uncertain containers can be reconciled")
    if not account.encrypted_token or not account.token_expires_at or svc.utc(account.token_expires_at) <= svc.now():
        raise HTTPException(409, "Valid token required for reconciliation")
    if publication.next_attempt_at and svc.utc(publication.next_attempt_at) > svc.now():
        raise HTTPException(409, "Wait until next_attempt_at before reconciling")
    with svc.meta_client() as meta:
        status = meta.get_status(publication.container_id, decrypt_token(account.encrypted_token))
    publication.next_attempt_at = svc.now() + timedelta(minutes=1)
    if status == ContainerStatus.PUBLISHED:
        post.status, post.published_at = "PUBLISHED", post.published_at or svc.now()
        publication.error = "published_missing_media_id"
        if verified_media_id is not None:
            if not operator_verified:
                raise HTTPException(422, "Explicit operator verification of media/account/container required")
            job = db.scalar(select(DurableJob).where(DurableJob.account_id == account_id, DurableJob.kind == "publish",
                                                     DurableJob.idempotency_key == str(post_id)).with_for_update())
            svc.audit(db, account_id, "manual_media_identity_verified", post_id, instagram_media_id=verified_media_id)
            complete(db, job, publication, post, verified_media_id)
    elif verified_media_id is not None:
        raise HTTPException(409, "Container is not confirmed published")
    svc.audit(db, account_id, "publication_reconciled", post_id, container_status=status.value)
    db.commit()
    return {"post_id": post_id, "publishing_status": publication.status, "container_status": status.value,
            "instagram_media_id": post.instagram_media_id, "error": publication.error}
