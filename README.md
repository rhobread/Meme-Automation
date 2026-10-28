# Instagram AI Meme Automation

## Cost Restrictions

No additional paid services or subscriptions are authorized. ChatGPT Plus ($20/month) does **not** include OpenAI API usage and does not establish coverage for Kilo or its model provider. The OpenAI adapter is blocked by default with `ALLOW_PAID_SERVICES=false`, even if an API key is present. Keep this flag false. Local Ollama inference and filesystem storage are the defaults; actual model inference remains unverified. Never use browser automation to repurpose ChatGPT as an API.

PostgreSQL, Redis, Python, Celery and self-hosted n8n can run locally without a service subscription, subject to their licenses. Docker Desktop has eligibility-dependent licensing; use it only if eligible for its free license, or use a suitable free container runtime. Local compute still uses electricity and existing hardware. Storage free tiers have quotas and terms; no cloud resources have been provisioned. Public HTTPS hosting/domain availability must be resolved without paid provisioning. GitHub Actions is manual-only; check free quotas and disable paid overages before running it. No claim that the whole deployment or Kilo usage is automatically covered by Plus is made.

Milestones 1-5 implement infrastructure, accounts/OAuth, original image ingestion, safe publishing, permitted discovery/local AI generation, multi-account scheduling, insights and monitoring. External adapters use real HTTP; tests mock external transport only. No live Meta authorization/publication, Docker startup, actual local-model inference or alert delivery has been verified. Optional dashboard, Reels and carousel are not implemented.

## Local Generation

Install Ollama only from its official distribution. Before starting it, set the server environment variable `OLLAMA_NO_CLOUD=1`; restart Ollama and verify its log says cloud disabled. Choose a local model whose license/hardware requirements you accept. The configured example `qwen2.5:7b` is not automatically downloaded. If suitable, the operator can run `ollama pull qwen2.5:7b`; this command was not executed here. Do not select cloud models or sign up for cloud inference.

Compose connects to `http://host.docker.internal:11434`. For non-Docker development set `OLLAMA_URL=http://127.0.0.1:11434`. Docker-to-host reachability depends on Docker Desktop/firewall configuration; if a bind change is necessary, restrict port11434 to the private Docker host interface/network, never the Internet. CPU-only inference can be slow and exceed the bounded60-second request timeout. No paid fallback occurs. Local-model safety judgments are imperfect; local generation always enters REVIEW.

Configure `DISCOVERY_FEEDS` in `.env` as JSON, replacing the account UUID and URL with an actually permitted RSS source:

```dotenv
DISCOVERY_FEEDS=[{"name":"permitted-source","url":"https://YOUR-PERMITTED-FEED/rss","account_ids":["YOUR-ACCOUNT-UUID"],"ideas_use_permitted":true}]
```

No source is assumed licensed. Only idea titles are used; source images are never downloaded. Trigger `POST /api/v1/jobs/discovery` then `/api/v1/jobs/generation` with authenticated JSON `{"account_id":"UUID","idempotency_key":"operator-run-1"}`. Poll the returned job UUID using the existing account-scoped job endpoint. Each generation job currently handles one candidate. Reuse the same key only for identical request input; use a fresh key for another intentional run. Review generated content and approve explicitly through the content endpoint.

Local media lives in the persistent `media_data` volume (`LOCAL_MEDIA_DIR` outside Docker). Set `MEDIA_PUBLIC_BASE_URL` to an existing HTTPS origin only when ready for Meta ingestion. Expose only `/media/*` and `/api/v1/oauth/instagram/callback` through that origin, never the remaining API or Ollama. Signed media URLs are one-hour bearer credentials; do not log/share them. No HTTPS origin is needed for private generation/storage, but live publishing cannot work until Meta can fetch the signed JPEG. Back up the media volume alongside PostgreSQL and the encryption key.

## Scheduling And n8n

`POST /api/v1/jobs/schedule` and `/publish-due` accept the same authenticated `account_id`/`idempotency_key` JSON contract. Scheduling reserves two days of slots, with a default two posts/account/day. Update `strategy.posting_times` using distinct `HH:MM` strings before increasing `posts_per_day`. Manual posts count toward the target. Pausing an account isolates it; `ENABLE_AUTOPUBLISH=false` stops new publishing, not private generation/scheduling. A slot over1h late returns to REVIEW instead of causing a catch-up burst.

Import the six `n8n/*.json` files individually; all are inactive. Select a Header Auth credential named `Meme Internal API`, header `X-API-Key`, with your internal key. The discovery/generation/schedule/publish/analytics workflows fetch active accounts and send account-scoped jobs. Generation currently makes one candidate per invocation, not a guarantee of enough approved content after rejections. Keep the manual review queue supplied before enabling publishing.

For monitoring, select SMTP credential `Meme Alerts SMTP` using an existing no-cost SMTP account/server whose terms permit alerts. Set n8n variables `ALERT_EMAIL` and `ALERT_FROM_EMAIL`, or replace those two address fields in the UI if variables are unavailable in your edition. Configure an n8n Error Workflow for HTTP/SMTP failures. Only publish workflows after checking credentials, timezone and backend health. Unhealthy monitoring emails every15min until resolved; SMTP delivery and n8n execution have not been tested here.

Poll `GET /api/v1/jobs/{job_id}?account_id=UUID` for actual completion;202 means accepted, not completed. Failed non-publishing jobs may be deliberately retried with `POST /api/v1/jobs/{job_id}/retry?account_id=UUID` after fixing the cause. Publishing jobs cannot use this retry route; uncertain publication requires the documented reconciliation procedure. Do not clear containers or clone uncertain content to retry it.

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

Open `http://localhost:5678` and complete n8n's first-run owner registration. It uses its own persistent SQLite volume, not the application database. Import/configure the six exports as described above; use `http://api:8000` and the `Meme Internal API` Header Auth credential, never inline keys.

## Interfaces

- `GET /api/v1/health` requires `X-API-Key`. Returns200 only when PostgreSQL, current Alembic head (`0005`), and Redis are available; returns sanitized503 on dependency failure and401 on invalid/missing authentication. It does not claim workers, local inference or n8n are healthy.
- `app.main:app`: FastAPI entrypoint; OpenAPI/Swagger routes disabled.
- `app.worker:celery_app`: JSON-only UTC Celery application; `app.worker.smoke` checks real dependencies. `app.worker.publish` handles one bounded publishing step; `app.worker.recover` redelivers due DB jobs. The separate Compose `beat` service triggers recovery every 60 seconds. Smoke results expire after one hour.
- `app.config.get_settings()`: validated environment configuration with secret values masked in representations.
- `app.db.get_engine()`: pooled PostgreSQL engine, UTC connection timezone, 3-second connection and 5-second statement timeouts.
- `app.security.encrypt_token()` / `decrypt_token()`: Fernet authenticated token encryption. No plaintext token database column exists.
- `app.models`: six core entities plus durable jobs, audit events, AI usage and hashed OAuth state storage. Ownership is enforced through a composite candidate foreign key. Per-account fingerprints, exact media hashes, schedule slots, job keys, metric snapshot times and one publishing job per post are unique. PostgreSQL timestamps are timezone-aware; application timestamps use UTC.
- `migrations/versions/0001_initial_schema.py`: frozen explicit Alembic operations, not runtime `create_all`. Add new revisions for future changes; never rewrite a deployed migration.
- `migrations/versions/0002_token_acquired.py`: nullable token acquisition timestamp. Existing tokens without an acquisition time must be reauthorized before refresh.
- Revisions `0003`/`0004`/`0005`: generation checkpoints/AI usage, exact-image dedup and append-only audit triggers. Existing duplicates must be investigated before upgrading to0004; never delete user data automatically to make a migration pass.
- `app.api`: accounts/strategy, OAuth, image ingestion/review, job status and reconciliation routes. `app.services`: DB dependency and configured adapter factories. `app.publishing`: safety gates, durable dispatch, pinned PostgreSQL account advisory lock plus account/post row locks, publish state machine and reconciliation.
- `integrations.meta.MetaClient`: existing official Instagram Login HTTP adapter, unchanged by M2. `integrations.storage.StorageClient`: context-managed real Supabase REST adapter; `upload(object_path, jpeg_bytes)` verifies a private bucket and forbids overwrite, `signed_url(object_path)` checks privacy again and returns a one-hour HTTPS URL. No signed URL or token enters a job payload or API media response.

OAuth state consumption is an atomic, expiring, single-use `UPDATE ... RETURNING` against a SHA-256 hash of256-bit random state. OAuth start requires the API key and account ID; the callback is state-authenticated rather than API-key authenticated. `/media/*` separately requires an expiring HMAC bearer signature. All remaining routes require the API key. Callback validates token/profile identity and never silently replaces an Instagram identity. Tokens are encrypted with Fernet; expiry uses Meta's `expires_in`. Refresh requires an unexpired long-lived token at least24h old. Content/job queries require `account_id`; conflicting scopes are rejected. The API key is an administrative credential for all accounts, not a per-tenant authorization token.

Audit events record rights, review, authorization, generation, scheduling, publishing and insights without secrets. Migration0005 rejects UPDATE/DELETE/TRUNCATE of the PostgreSQL audit table (SQLite UPDATE/DELETE in tests). A database owner can still override triggers; production runtime-role separation is required.

## Manual Test Workflow

Prerequisites: an authorized Instagram Professional account, a Meta developer app with Instagram Login/scopes, and an existing reachable HTTPS origin. Set `META_APP_ID`, `META_APP_SECRET`, `META_REDIRECT_URI` (ending `/api/v1/oauth/instagram/callback`) and `MEDIA_PUBLIC_BASE_URL` locally. Default private local storage needs no cloud credentials. The optional Supabase adapter is not enabled by Compose; do not enable cloud storage without checking its free-tier quotas, billing and privacy. No OpenAI credentials are needed.

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

Prepare an image that you personally created and have permission to publish. A1080x1350 sRGB JPEG is recommended. The API validates actual Pillow decoding, JPEG-only format, <=8,000,000 bytes, width320-1440 and aspect ratio4:5 through1.91:1. It applies EXIF orientation, converts embedded profiles to sRGB, strips metadata by re-encoding and stores SHA-256. Original pixels are never sourced automatically. `rights_attested=true` is an explicit operator statement of publishing rights, not a claim inferred by the application.

```powershell
$upload = curl.exe --fail --silent --show-error -X POST "$base/accounts/$accountId/test-image?account_id=$accountId" -H "X-API-Key: $env:INTERNAL_API_KEY" -F 'rights_attested=true' -F 'caption=Original operator test image' -F 'image=@C:\path\original-test.jpg;type=image/jpeg' | ConvertFrom-Json
$postId = $upload.id
Invoke-RestMethod -Uri "$base/content/$postId`?account_id=$accountId" -Headers $headers
Invoke-RestMethod -Method Patch -Uri "$base/content/$postId`?account_id=$accountId" -Headers $headers -ContentType 'application/json' -Body '{"status":"APPROVED"}'
```

Upload creates an ORIGINAL candidate and REVIEW content; approval is separate. Review supports REVIEW/APPROVED/REJECTED and optional caption; once a publishing job exists, review is locked. Inspect generated/uploaded images using authenticated `GET /content/{id}/image?account_id=UUID`, which works privately without a public HTTPS host. API listing responses contain no bearer media URL. Duplicate normalized image hashes are rejected per account.

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
- Check global switch, pause, approval/rights, due slot, encrypted token expiry and API-reported quota before publishing. The app cap defaults to25 and is constrained to<=50 because current Meta docs conflict on50/100 limits. The scheduler's daily target is separate from this safety cap.
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

## Insights And Monitoring

`POST /api/v1/jobs/analytics` accepts account-scoped idempotent jobs. n8n triggers it daily; hourly Celery housekeeping also queues it to collect closer to the24h/72h/7d checkpoints. Each job collects at most10 snapshots. Unsupported/missing metrics remain null, never zero. The fixed checkpoint is the dedup key; actual observation time/age is recorded separately. After downtime, only the latest due checkpoint is collected: earlier historical observations are never invented.

Read authenticated `GET /api/v1/analytics/summary?account_id=UUID&days=14` for same-age cohorts, reach-based share/save/engagement rates and AI API-cost accounting. Zero/unknown reach yields null rates; a missing engagement component yields unknown engagement. Observations more than12h late are excluded from comparable cohort totals. Fewer than20 comparable posts is flagged exploratory, not proof of a winning strategy. Local API fees are0; hardware/electricity are not included in that value.

Hourly housekeeping persists one versioned report/account/UTC ISO week; read it with `GET /api/v1/analytics/weekly?account_id=UUID`. Reports provide cohort evidence and controlled-experiment guidance, not automatic prompt/strategy changes. Use strategy prompt versions and vary one topic/slot at a time. No claims of virality are made.

`GET /api/v1/monitoring` supplies the sixth workflow with failed/stale jobs, uncertain publications, expiring tokens and unauthorized active accounts. Paused accounts are excluded from stale-job recovery. Tokens expiring within7d are refreshed only if unexpired and at least24h old; expiry or unknown acquisition needs reauthorization. Manual refresh remains available. Keep beat and worker running for housekeeping; monitoring alerts still require configured SMTP and n8n.

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m compileall -q app integrations migrations tests
.\.venv\Scripts\python -m pip check
```

Tests cover migrations/model parity/roundtrips/PostgreSQL offline DDL, account isolation, OAuth, publishing uncertainty/recovery, local AI protocol and safety, image rendering/dedup, signed storage/previews, scheduling, durable jobs, insights/missingness, reports, monitoring and backup command safety. Actual PostgreSQL advisory-lock tests are opt-in using `TEST_POSTGRES_URL`; they skip without a PostgreSQL16 test server. Real Redis delivery, Docker networking, n8n and model inference remain outside the local unit result. Run Compose smoke for real service delivery. Dependencies are bounded, not transitively locked; container tags other than n8n track patch updates.

`.github/workflows/tests.yml` is manual-only, defining Python3.12/3.13 unit jobs, real PostgreSQL16 lock tests and Compose smoke/n8n readiness with ephemeral credentials and publishing disabled. CI has not run here. Verify account quotas and prohibit paid overages before manually running it.

## Security And Operations

API and n8n ports bind only to `127.0.0.1`. PostgreSQL and Redis have no published ports and use an internal Docker network. API/worker additionally join the egress network for Meta/Supabase; migration/beat remain internal-only. n8n has egress but no Meta tokens or token-encryption key. API/worker run as a non-root user. Uvicorn access logs are disabled; HTTPX/httpcore logging is suppressed to avoid OAuth query-string leakage. Do not enable request/response tracing. Validation and exception responses are sanitized. Do not put tokens in job payloads, errors, audit details or logs.

Use an existing HTTPS reverse proxy that exposes only `GET /api/v1/oauth/instagram/callback` and `GET /media/*`; all other API routes stay private. Disable query/access logs for both paths. Limit multipart bodies near9MiB on the private admin ingress: parsing can spool requests before route validation. Local signed media URLs expire after1h; rotating the signing/encryption key invalidates them but also affects encrypted account tokens, so do not rotate it casually. Supabase signed URL revocation differs; its adapter is optional, not the default.

This Compose setup is local development, not a hardened production deployment: the application database role also owns migrations; Redis trusts the isolated backend network; n8n HTTP cookies are permitted only for localhost. Before production, separate migration/runtime roles, configure authenticated TLS service access as needed, use HTTPS reverse proxy/access control for n8n, enable secure cookies, and expose only narrowly scoped OAuth callbacks rather than the internal API.

Compose waits for healthy PostgreSQL before migrations and successful migration completion before API/worker startup. Redis uses AOF. PostgreSQL, Redis, n8n and local media have separate named volumes. `docker compose down` preserves data; **do not use `down -v` unless intentionally deleting all data**.

Back up PostgreSQL with `pg_dump` (run inside the database container), copy the resulting dump to encrypted external storage, and verify restore into a separate database. Back up the n8n volume while n8n is stopped, together with its encryption key; protect the application Fernet key separately. Volume persistence is not a backup. Restore procedures have not been exercised in this environment.

The Python backup CLI avoids Windows PowerShell binary-pipeline corruption. From the project directory, with a pre-existing `backups` directory on a protected/encrypted drive:

```powershell
.\.venv\Scripts\python -m app.backup backup .\backups\snapshot-20261028.dump
docker compose exec -T db createdb -U meme meme_restore_check
.\.venv\Scripts\python -m app.backup restore-check .\backups\snapshot-20261028.dump meme_restore_check
docker compose exec -T db psql -U meme -d meme_restore_check -c "SELECT version_num FROM alembic_version;"
```

The restore-check command refuses the application database and uses no destructive clean/drop option. Use a newly created empty verification database for each drill. Backups contain encrypted tokens plus private content and need access controls/encrypted storage; do not upload to paid backup services. The CLI command construction/file handling is tested, not a real dump/restore. Back up local media and n8n separately during a maintenance pause; verify database/media consistency before any production recovery.

Before production, keep the kill switch off, complete Compose smoke and a real local-model generation/review, configure private admin access and the narrow HTTPS callback/media proxy, verify licenses/free-tier limits, then authorize one account for one intentional test post. Only after confirming actual Instagram success should the other two accounts and n8n publishing be enabled. Separate DB migration/runtime roles, restrict network egress to approved feeds/Meta, harden TLS/n8n cookies, pin audited dependency/image versions and perform backup/restore drills. This repository does not provision a domain, hosting, TLS service or paid infrastructure.

Troubleshooting:

- `docker` not found: install Docker Desktop, reopen the terminal, and verify `docker version` and `docker compose version`.
- Migration exits nonzero: inspect `docker compose logs migrate`; fix the cause and rerun `docker compose up -d`. Never bypass failed migrations.
- API unhealthy: check generated keys, database/Redis health and `docker compose logs api`. Placeholder API/Fernet keys intentionally fail validation.
- Password changes do not update an existing PostgreSQL volume automatically; rotate the database user's password deliberately rather than deleting data.
- Worker smoke timeout: inspect `docker compose logs worker redis` and worker health. Never interpret a queued task as completed work.
- n8n cookie/login problems over remote HTTP: this configuration is localhost-only. Deploy HTTPS rather than exposing insecure HTTP.

n8n is pinned to `2.45.4` based on official Docker documentation research supplied for this milestone: https://docs.n8n.io/hosting/installation/docker/ . `GENERIC_TIMEZONE=Asia/Jakarta` and settings file permissions are configured. Deprecated `N8N_RUNNERS_ENABLED` is intentionally omitted. No n8n Code nodes or Python runners are required.
