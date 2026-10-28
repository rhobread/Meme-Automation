import hashlib
import io
import secrets
import uuid
from datetime import timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from PIL import Image, ImageCms, ImageOps
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import authenticate
from app.models import AccountStrategy, ContentPost, DurableJob, InstagramAccount, MemeCandidate, OAuthState, PublishingJob
from app.security import decrypt_token, encrypt_token
from app import services as svc

router = APIRouter(prefix="/api/v1", dependencies=[Depends(authenticate)])
oauth_router = APIRouter(prefix="/api/v1/oauth/instagram")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(Input):
    account_id: uuid.UUID | None = None


class Strategy(Input):
    tone: str = Field(default="", max_length=1000)
    audience: str = Field(default="", max_length=1000)
    topic_allowlist: list[str] = Field(default_factory=list, max_length=100)
    topic_blocklist: list[str] = Field(default_factory=list, max_length=100)
    posting_times: list[str] = Field(default_factory=list, max_length=24)
    threshold: float = Field(default=75, ge=0, le=100)
    prompt_version: str = Field(default="manual-v1", max_length=100)


class AccountCreate(Input):
    username: str = Field(min_length=1, max_length=100)
    niche: str = Field(min_length=1, max_length=100)
    language: str = Field(default="id", min_length=1, max_length=20)
    timezone: str = "Asia/Jakarta"
    posts_per_day: int = Field(default=2, ge=0, le=25)
    active: bool = False
    approval_mode: Literal["MANUAL", "AUTO"] = "MANUAL"
    strategy: Strategy = Field(default_factory=Strategy)

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid timezone") from None
        return value


class AccountPatch(Input):
    account_id: uuid.UUID | None = None
    niche: str | None = Field(default=None, min_length=1, max_length=100)
    language: str | None = Field(default=None, min_length=1, max_length=20)
    timezone: str | None = None
    posts_per_day: int | None = Field(default=None, ge=0, le=25)
    active: bool | None = None
    approval_mode: Literal["MANUAL", "AUTO"] | None = None
    strategy: Strategy | None = None

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        return AccountCreate.timezone_valid(value) if value is not None else value


def check_scope(query_id, body_id):
    if body_id is not None and query_id != body_id:
        raise HTTPException(409, "Account scope mismatch")


def account_view(db, row):
    strategy = db.get(AccountStrategy, row.id)
    return {"id": row.id, "username": row.username, "ig_user_id": row.ig_user_id,
            "niche": row.niche, "language": row.language, "timezone": row.timezone,
            "posts_per_day": row.posts_per_day, "active": row.active, "approval_mode": row.approval_mode,
            "authorized": row.encrypted_token is not None, "token_expires_at": row.token_expires_at,
            "strategy": {name: getattr(strategy, name) for name in Strategy.model_fields} if strategy else None}


@router.get("/accounts")
def accounts(db: Session = Depends(svc.session)):
    return [account_view(db, row) for row in db.scalars(select(InstagramAccount).order_by(InstagramAccount.created_at))]


@router.post("/accounts", status_code=201)
def create_account(body: AccountCreate, db: Session = Depends(svc.session)):
    row = InstagramAccount(**body.model_dump(exclude={"strategy"}))
    db.add(row)
    db.flush()
    db.add(AccountStrategy(account_id=row.id, **body.strategy.model_dump()))
    svc.audit(db, row.id, "account_created", row.id)
    db.commit()
    return account_view(db, row)


@router.patch("/accounts/{id}")
def patch_account(id: uuid.UUID, body: AccountPatch, account_id: uuid.UUID | None = None,
                  db: Session = Depends(svc.session)):
    check_scope(id, account_id)
    check_scope(id, body.account_id)
    row = svc.account(db, id, lock=True)
    for key, value in body.model_dump(exclude={"account_id", "strategy"}, exclude_unset=True).items():
        if value is None:
            raise HTTPException(422, "Null account fields are not supported")
        setattr(row, key, value)
    if body.strategy is not None:
        strategy = db.get(AccountStrategy, id)
        if strategy is None:
            strategy = AccountStrategy(account_id=id)
            db.add(strategy)
        for key, value in body.strategy.model_dump(exclude_unset=True).items():
            setattr(strategy, key, value)
    svc.audit(db, id, "account_updated", id)
    db.commit()
    return account_view(db, row)


@router.get("/oauth/instagram/start")
def oauth_start(account_id: uuid.UUID, db: Session = Depends(svc.session)):
    svc.account(db, account_id, lock=True)
    state = secrets.token_urlsafe(32)
    with svc.meta_client() as meta:
        url = meta.authorize_url(state)
    db.add(OAuthState(account_id=account_id, state_hash=hashlib.sha256(state.encode()).hexdigest(),
                      expires_at=svc.now() + timedelta(minutes=10)))
    db.commit()
    return {"authorization_url": url}


@oauth_router.get("/callback")
def oauth_callback(state: str, code: str | None = None, error: str | None = None,
                   db: Session = Depends(svc.session)):
    # UPDATE ... RETURNING makes concurrent callbacks single-use even across processes.
    account_id = db.scalar(update(OAuthState).where(
        OAuthState.state_hash == hashlib.sha256(state.encode()).hexdigest(),
        OAuthState.consumed_at.is_(None), OAuthState.expires_at > svc.now(),
    ).values(consumed_at=svc.now()).returning(OAuthState.account_id))
    db.commit()
    if account_id is None:
        raise HTTPException(400, "Invalid or expired OAuth state")
    if error or not code:
        raise HTTPException(400, "Authorization was not completed; start again")
    row = svc.account(db, account_id, lock=True)
    with svc.meta_client() as meta:
        token = meta.exchange_code(code)
        profile = meta.get_profile(token.access_token)
    if token.user_id != profile.user_id or (row.ig_user_id is not None and row.ig_user_id != profile.user_id):
        raise HTTPException(409, "Instagram identity mismatch")
    duplicate = db.scalar(select(InstagramAccount.id).where(InstagramAccount.ig_user_id == profile.user_id,
                                                          InstagramAccount.id != account_id))
    if duplicate is not None:
        raise HTTPException(409, "Instagram identity already connected")
    row.ig_user_id, row.username = profile.user_id, profile.username
    row.encrypted_token = encrypt_token(token.access_token)
    row.token_acquired_at = svc.now()
    row.token_expires_at = row.token_acquired_at + timedelta(seconds=token.expires_in)
    svc.audit(db, account_id, "oauth_connected", account_id)
    db.commit()
    return {"account_id": account_id, "authorized": True}


@router.post("/accounts/{id}/refresh-token")
def refresh_token(id: uuid.UUID, account_id: uuid.UUID, body: Scope | None = None, db: Session = Depends(svc.session)):
    check_scope(id, account_id)
    check_scope(account_id, body.account_id if body else None)
    row = svc.account(db, id, lock=True)
    if (not row.encrypted_token or not row.token_expires_at or svc.utc(row.token_expires_at) <= svc.now()
            or not row.token_acquired_at or svc.utc(row.token_acquired_at) > svc.now() - timedelta(hours=24)):
        raise HTTPException(409, "Token is not eligible for refresh; reauthorize if expired")
    with svc.meta_client() as meta:
        token = meta.refresh(decrypt_token(row.encrypted_token))
        profile = meta.get_profile(token.access_token)
    if profile.user_id != row.ig_user_id:
        raise HTTPException(409, "Instagram identity mismatch")
    row.encrypted_token = encrypt_token(token.access_token)
    row.token_acquired_at = svc.now()
    row.token_expires_at = row.token_acquired_at + timedelta(seconds=token.expires_in)
    svc.audit(db, id, "token_refreshed", id)
    db.commit()
    return {"account_id": id, "token_expires_at": row.token_expires_at}


def validate_jpeg(data: bytes) -> bytes:
    if not data or len(data) > 8_000_000:
        raise HTTPException(422, "JPEG must be at most 8 MB")
    output = io.BytesIO()
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or getattr(image, "n_frames", 1) != 1:
                raise ValueError()
            width, height = image.size
            if not 320 <= width <= 1440 or not 0.8 <= width / height <= 1.91:
                raise ValueError()
            image.load()
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if not 320 <= width <= 1440 or not 0.8 <= width / height <= 1.91:
                raise ValueError()
            if image.info.get("icc_profile"):
                image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(image.info["icc_profile"])),
                                                   ImageCms.createProfile("sRGB"), outputMode="RGB")
            elif image.mode not in ("RGB", "L"):
                raise ValueError()
            image.convert("RGB").save(output, "JPEG", quality=95)
    except Exception:
        raise HTTPException(422, "Invalid JPEG: require width 320-1440, ratio 4:5 to 1.91:1 and valid color profile") from None
    normalized = output.getvalue()
    if len(normalized) > 8_000_000:
        raise HTTPException(422, "Normalized JPEG exceeds 8 MB")
    return normalized


def content_view(row):
    return {key: getattr(row, key) for key in ("id", "account_id", "caption", "status", "media_key", "media_hash",
                                                "published_at", "instagram_media_id", "error", "concept", "template_type",
                                                "top_text", "bottom_text", "alt_text", "hashtags", "generation_metadata")}


@router.post("/accounts/{id}/test-image", status_code=201)
def upload_image(id: uuid.UUID, account_id: uuid.UUID, image: UploadFile = File(),
                 rights_attested: bool = Form(), caption: str = Form(default="", max_length=2200),
                 form_account_id: uuid.UUID | None = Form(default=None, alias="account_id"),
                 db: Session = Depends(svc.session)):
    check_scope(id, account_id)
    check_scope(account_id, form_account_id)
    svc.account(db, id, lock=True)
    if not rights_attested:
        raise HTTPException(422, "Explicit original-image rights attestation required")
    data = validate_jpeg(image.file.read(8_000_001))
    digest = hashlib.sha256(data).hexdigest()
    if db.scalar(select(ContentPost.id).where(ContentPost.account_id == id, ContentPost.media_hash == digest)):
        raise HTTPException(409, "Image already exists for this account")
    if db.scalar(select(MemeCandidate.id).where(MemeCandidate.account_id == id, MemeCandidate.fingerprint == digest)):
        raise HTTPException(409, "Image already exists for this account")
    candidate = MemeCandidate(account_id=id, source_name="manual_original", title="Operator original JPEG",
                              idea="Manual test image", fingerprint=digest, rights_status="ORIGINAL", status="REVIEW")
    db.add(candidate)
    db.flush()
    row = ContentPost(account_id=id, candidate_id=candidate.id, caption=caption, status="REVIEW", media_hash=digest)
    db.add(row)
    db.flush()
    row.media_key = f"{id}/{row.id}/{digest}.jpg"
    with svc.storage_client() as storage:
        storage.upload(row.media_key, data)
    svc.audit(db, id, "original_rights_attested", row.id, media_hash=digest)
    db.commit()
    return content_view(row)


@router.get("/content")
def list_content(account_id: uuid.UUID, db: Session = Depends(svc.session)):
    svc.account(db, account_id)
    return [content_view(row) for row in db.scalars(select(ContentPost).where(ContentPost.account_id == account_id))]


@router.get("/content/{id}")
def get_content(id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(svc.session)):
    return content_view(svc.post(db, id, account_id))


@router.get("/content/{id}/image")
def preview_content(id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(svc.session)):
    from integrations.local_storage import LocalStorageClient
    row = svc.post(db, id, account_id)
    if not row.media_key:
        raise HTTPException(404, "Media unavailable")
    with svc.storage_client() as storage:
        if isinstance(storage, LocalStorageClient):
            path = storage.path(row.media_key)
            if not path.is_file():
                raise HTTPException(404, "Media unavailable")
            return FileResponse(path, media_type="image/jpeg", headers={"X-Content-Type-Options": "nosniff"})
        return RedirectResponse(storage.signed_url(row.media_key), status_code=307)


class Review(Input):
    account_id: uuid.UUID | None = None
    status: Literal["APPROVED", "REJECTED", "REVIEW"]
    caption: str | None = Field(default=None, max_length=2200)


@router.patch("/content/{id}")
def review_content(id: uuid.UUID, account_id: uuid.UUID, body: Review, db: Session = Depends(svc.session)):
    check_scope(account_id, body.account_id)
    svc.account(db, account_id, lock=True)
    row = svc.post(db, id, account_id, lock=True)
    if row.status not in ("REVIEW", "APPROVED", "REJECTED") or db.scalar(select(PublishingJob.id).where(PublishingJob.post_id == id)):
        raise HTTPException(409, "Content is locked by publishing")
    candidate = db.get(MemeCandidate, row.candidate_id)
    if body.status == "APPROVED" and (candidate.rights_status != "ORIGINAL" or not row.media_key or not row.media_hash):
        raise HTTPException(409, "Original media required")
    if body.caption is not None:
        row.caption = body.caption
    row.status = body.status
    candidate.status = body.status
    svc.audit(db, account_id, "review_" + body.status.lower(), id)
    db.commit()
    return content_view(row)


@router.post("/content/{id}/publish", status_code=202)
def publish_content(id: uuid.UUID, account_id: uuid.UUID, body: Scope | None = None, db: Session = Depends(svc.session)):
    from app.publishing import gate, dispatch
    check_scope(account_id, body.account_id if body else None)
    acct = svc.account(db, account_id, lock=True)
    row = svc.post(db, id, account_id, lock=True)
    existing = db.scalar(select(DurableJob).where(DurableJob.account_id == account_id,
                                                 DurableJob.kind == "publish", DurableJob.idempotency_key == str(id)))
    if existing is None:
        gate(db, acct, row)
        existing = DurableJob(account_id=account_id, kind="publish", idempotency_key=str(id), payload={"post_id": str(id)})
        db.add(existing)
        db.add(PublishingJob(post_id=id, next_attempt_at=svc.now()))
        svc.audit(db, account_id, "publish_requested", id)
        db.commit()
    accepted = dispatch(existing.id)
    return {"job_id": existing.id, "status": existing.status, "dispatch": "submitted" if accepted else "pending_recovery"}


@router.get("/jobs/{id}")
def job_status(id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(svc.session)):
    job = db.scalar(select(DurableJob).where(DurableJob.id == id, DurableJob.account_id == account_id))
    if job is None:
        raise HTTPException(404, "Job not found")
    result = {"id": job.id, "account_id": job.account_id, "kind": job.kind, "status": job.status, "result": job.result,
              "error": job.error, "attempts": job.attempts, "next_attempt_at": job.next_attempt_at,
              "lease_until": job.lease_until, "finished_at": job.finished_at}
    if job.kind == "publish":
        publishing = db.scalar(select(PublishingJob).where(PublishingJob.post_id == uuid.UUID(job.payload["post_id"])))
        if publishing:
            result.update(publishing_status=publishing.status, container_id=publishing.container_id,
                          next_attempt_at=publishing.next_attempt_at, attempts=publishing.attempts)
    return result


class JobRequest(Input):
    account_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")


class GenerationRequest(JobRequest):
    candidate_id: uuid.UUID | None = None


def accept_job(body, kind, query_account_id, db):
    from app.jobs import dispatch_job, enqueue_job
    if query_account_id is not None:
        check_scope(query_account_id, body.account_id)
    payload = {}
    candidate_id = getattr(body, "candidate_id", None)
    if candidate_id is not None:
        candidate = db.scalar(select(MemeCandidate.id).where(MemeCandidate.id == candidate_id,
                                                            MemeCandidate.account_id == body.account_id))
        if candidate is None:
            raise HTTPException(404, "Candidate not found")
        payload["candidate_id"] = str(candidate_id)
    job = enqueue_job(db, body.account_id, kind, body.idempotency_key, payload)
    submitted = dispatch_job(job.id)
    return {"job_id": job.id, "status": job.status, "dispatch": "submitted" if submitted else "pending_recovery"}


@router.post("/jobs/discovery", status_code=202)
def discovery_job(body: JobRequest, account_id: uuid.UUID | None = None, db: Session = Depends(svc.session)):
    return accept_job(body, "discovery", account_id, db)


@router.post("/jobs/generation", status_code=202)
def generation_job(body: GenerationRequest, account_id: uuid.UUID | None = None, db: Session = Depends(svc.session)):
    return accept_job(body, "generation", account_id, db)


@router.post("/jobs/schedule", status_code=202)
def schedule_job(body: JobRequest, account_id: uuid.UUID | None = None, db: Session = Depends(svc.session)):
    return accept_job(body, "schedule", account_id, db)


@router.post("/jobs/publish-due", status_code=202)
def publish_due_job(body: JobRequest, account_id: uuid.UUID | None = None, db: Session = Depends(svc.session)):
    return accept_job(body, "publish-due", account_id, db)


@router.post("/jobs/analytics", status_code=202)
def analytics_job(body: JobRequest, account_id: uuid.UUID | None = None, db: Session = Depends(svc.session)):
    return accept_job(body, "analytics", account_id, db)


@router.get("/analytics/summary")
def analytics_summary(account_id: uuid.UUID, days: int = 14, db: Session = Depends(svc.session)):
    from app.analytics import summary
    if not 1 <= days <= 90:
        raise HTTPException(422, "Days must be between1 and90")
    return summary(db, account_id, days)


@router.get("/analytics/weekly")
def latest_weekly_report(account_id: uuid.UUID, db: Session = Depends(svc.session)):
    svc.account(db, account_id)
    job = db.scalar(select(DurableJob).where(DurableJob.account_id == account_id,
        DurableJob.kind == "weekly-report", DurableJob.status == "SUCCEEDED").order_by(DurableJob.finished_at.desc()).limit(1))
    if job is None:
        raise HTTPException(404, "No completed weekly report")
    return {"job_id": job.id, "report": job.result}


@router.get("/monitoring")
def monitoring_status(db: Session = Depends(svc.session)):
    from app.analytics import monitoring
    return monitoring(db)


@router.post("/jobs/{id}/retry", status_code=202)
def retry_job(id: uuid.UUID, account_id: uuid.UUID, db: Session = Depends(svc.session)):
    from app.jobs import GENERIC_KINDS, dispatch_job
    job = db.scalar(select(DurableJob).where(DurableJob.id == id, DurableJob.account_id == account_id).with_for_update())
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.kind not in GENERIC_KINDS or job.status != "FAILED":
        raise HTTPException(409, "Only failed non-publishing jobs can be retried")
    job.status, job.attempts, job.error, job.finished_at = "PENDING", 0, None, None
    job.lease_until, job.lease_owner, job.next_attempt_at = None, None, None
    svc.audit(db, account_id, "job_manual_retry", id)
    db.commit()
    submitted = dispatch_job(job.id)
    return {"job_id": job.id, "status": job.status, "dispatch": "submitted" if submitted else "pending_recovery"}


class Reconcile(Input):
    account_id: uuid.UUID | None = None
    verified_media_id: str | None = Field(default=None, pattern=r"^[0-9]+$", max_length=100)
    operator_verified: bool = False


@router.post("/content/{id}/reconcile")
def reconcile_content(id: uuid.UUID, account_id: uuid.UUID, body: Reconcile, db: Session = Depends(svc.session)):
    from app.publishing import reconcile
    check_scope(account_id, body.account_id)
    return reconcile(db, account_id, id, body.verified_media_id, body.operator_verified)
