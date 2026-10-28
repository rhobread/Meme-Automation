import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, JSON, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


LIFECYCLE = "'DISCOVERED','SCORED','SELECTED','GENERATED','REVIEW','APPROVED','SCHEDULED','PUBLISHED','ANALYZED','FAILED','REJECTED'"


class InstagramAccount(Identity, Base):
    __tablename__ = "instagram_accounts"
    ig_user_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    username: Mapped[str] = mapped_column(String(100))
    niche: Mapped[str] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(20), default="id")
    timezone: Mapped[str] = mapped_column(String(100), default="Asia/Jakarta")
    posts_per_day: Mapped[int] = mapped_column(default=2)
    approval_mode: Mapped[str] = mapped_column(String(20), default="MANUAL")
    encrypted_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (CheckConstraint("posts_per_day >= 0 AND posts_per_day <= 25"), CheckConstraint("approval_mode IN ('MANUAL','AUTO')"))


class MemeCandidate(Identity, Base):
    __tablename__ = "meme_candidates"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id"), index=True)
    source_name: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None]
    source_id: Mapped[str | None]
    title: Mapped[str]
    idea: Mapped[str]
    rights_status: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    final_score: Mapped[float | None]
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="DISCOVERED")
    __table_args__ = (UniqueConstraint("account_id", "fingerprint"), UniqueConstraint("account_id", "id"), CheckConstraint(f"status IN ({LIFECYCLE})"), CheckConstraint("rights_status IN ('UNKNOWN','ORIGINAL','LICENSED','REJECTED')"), CheckConstraint("final_score >= 0 AND final_score <= 100"))


class ContentPost(Identity, Base):
    __tablename__ = "content_posts"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id"), index=True)
    candidate_id: Mapped[uuid.UUID]
    content_type: Mapped[str] = mapped_column(String(20), default="IMAGE")
    joke: Mapped[str | None]
    caption: Mapped[str | None]
    hashtags: Mapped[list[str]] = mapped_column(JSON, default=list)
    media_key: Mapped[str | None]
    media_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="REVIEW")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    instagram_media_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    error: Mapped[str | None]
    __table_args__ = (ForeignKeyConstraint(["account_id", "candidate_id"], ["meme_candidates.account_id", "meme_candidates.id"]), UniqueConstraint("account_id", "candidate_id"), UniqueConstraint("account_id", "id"), UniqueConstraint("account_id", "scheduled_at"), CheckConstraint(f"status IN ({LIFECYCLE})"), CheckConstraint("content_type = 'IMAGE'"), Index("ix_posts_due", "status", "scheduled_at"))


class PublishingJob(Identity, Base):
    __tablename__ = "publishing_jobs"
    post_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content_posts.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    container_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[uuid.UUID | None]
    error: Mapped[str | None]
    __table_args__ = (CheckConstraint("status IN ('PENDING','PROCESSING','UNCERTAIN','SUCCEEDED','FAILED')"), CheckConstraint("attempts >= 0"), Index("ix_publish_due", "status", "next_attempt_at"))


class PostMetric(Identity, Base):
    __tablename__ = "post_metrics"
    post_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("content_posts.id"))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    views: Mapped[int | None]
    reach: Mapped[int | None]
    likes: Mapped[int | None]
    comments: Mapped[int | None]
    shares: Mapped[int | None]
    saves: Mapped[int | None]
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("post_id", "collected_at"), CheckConstraint("views >= 0 AND reach >= 0 AND likes >= 0 AND comments >= 0 AND shares >= 0 AND saves >= 0"))


class AccountStrategy(Base):
    __tablename__ = "account_strategies"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id"), primary_key=True)
    tone: Mapped[str]
    audience: Mapped[str]
    topic_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    topic_blocklist: Mapped[list[str]] = mapped_column(JSON, default=list)
    posting_times: Mapped[list[str]] = mapped_column(JSON, default=list)
    threshold: Mapped[float] = mapped_column(default=75)
    prompt_version: Mapped[str] = mapped_column(String(100))
    __table_args__ = (CheckConstraint("threshold >= 0 AND threshold <= 100"),)


class DurableJob(Identity, Base):
    __tablename__ = "durable_jobs"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id"))
    kind: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[uuid.UUID | None]
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None]
    __table_args__ = (UniqueConstraint("account_id", "kind", "idempotency_key"), CheckConstraint("attempts >= 0"), CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')"), Index("ix_jobs_pending", "status", "created_at"))


class AuditEvent(Identity, Base):
    __tablename__ = "audit_events"
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("instagram_accounts.id"), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[uuid.UUID | None]
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OAuthState(Identity, Base):
    __tablename__ = "oauth_states"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id"))
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
