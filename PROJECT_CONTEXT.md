# PROJECT CONTEXT: Instagram AI Meme Automation

## Goal
Build an end-to-end AI-driven Instagram meme operation managing **3 authorized Instagram Professional accounts**. Discover trending ideas, generate **original** memes and Indonesian captions, quality-check, schedule and publish to each account through the official Meta Instagram API, collect insights and improve selection based on performance.

## MVP scope
- Three Instagram accounts, each with distinct niche and tone: Office / Work, Gaming, Programming / IT.
- Initial target 2 JPEG image posts per account per day (6/day), adjustable by account.
- Initial image size 1080x1350, Pillow rendering with reusable, rights-cleared templates. Reels and carousel are phase 2.
- Scheduled discovery every 6 hours; AI creation; manual review initially, configurable auto-approval when validated; posting every 5 minutes; daily analytics; failure monitoring.
- Entire workflow automated once configured, with safety gates and manual override/kill switch.
- Indonesian language captions optimized for relevance and genuine engagement; no guarantees of virality.

## Stack and responsibility
Cost override from the user's later instruction: do not require additional paid subscriptions/services beyond ChatGPT Plus. Plus does not include OpenAI API usage. Default AI is now local Ollama with cloud disabled and mandatory manual review; default storage is private local filesystem with expiring signed HTTPS media URLs. The original OpenAI/Supabase adapters are optional, not enabled by Compose, and no paid service is authorized. No cloud model downloads/provisioning or paid calls are performed automatically. Live HTTPS hosting, local model/hardware and Meta authorization remain operator prerequisites.

- Docker Compose services: FastAPI (Python 3.12), Celery worker, Redis broker, PostgreSQL 16, n8n.
- n8n: scheduling, HTTP triggering, monitoring, alerts; never stores per-account Meta tokens or core business logic.
- Python services: source discovery adapters, deduplication, AI scoring, meme joke/caption creation, Pillow rendering, storage uploads, scheduling, Meta OAuth & publishing, insights, retry/reconciliation.
- AI: OpenAI API with structured JSON output and validation; model names configured via env.
- Storage: private Supabase Storage (or compatible S3 object storage) with time-limited media URLs accessible to Meta during ingestion.
- Later: optional admin dashboard and FFmpeg Reels support.

## Required architecture
- Backend endpoints (all internal routes authenticated):
  - GET `/api/v1/health`
  - GET/POST `/api/v1/accounts`, PATCH `/api/v1/accounts/{id}`
  - GET `/api/v1/oauth/instagram/start`, GET `/api/v1/oauth/instagram/callback`
  - POST `/api/v1/jobs/discovery`, `/generation`, `/schedule`, `/publish-due`, `/analytics`
  - GET `/api/v1/jobs/{id}`
  - GET `/api/v1/content`, PATCH `/api/v1/content/{id}`, POST `/api/v1/content/{id}/publish`
  - GET `/api/v1/analytics/summary`
- Async jobs return HTTP 202 with stable job ID and are persisted, with GET status.
- Candidate lifecycle: DISCOVERED -> SCORED -> SELECTED -> GENERATED -> REVIEW -> APPROVED -> SCHEDULED -> PUBLISHED -> ANALYZED, with FAILED/REJECTED branches (align database state enums and APIs).
- Separate per-account content queue and schedule slots.

## Database entities
1. `instagram_accounts`: uuid, ig_user_id, username, niche, language, timezone, posts_per_day, approval_mode, encrypted token, token expiry, active status.
2. `meme_candidates`: uuid, account_id, source name/url/id, title, idea, rights status, individual scores, final score, fingerprint, status.
3. `content_posts`: uuid, account_id, candidate_id, content type, joke, caption, hashtags, media key/hash, status, scheduled/published time, Instagram media id, error.
4. `publishing_jobs`: uuid, post_id, status, container_id, attempts, next attempt, lease, error.
5. `post_metrics`: post_id, collected time, views/reach/likes/comments/shares/saves where supported, raw JSON.
6. `account_strategies`: account_id, tone, audience, topic allow/block lists, posting times, threshold, prompt version.
Implement with SQLAlchemy models and Alembic migrations. Add indexes, ownership checks, idempotency, audit trails, and job status persistence. Use UTC in DB.

## n8n workflows to export as importable JSON
01 Discover memes: every 6 hours -> POST backend discovery per active account.
02 Generate content: 06:15 and 18:15 Jakarta -> POST generation per active account.
03 Schedule: every 30 minutes -> POST scheduler.
04 Publish due: every 5 minutes -> POST publish-due.
05 Analytics: daily 01:00 Jakarta -> POST analytics.
06 Failure monitoring: every 15 minutes -> check failed/stale jobs and alert.
Use `http://api:8000` for Docker-to-Docker calls and n8n credentials for internal API key.

## AI content pipeline
- Discover trend *ideas* via RSS and licensed content sources; Reddit integration is optional and only if API approval/terms permit.
- Normalize, fingerprint and deduplicate ideas and images.
- AI score based on niche relevance 30%, humor/relatability 25%, freshness 20%, uniqueness 15%, quality/safety 10%; configurable threshold ~75/100, plus hard rights/safety rejections.
- Generate structured `concept`, `template_type`, `top_text`, `bottom_text`, `caption`, `hashtags`, `alt_text` (Pydantic validation).
- Render safe-area image, wrap and scale text; verify dimensions and quality, optionally vision-check.
- Publish only original/rights-cleared media, no blind reposting or false claims.
- Manual approval first; later optional auto-approval if quality gates pass.

## Meta Instagram integration
- Use *Instagram API with Instagram Login* for professional accounts, and verify current official permissions/OAuth endpoints using docs at development time. Do not mix with Facebook Login permissions.
- Secure per-account authorization and token refresh; validate OAuth state, encrypt tokens, track expiry and failures.
- Single image publishing: create media container, poll status, publish container, save IG media ID and timestamps.
- Respect per-account limits and backoff. For ambiguous publish timeout, mark UNCERTAIN; reconcile before any retry. Never blindly repeat a publish call.
- No login/password automation; no bypass of Meta restrictions. Manual action needed for Meta app registration, Instagram authorization, external approvals, API secrets and hosting.

## Analytics
Collect metric snapshots 24h, 72h, 7d when available. Compute share/save/engagement rates using reach denominators only when nonzero. Compare posts of same age; avoid naive conclusions from small samples. Version prompts and track AI spend. Weekly reports and topic experiments.

## Suggested account slots (Jakarta time)
- Office: 12:00 and 19:00
- Gaming: 13:00 and 21:00
- IT: 11:30 and 20:00
Treat as starting hypotheses, not guaranteed best times.

## Security and reliability
- Dev `.env.example` with placeholders; never commit real `.env`.
- Token encryption; service account isolation; avoid sensitive fields in logs.
- Per-post and per-account locks, DB transactions, durable job state, bounded retries and dead-letter/manual recovery.
- Globally configurable `ENABLE_AUTOPUBLISH=false` initially, plus per-account pause.
- No public exposure for database, Redis or API. Reverse proxy HTTPS for OAuth and n8n production dashboard.
- Docker healthchecks, backup plan, logs/alerts, automated tests.

## Development milestone order
1. Docker + database migrations + working FastAPI health + Celery + n8n + tests.
2. Meta OAuth + official publishing integration + one manually approved test image.
3. Discovery + AI structured meme/caption generator + renderer/storage + rights gating.
4. Scheduling + multi-account queue + importable n8n workflows + reliability tests.
5. Insights + weekly strategy and monitoring + deployment docs.
6. Optional dashboard, Reels and advanced optimizations.

## Testing acceptance
- 3 accounts separately authorized and isolated.
- Dedup discovery works, generated image legible, caption matches account.
- Correct 6/day schedule targets and no duplicate posting under concurrent workers.
- Rate limits, expired tokens, failed processing and uncertain publish outcomes handled safely.
- Pausing one account doesn't affect others. Kill switch halts new publish attempts.
- Metrics snapshots persist; recovery after worker restart.
- No claim of live Meta publishing success until actual authorized end-to-end test completes.

## Operator expectations
Develop the project files rather than only explaining them. Keep a working README, exact commands and testing status. Implement feature by feature, commit meaningful progress and record blockers in `IMPLEMENTATION_PROGRESS.md`.
