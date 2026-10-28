"""Database outbox, account serialization and bounded generic job recovery."""
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import services as svc
from app.config import get_settings
from app.db import get_engine
from app.models import DurableJob, InstagramAccount
from app.publishing import account_session


GENERIC_KINDS = ("discovery", "generation", "schedule", "publish-due", "analytics", "token-maintenance", "weekly-report")


def enqueue_job(db, account_id, kind, idempotency_key, payload=None):
    """Commit before dispatch. Reusing a key with different input is a conflict."""
    svc.account(db, account_id, lock=True)
    payload = payload or {}
    query = select(DurableJob).where(DurableJob.account_id == account_id, DurableJob.kind == kind,
                                    DurableJob.idempotency_key == idempotency_key)
    job = db.scalar(query)
    if job is None:
        try:
            with db.begin_nested():
                job = DurableJob(account_id=account_id, kind=kind, idempotency_key=idempotency_key, payload=payload)
                db.add(job)
                db.flush()
        except IntegrityError:
            job = db.scalar(query)
            if job is None:
                raise
        svc.audit(db, account_id, kind + "_requested", job.id)
        db.commit()
    # Internal checkpoint keys start with underscore and are not request input.
    original = {key: value for key, value in job.payload.items() if not key.startswith("_")}
    if original != payload:
        raise HTTPException(409, "Idempotency key already used with different input")
    return job


def dispatch_job(job_id):
    from app.worker import generic_task
    try:
        generic_task.apply_async(args=[str(job_id)], retry=False)
        return True
    except Exception:
        return False


def job_handlers():
    from app.pipeline import run_discovery, run_generation
    from app.scheduling import schedule, publish_due
    from app.analytics import collect
    from app.maintenance import refresh_account, weekly_report
    return {"discovery": run_discovery, "generation": run_generation,
            "schedule": lambda db, job, account: schedule(account.id, db=db),
            "publish-due": lambda db, job, account: publish_due(account.id, db=db),
            "analytics": lambda db, job, account: collect(account.id, db=db),
            "token-maintenance": refresh_account, "weekly-report": weekly_report}


def run_job(job_id, handlers=None):
    job_id = uuid.UUID(str(job_id))
    handlers = job_handlers() if handlers is None else handlers
    with Session(get_engine()) as lookup:
        record = lookup.get(DurableJob, job_id)
        if record is None or record.kind not in handlers:
            return
        account_id = record.account_id
    # Same lock namespace as publishing, across every checkpoint commit.
    with account_session(account_id) as db:
        if db is None:
            return
        job = db.scalar(select(DurableJob).where(DurableJob.id == job_id).with_for_update())
        now = svc.now()
        if (job.status in ("SUCCEEDED", "FAILED") or
                (job.next_attempt_at and svc.utc(job.next_attempt_at) > now) or
                (job.status == "RUNNING" and job.lease_until and svc.utc(job.lease_until) > now)):
            return
        account = svc.account(db, account_id, lock=True)
        if not account.active:
            job.next_attempt_at = now + timedelta(minutes=1)
            db.commit()
            return
        if job.attempts >= get_settings().generic_job_max_attempts:
            job.status, job.error, job.finished_at = "FAILED", "attempt_budget_exhausted", now
            job.lease_until, job.lease_owner = None, None
            db.commit()
            return
        job.attempts += 1
        job.status, job.error = "RUNNING", None
        job.lease_owner, job.lease_until = uuid.uuid4(), now + timedelta(minutes=10)
        job.next_attempt_at = None
        db.commit()
        try:
            job.result = handlers[job.kind](db, job, account)
            job.status, job.finished_at, job.error = "SUCCEEDED", svc.now(), None
            job.lease_until, job.lease_owner, job.next_attempt_at = None, None, None
            db.commit()
        except Exception:
            db.rollback()
            job = db.get(DurableJob, job_id)
            job.error = "pipeline_unavailable"
            exhausted = job.attempts >= get_settings().generic_job_max_attempts
            job.status = "FAILED" if exhausted else "PENDING"
            job.finished_at = svc.now() if exhausted else None
            job.next_attempt_at = None if exhausted else svc.now() + timedelta(minutes=1)
            job.lease_until, job.lease_owner = None, None
            db.commit()


def recover_jobs(limit=100):
    with Session(get_engine()) as db:
        now = svc.now()
        ids = list(db.scalars(select(DurableJob.id).join(InstagramAccount).where(
            InstagramAccount.active.is_(True),
            DurableJob.kind.in_(GENERIC_KINDS), DurableJob.status.in_(("PENDING", "RUNNING")),
            or_(DurableJob.next_attempt_at.is_(None), DurableJob.next_attempt_at <= now),
            or_(DurableJob.status == "PENDING", DurableJob.lease_until.is_(None), DurableJob.lease_until <= now),
        ).order_by(DurableJob.created_at).limit(limit)))
    return sum(dispatch_job(id) for id in ids)
