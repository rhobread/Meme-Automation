"""Hourly, persisted housekeeping; never downloads models or invokes paid APIs."""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services as svc
from app.db import get_engine
from app.models import InstagramAccount
from app.security import decrypt_token, encrypt_token


def refresh_account(db, job, account):
    now = svc.now()
    if not account.token_expires_at or not account.encrypted_token or svc.utc(account.token_expires_at) <= now:
        return {"refreshed": False, "reauthorization_required": True}
    if (svc.utc(account.token_expires_at) > now + timedelta(days=7) or not account.token_acquired_at
            or svc.utc(account.token_acquired_at) > now - timedelta(hours=24)):
        return {"refreshed": False}
    with svc.meta_client() as meta:
        token = meta.refresh(decrypt_token(account.encrypted_token))
        profile = meta.get_profile(token.access_token)
    if profile.user_id != account.ig_user_id:
        raise ValueError("Token identity mismatch")
    account.encrypted_token = encrypt_token(token.access_token)
    account.token_acquired_at = now
    account.token_expires_at = now + timedelta(seconds=token.expires_in)
    svc.audit(db, account.id, "token_automatically_refreshed", account.id)
    db.commit()
    return {"refreshed": True}


def weekly_report(db, job, account):
    from app.analytics import summary
    report = summary(db, account.id, days=14)
    report["generated_at"] = svc.now().isoformat()
    report["report_version"] = "age-cohorts-v1"
    svc.audit(db, account.id, "weekly_report_generated", job.id)
    return report


def enqueue_housekeeping():
    from app.jobs import enqueue_job, dispatch_job
    now = svc.now()
    ids = []
    with Session(get_engine(), expire_on_commit=False) as db:
        accounts = db.scalars(select(InstagramAccount).where(InstagramAccount.active.is_(True))).all()
        for account in accounts:
            # Separate account jobs allow one expired token to fail independently.
            kinds = ["weekly-report"]
            if account.encrypted_token and account.token_expires_at and svc.utc(account.token_expires_at) > now:
                kinds.extend(("analytics", "token-maintenance"))
            for kind in kinds:
                key = "weekly:" + now.strftime("%G-W%V") if kind == "weekly-report" else "hourly:" + now.strftime("%Y%m%dT%H")
                ids.append(enqueue_job(db, account.id, kind, key).id)
    return sum(dispatch_job(id) for id in ids)
