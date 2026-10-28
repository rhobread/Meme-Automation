# Instagram AI Meme Automation

Milestones 1 and 2 implement infrastructure, account/strategy APIs, Instagram Login OAuth, private original JPEG uploads, explicit review, and durable manual publishing with conservative recovery. External adapters use real HTTP; tests mock the external transport only. No live Meta authorization, Supabase upload, or Instagram publication has been verified. Discovery, AI generation, scheduling and analytics remain future milestones.

## Windows Setup

1. Install Docker Desktop with the WSL2 Linux-container backend and current Docker Compose v2. Start Docker Desktop. Open this folder in VS Code.
2. Open a PowerShell terminal in `C:\Personal Projects\Meme Automation`.
3. Create `.env` from `.env.example` using the VS Code file explorer. Replace every placeholder locally. Never paste credentials into chat or commit `.env`.
4. Use a random URL-safe alphanumeric PostgreSQL password (at least 32 characters), independent random internal API/n8n keys (at least 32 characters), and a Fernet encryption key. Keep `ENABLE_AUTOPUBLISH=false`. Keys must remain stable across restarts.

Generate values locally without posting the output elsewhere:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\python -c "import secrets; print(secrets.token_hex(32))"
.\.venv\Scripts\python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run the random hex command separately for each password/key; the Fernet command is only for `TOKEN_ENCRYPTION_KEY`. Python 3.12 is the target runtime; local development also permits 3.13. The Docker image always uses 3.12.

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps -a
docker compose exec api python -m app.smoke
```

The smoke command checks the authenticated HTTP endpoint, migration revision, PostgreSQL and Redis connectivity, then sends a real Celery task and waits for its result. A passing local pytest suite is not a substitute for this command.

Open `http://localhost:5678` and complete n8n's first-run owner registration. It uses its own persistent SQLite volume, not the application database. Later workflows should call `http://api:8000` and use an n8n Header Auth credential named `X-API-Key`; do not embed the API key in workflow exports. No later-milestone workflow exports are included yet.

## Interfaces

- `GET /api/v1/health` requires `X-API-Key`. Returns 200 only when PostgreSQL, the current Alembic head (currently `0002`), and Redis are available; returns sanitized 503 on dependency failure and 401 on invalid/missing authentication. It does not claim that workers or n8n are healthy.
- `app.main:app`: FastAPI entrypoint; OpenAPI/Swagger routes disabled.
- `app.worker:celery_app`: JSON-only UTC Celery application; `app.worker.smoke` checks real dependencies. `app.worker.publish` handles one bounded publishing step; `app.worker.recover` redelivers due DB jobs. The separate Compose `beat` service triggers recovery every 60 seconds. Smoke results expire after one hour.
- `app.config.get_settings()`: validated environment configuration with secret values masked in representations.
- `app.db.get_engine()`: pooled PostgreSQL engine, UTC connection timezone, 3-second connection and 5-second statement timeouts.
- `app.security.encrypt_token()` / `decrypt_token()`: Fernet authenticated token encryption. No plaintext token database column exists.
- `app.models`: six core entities plus durable jobs, audit events and hashed OAuth state storage. Account ownership is enforced through a composite candidate foreign key. Per-account fingerprints, schedule slots, job idempotency keys, metric snapshot times and one publishing job per post are unique. PostgreSQL timestamps are timezone-aware; application timestamps use UTC.
- `migrations/versions/0001_initial_schema.py`: frozen explicit Alembic operations, not runtime `create_all`. Add new revisions for future changes; never rewrite a deployed migration.
- `migrations/versions/0002_token_acquired.py`: nullable token acquisition timestamp. Existing tokens without an acquisition time must be reauthorized before refresh.
- `app.api`: accounts/strategy, OAuth, image ingestion/review, job status and reconciliation routes. `app.services`: DB dependency and configured adapter factories. `app.publishing`: safety gates, durable dispatch, pinned PostgreSQL account advisory lock plus account/post row locks, publish state machine and reconciliation.
- `integrations.meta.MetaClient`: existing official Instagram Login HTTP adapter, unchanged by M2. `integrations.storage.StorageClient`: context-managed real Supabase REST adapter; `upload(object_path, jpeg_bytes)` verifies a private bucket and forbids overwrite, `signed_url(object_path)` checks privacy again and returns a one-hour HTTPS URL. No signed URL or token enters a job payload or API media response.

OAuth state consumption is an atomic, expiring, single-use `UPDATE ... RETURNING` against a SHA-256 hash of 256-bit random state. OAuth start requires the API key and account ID; the public callback is the **sole authentication exception**. The callback validates token/profile identity and never silently replaces an existing Instagram identity. Tokens are encrypted with Fernet; expiry uses Meta's `expires_in`. Refresh requires an unexpired long-lived token acquired at least 24 hours ago. All content/job queries require `account_id`; conflicting path/query/body scopes are rejected. The API key is an administrative credential authorized for all accounts, not a per-tenant authorization token.

Audit events record rights attestation, approval, authorization and publishing actions without secrets; database-enforced append-only policy is still future production hardening. Metrics remain schema-only.

## Manual Test Workflow

Prerequisites: an authorized Instagram Professional account, a Meta developer app with Instagram Login and the documented scopes, a registered HTTPS redirect URI, and a pre-existing **private** Supabase bucket. Set `META_APP_ID`, `META_APP_SECRET`, `META_REDIRECT_URI` (ending in `/api/v1/oauth/instagram/callback`), `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_BUCKET` in local `.env`. Service-role credentials stay server-side. Storage upload needs INSERT, signing SELECT; the service role also permits the bucket privacy check. Do not make the bucket public. No OpenAI credentials are required for M2.

Keep `ENABLE_AUTOPUBLISH=false` while configuring and reviewing. In PowerShell, make the same internal key available as the local environment variable `INTERNAL_API_KEY` without putting its value in a command or sharing it. Then:

```powershell
$base = 'http://localhost:8000/api/v1'
$headers = @{'X-API-Key' = $env:INTERNAL_API_KEY}
$account = Invoke-RestMethod -Method Post -Uri "$base/accounts" -Headers $headers -ContentType 'application/json' -Body '{"username":"pending-authorization","niche":"Programming","strategy":{"tone":"dry","audience":"developers"}}'
$accountId = $account.id
$oauth = Invoke-RestMethod -Uri "$base/oauth/instagram/start?account_id=$accountId" -Headers $headers
Start-Process $oauth.authorization_url
```

Authorize in the browser. The callback uses state, not `X-API-Key`, and returns `{account_id, authorized:true}` on completion. It does not activate a paused account. State expires after ten minutes and is consumed even if authorization/exchange fails; start a new flow after failure. Never log or share the authorization URL, callback query or browser history.

Prepare an image that you personally created and have permission to publish. A 1080x1350 sRGB JPEG is recommended. The API validates actual Pillow decoding, JPEG-only format, <=8 MiB, width 320-1440 and aspect ratio 4:5 through 1.91:1. It applies EXIF orientation, converts valid embedded color profiles to sRGB, strips metadata by re-encoding, and stores the normalized content's SHA-256. Original pixels are never sourced automatically. `rights_attested=true` is an explicit operator statement of original-image publishing rights, not a claim inferred by the application.

```powershell
$upload = curl.exe --fail --silent --show-error -X POST "$base/accounts/$accountId/test-image?account_id=$accountId" -H "X-API-Key: $env:INTERNAL_API_KEY" -F 'rights_attested=true' -F 'caption=Original operator test image' -F 'image=@C:\path\original-test.jpg;type=image/jpeg' | ConvertFrom-Json
$postId = $upload.id
Invoke-RestMethod -Uri "$base/content/$postId`?account_id=$accountId" -Headers $headers
Invoke-RestMethod -Method Patch -Uri "$base/content/$postId`?account_id=$accountId" -Headers $headers -ContentType 'application/json' -Body '{"status":"APPROVED"}'
```

Upload creates `ORIGINAL` candidate + `REVIEW` content only. Approval is separate. Review supports `REVIEW`, `APPROVED`, `REJECTED` and an optional caption; once a publishing job exists, media review is locked. The operator reviews the local image used for upload; API responses intentionally do not include signed image URLs. Uploads are account-scoped and duplicate normalized image hashes are rejected per account.

When ready for the intentional test post, set `ENABLE_AUTOPUBLISH=true` in `.env` and recreate API/worker/beat so every process receives the setting. Despite the variable's historical name, this kill switch applies to **every new publish call, including manual publishing**. There is no bypass.

```powershell
docker compose up -d --force-recreate api worker beat
Invoke-RestMethod -Method Patch -Uri "$base/accounts/$accountId" -Headers $headers -ContentType 'application/json' -Body '{"active":true}'
$job = Invoke-RestMethod -Method Post -Uri "$base/content/$postId`/publish?account_id=$accountId" -Headers $headers
$jobId = $job.job_id
Invoke-RestMethod -Uri "$base/jobs/$jobId`?account_id=$accountId" -Headers $headers
```

The publish route returns **202 accepted, not published**. The job ID is stable for repeated requests to the same post. `dispatch=pending_recovery` means broker submission failed but the DB job is committed; beat retries delivery when Redis/worker recover. Check status after the reported `next_attempt_at`; normal processing requires at least one later minute for the container poll. Status includes durable and publishing state, attempts, container ID, result and safe error code. Confirm `SUCCEEDED` and the actual post on Instagram before treating the live test as passed. Stop further publishing by pausing the account, or by setting the global switch false and recreating all app processes.

```powershell
Invoke-RestMethod -Method Patch -Uri "$base/accounts/$accountId" -Headers $headers -ContentType 'application/json' -Body '{"active":false}'
Invoke-RestMethod -Method Post -Uri "$base/accounts/$accountId/refresh-token?account_id=$accountId" -Headers $headers
```

Refresh before expiry but never less than 24 hours after acquisition/refresh. A 409 means the age/expiry/scope gate rejected it; reauthorize an expired or legacy acquisition-unknown token. Update strategy fields using `PATCH /accounts/{id}` with a `strategy` object; omitted strategy fields are preserved. Repeat account creation and OAuth separately for the other two accounts.

## Recovery Semantics

- The worker serializes each account with a pinned PostgreSQL session advisory lock across commits; row locks protect account/post updates. Different accounts can progress independently. SQLite is used only for local tests, not production locking.
- Check the global switch, account pause, approval/original rights, encrypted token expiry and API-reported per-account quota before external publishing side effects. The app cap defaults to 25 and is constrained to <=50 because current Meta documents contain conflicting 50/100 statements. No scheduling or daily-target automation is implemented.
- Persist the container before polling. Poll no faster than once per minute, with a five-step automatic attempt budget including creation. Failed/expired containers and exhausted budgets stop; terminal jobs have no automatic reset endpoint.
- Commit `UNCERTAIN` **before** calling `media_publish`. Every error/crash after this boundary stays uncertain, even if the call might not have been sent. A worker restart, duplicate delivery or repeated publish request never republishes it. An unresolved uncertain post blocks new publications for that account.
- `FINISHED` means ready, not published. Authenticated reconciliation performs only a status read, never a publish. `PUBLISHED` marks the post published but keeps the job unresolved if the media ID is missing; no heuristic lookup/matching is attempted.
- `POST /content/{id}/reconcile?account_id=...` with `{}` checks an uncertain container; respect its one-minute retry interval. When the container is `PUBLISHED`, an operator who has independently verified the exact media/account/container relationship may submit `{"verified_media_id":"<numeric-id>","operator_verified":true}` to record it and finish the job. This is an audited human attestation, not an API-derived identity guarantee. Do not guess an ID. Other container statuses remain uncertain and require investigation; never manually reset a timeout to pending.
- If the process dies after storage upload but before DB commit, an unreferenced private object can remain. A later operational cleanup must only remove verified unreferenced objects; no destructive garbage collection is included in M2.

The test workflow above is covered by `tests/test_milestone2.py` against real application/database code and mocked Meta/Supabase HTTP transport. It has **not** been executed against live services.

Official references checked for this milestone (2026-10-28; Meta pages accessed through `https://r.jina.ai/` where direct access returned 400):

- https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login/
- https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing/
- https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-container/
- https://developers.facebook.com/docs/graph-api/changelog/ (`META_API_VERSION=v26.0`, configurable)
- https://supabase.com/docs/guides/storage/uploads/standard-uploads
- https://supabase.com/docs/guides/storage/serving/downloads

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m compileall -q app integrations migrations tests
.\.venv\Scripts\python -m pip check
```

Tests execute immutable migrations against temporary SQLite databases with foreign keys enabled, compare schema to ORM metadata, exercise upgrade/downgrade/upgrade, compile PostgreSQL migration SQL, and cover account isolation, atomic concurrent OAuth consumption, refresh eligibility, JPEG validation, private storage, explicit rights/review, broker failure/recovery, bounded polling, pre-publish uncertainty persistence, restart safety and reconciliation. They do not exercise PostgreSQL advisory locks, Redis delivery, Docker networking, or n8n startup. Run the Compose smoke check for real service delivery. Python dependencies are bounded in `pyproject.toml`, not transitively locked; container image tags other than n8n track patch updates.

`.github/workflows/tests.yml` defines Python 3.12/3.13 unit jobs and a Compose smoke/n8n readiness job using ephemeral credentials and publishing disabled. This workflow has not run on CI in this workspace; there is no git remote configured here.

## Security And Operations

API and n8n ports bind only to `127.0.0.1`. PostgreSQL and Redis have no published ports and use an internal Docker network. API/worker additionally join the egress network for Meta/Supabase; migration/beat remain internal-only. n8n has egress but no Meta tokens or token-encryption key. API/worker run as a non-root user. Uvicorn access logs are disabled; HTTPX/httpcore logging is suppressed to avoid OAuth query-string leakage. Do not enable request/response tracing. Validation and exception responses are sanitized. Do not put tokens in job payloads, errors, audit details or logs.

Use an HTTPS reverse proxy that exposes only `GET /api/v1/oauth/instagram/callback`; all other API routes must stay private and require `X-API-Key`. Disable/redact callback query logging at the proxy too. Set a request-body limit near 9 MiB at the proxy: the API caps decoded image reads but multipart parsing can spool larger incoming requests before route validation. Signed storage URLs are one-hour bearer URLs and must not be logged; according to Supabase, rotating API keys does not revoke already-issued signed URLs.

This Compose setup is local development, not a hardened production deployment: the application database role also owns migrations; Redis trusts the isolated backend network; n8n HTTP cookies are permitted only for localhost. Before production, separate migration/runtime roles, configure authenticated TLS service access as needed, use HTTPS reverse proxy/access control for n8n, enable secure cookies, and expose only narrowly scoped OAuth callbacks rather than the internal API.

Compose waits for healthy PostgreSQL before migrations and successful migration completion before API/worker startup. Redis uses AOF persistence. PostgreSQL, Redis and n8n have separate named volumes. `docker compose down` preserves data; **do not use `down -v` unless intentionally deleting all data**.

Back up PostgreSQL with `pg_dump` (run inside the database container), copy the resulting dump to encrypted external storage, and verify restore into a separate database. Back up the n8n volume while n8n is stopped, together with its encryption key; protect the application Fernet key separately. Volume persistence is not a backup. Restore procedures have not been exercised in this environment.

Troubleshooting:

- `docker` not found: install Docker Desktop, reopen the terminal, and verify `docker version` and `docker compose version`.
- Migration exits nonzero: inspect `docker compose logs migrate`; fix the cause and rerun `docker compose up -d`. Never bypass failed migrations.
- API unhealthy: check generated keys, database/Redis health and `docker compose logs api`. Placeholder API/Fernet keys intentionally fail validation.
- Password changes do not update an existing PostgreSQL volume automatically; rotate the database user's password deliberately rather than deleting data.
- Worker smoke timeout: inspect `docker compose logs worker redis` and worker health. Never interpret a queued task as completed work.
- n8n cookie/login problems over remote HTTP: this configuration is localhost-only. Deploy HTTPS rather than exposing insecure HTTP.

n8n is pinned to `2.45.4` based on official Docker documentation research supplied for this milestone: https://docs.n8n.io/hosting/installation/docker/ . `GENERIC_TIMEZONE=Asia/Jakarta` and settings file permissions are configured. Deprecated `N8N_RUNNERS_ENABLED` is intentionally omitted. No n8n Code nodes or Python runners are required.
