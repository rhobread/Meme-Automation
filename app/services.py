from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.models import InstagramAccount, ContentPost, AuditEvent
from integrations.meta import MetaClient
from integrations.storage import StorageClient
from integrations.local_storage import LocalStorageClient


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def session():
    with Session(get_engine(), expire_on_commit=False) as db:
        yield db


def meta_client():
    s = get_settings()
    if not s.meta_app_id or not s.meta_app_secret.get_secret_value() or not s.meta_redirect_uri.startswith("https://"):
        raise HTTPException(503, "Meta configuration required")
    return MetaClient(s.meta_app_id, s.meta_app_secret.get_secret_value(), s.meta_redirect_uri, s.meta_api_version)


def storage_client():
    s = get_settings()
    if s.storage_provider == "local":
        return LocalStorageClient(s.local_media_dir, s.token_encryption_key.get_secret_value(),
                                  s.media_public_base_url, s.media_url_ttl_seconds)
    return StorageClient(s.supabase_url, s.supabase_service_role_key.get_secret_value(), s.supabase_bucket)


def account(db, account_id, lock=False):
    query = select(InstagramAccount).where(InstagramAccount.id == account_id).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "Account not found")
    return row


def post(db, post_id, account_id, lock=False):
    query = select(ContentPost).where(ContentPost.id == post_id, ContentPost.account_id == account_id).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "Content not found")
    return row


def audit(db, account_id, action, entity_id, **details):
    db.add(AuditEvent(account_id=account_id, actor="operator", action=action, entity_id=entity_id, details=details))
