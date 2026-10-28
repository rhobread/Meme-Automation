# Implementation progress

## Status
Cost restriction: no additional subscriptions or paid services beyond ChatGPT Plus ($20/month). Plus does NOT include OpenAI API or establish Kilo billing coverage. User authorized continuing with local-only defaults. No live OpenAI/Meta/Supabase calls, model downloads, paid provisioning, deployments or CI executions performed. OpenAI remains blocked by `ALLOW_PAID_SERVICES=false`; Compose hard-disables paid services and selects Ollama/local storage. CI remains manual-only. No new agents launched after the cost restriction.

Cost guard verification: `.\.venv\Scripts\python -m pytest tests/test_cost_guard.py -q` passed, 2 tests in 0.90s. Checks default paid-services denial and prevents OpenAI client construction without spending authorization. The entire suite has not been rerun after this guard and concurrent incomplete Milestone 3 changes.

Milestones 1-5 implemented with local automated verification on Python3.13.14. Final acceptance remains pending for Docker/Python3.12, real PostgreSQL locking/Redis delivery, actual local-model inference, n8n/SMTP, HTTPS and authorized Instagram publishing. Optional dashboard/Reels/carousel are not implemented. No paid services enabled or provisioned.

## Milestones
- [x] 1. Workspace, config, Docker Compose, PostgreSQL migrations, Redis, FastAPI health, Celery, n8n, automated smoke test (implemented; real Compose smoke execution pending)
- [x] 2. Meta OAuth and single image publishing (implemented and mock-HTTP workflow tested; live authorization/storage/publication still unverified)
- [x] 3. Source adapters, candidate dedup, local structured AI, renderer, private local storage and quality gates (mock-HTTP inference tested; actual model setup pending)
- [x] 4. Multi-account scheduling, durable jobs, idempotency, 6 n8n exports (real PostgreSQL concurrency and n8n runtime acceptance pending)
- [x] 5. Insights snapshots, weekly reports, monitoring/alert workflow, token housekeeping, deployment/backup guide (live acceptance pending)
- [ ] 6. Optional dashboard, Reels and carousel

## Verified commands/results
Executed from `C:\Personal Projects\Meme Automation` on 2026-10-28:

- `Test-Path -LiteralPath "C:\Personal Projects\Meme Automation"`: True.
- `python -m venv .venv`: succeeded.
- `.\.venv\Scripts\python -m pip install -e ".[test]"`: succeeded, installed application/test dependencies.
- `$env:DATABASE_URL='sqlite://'; .\.venv\Scripts\alembic revision --autogenerate --rev-id 0001 -m initial_schema`: generated frozen explicit initial migration; reviewed generated file. This was development-time generation only, not an instruction to regenerate the migration on deployment.
- `.\.venv\Scripts\python -m pytest -q`: initially 11 passed; final run 13 passed in 1.56 seconds. One upstream Starlette warning about its current httpx TestClient adapter. Tests cover authenticated health and redaction, encryption/tamper rejection, invalid key rejection, SQLite migration/model parity and downgrade/upgrade, PostgreSQL offline DDL compilation, cross-account ownership, account-scoped dedup, durable-job idempotency, unique per-post publishing job, and Celery eager success/failure.
- `.\.venv\Scripts\python -m compileall -q app migrations tests`: succeeded.
- `.\.venv\Scripts\python -m pip check`: No broken requirements found.
- `.\.venv\Scripts\python --version`: Python 3.13.14.
- `docker version`: failed because `docker` is not recognized. No Docker build, Compose config validation, PostgreSQL server migration, live Redis/Celery delivery, n8n startup, or Python 3.12 execution was performed.

After Docker installation and local `.env` configuration, run `docker compose config --quiet`, `docker compose up --build -d`, `docker compose ps -a`, and `docker compose exec api python -m app.smoke`. These are pending commands, not passed checks.

## Milestone 1 Implementation

- Non-root Python 3.12 image; PostgreSQL 16, persistent Redis AOF, migration-gated API/worker, pinned n8n 2.45.4 with isolated persistent SQLite volume; dependency/service healthchecks.
- API/n8n localhost-only ports; no published DB/Redis ports; internal backend network. M2 adds external egress to API/worker only, in addition to n8n's existing egress.
- Authenticated `/api/v1/health` verifies DB, schema revision and Redis. Real container smoke runner also sends and awaits a Celery task. No fake business-job success paths.
- SQLAlchemy entities for all six core tables plus durable jobs, audit events and hashed OAuth state. Explicit immutable Alembic revision `0001`; UTC-aware PostgreSQL timestamps, ownership foreign keys and scoped unique constraints.
- Fernet token encryption helpers, secret settings validation, no actual secrets in repository, placeholder-only environment example, operational README.
- M1 provided schema foundations only. M2 now implements durable manual publish dispatch/recovery, atomic OAuth state consumption and conservative publishing reconciliation. Full scheduling/lease orchestration and database-enforced audit append-only policy remain future work.
- Dependency ranges are bounded but not transitively locked; PostgreSQL/Python/Redis image tags track patches. Live production hardening and backup/restore drills are not verified.

## Blocking external prerequisites
- Meta Developer App and three authorized Instagram Professional accounts
- Meta OAuth approval/permissions as applicable
- Locally installed Ollama/model, sufficient RAM/CPU or GPU and verified model license; no OpenAI key required
- Local persistent media volume; external storage credentials not required for default local mode
- Server/domain/HTTPS for live OAuth and hosting

## Open decisions
- n8n pinned to 2.45.4 from official Docker documentation research supplied by the parent; runtime unverified. M2 Meta Graph defaults to configurable v26.0. OpenAI model selection remains outside M2.
- Source discovery feeds and media licenses: choose permitted sources.

## Milestone 2 Implementation

- Read complete AGENTS/context/progress and existing `integrations/meta.py`; the independently tested Meta adapter was not rewritten. Rechecked official content-publishing and Supabase upload/download documentation; supplied official OAuth/container/changelog research informs identity, refresh and reconciliation behavior. README links the official references.
- `app.api`, `app.auth`, `app.services`: protected account GET/POST/PATCH with strategy create/partial update; protected account-scoped OAuth start and refresh; sole public callback protected by 256-bit random state stored only as SHA-256, ten-minute expiry and atomic single-use UPDATE RETURNING. Callback checks exchange/profile identity, rejects conflicting existing identity and encrypts long-lived tokens. Refresh checks unexpired token and >=24-hour acquisition age.
- Frozen migration `0002` adds nullable `token_acquired_at`; health and real service smoke derive the expected revision dynamically from Alembic head. Existing tokens with unknown acquisition require reauthorization before refresh.
- `POST /accounts/{id}/test-image` validates actual JPEG bytes through Pillow, <=8 MiB, width 320-1440 and ratio 4:5..1.91:1, EXIF orientation and sRGB color conversion; normalized metadata-free JPEG hash/key are recorded. Explicit operator rights attestation is required to mark ORIGINAL; upload leaves content in REVIEW, and PATCH approval is separate. Content/job access filters account IDs, including rejection of conflicting path/query/body/form scope.
- `integrations.storage.StorageClient`: real httpx Supabase REST upload/signing, server-only service credentials, private-bucket verification before upload/signing, no overwrite, one-hour signed URL generated immediately before container ingestion. No production mock success path.
- `app.publishing`: persisted 202 job acceptance, stable per-post idempotency, database outbox recovery after failed broker submission, per-account PostgreSQL session advisory locking across commits plus row locks. Persisted container reuse; bounded once-per-minute next-attempt polling; pause/kill-switch/rights/token/quota checks before external publishing side effects. Configurable conservative cap defaults to25/max50 and also respects the live reported quota.
- Before calling Meta publish, commit UNCERTAIN; timeouts, crashes and errors after that boundary never trigger another publish. Authenticated reconciliation only reads container status. PUBLISHED without media ID remains unresolved until explicit operator verification of the exact ID/account/container; no heuristic matching. Uncertain jobs block other new publications on that account. Failed/exhausted jobs are terminal, not silently reset.
- `app.worker` adds publish/recovery tasks, late acknowledgements, safe 240/300-second task limits and minute recovery schedule. Compose adds a separate internal-only beat process; API/worker gain egress, without exposing DB/Redis ports. Runtime packaging and Docker COPY now include `integrations*`.
- `.env.example` has blank external credentials, configurable Meta version/quota/private bucket, publishing disabled by default. HTTPX/httpcore logging suppressed, Uvicorn access logging remains disabled, all error/validation responses sanitized, secrets excluded from audit/job payloads. README includes exact Windows account/OAuth/upload/approve/publish/status/pause/refresh/reconcile workflow, proxy restrictions and recovery limitations.
- Parent added `.github/workflows/tests.yml`: Python3.12/3.13 tests plus Compose smoke/n8n readiness with ephemeral secrets and publishing disabled. CI has not run here; no git or remote operations were performed.

## Milestone 2 Verification

Executed locally on 2026-10-28:

- `Test-Path -LiteralPath ".venv"`: True.
- `.\.venv\Scripts\python -m pip install -e ".[test]"`: succeeded; Pillow12.3.0 and python-multipart0.0.32 installed in existing project venv only. No global dependency installation.
- `.\.venv\Scripts\python -m pytest -q`: M1+Meta baseline after integration53 passed; initial M2 tests83 passed; expanded suite95 passed (latest recorded full run6.40 seconds). One upstream Starlette/httpx TestClient deprecation warning.
- Tests use real application services, SQLite database transactions/constraints and real Meta/Supabase adapters with mocked HTTP transport, plus mocked external broker submission. Cover full account/OAuth/upload/review/async publish/status path, three-account isolation, refresh age/expiry, concurrent state consume, identity replacement rejection, JPEG format/dimension/size/rights/private-bucket gates, persisted uncertainty before HTTP publish, timeouts/restarts, broker failure/recovery, pause/kill/token/quota gates, bounded polling/container reuse, ERROR/EXPIRED/PUBLISHED handling, audited manual reconciliation and secret redaction.
- `.\.venv\Scripts\python -m compileall -q app integrations migrations tests`: succeeded.
- `.\.venv\Scripts\python -m pip check`: No broken requirements found.
- Existing migration tests now verify0001->0002 model parity, downgrade/upgrade and offline PostgreSQL DDL compilation.

Not executed: real PostgreSQL advisory-lock/concurrency acceptance, Redis/Celery/beat delivery, Docker build/Compose startup, n8n runtime, Python3.12 execution, live OAuth, real private storage upload/signing, Meta container/publish/quota calls or real three-account acceptance. Docker remains unavailable and no live external credentials/approval were supplied. The documented operator workflow is tested with mocked external HTTP, not claimed as a live Instagram test post.

Operational limitations deliberately left for subsequent milestones: full scheduling/fair multi-account queues, automatic token maintenance, audit database append-only enforcement, orphan storage cleanup, production DB roles/TLS/backups and dependency locking. A crash before a publish call but after its uncertainty commit intentionally requires manual investigation rather than an unsafe automatic retry.

## Milestone 3 Implementation And Verification

- Completed saved partial M3 code: RSS/Atom ideas-only adapter, normalization/fingerprints, per-account dedup, permission metadata, structured weighted assessment, generated Indonesian captions/alt text, original primitive/text templates, durable checkpoint/retry jobs and AI usage ledger. Frozen migration0003 adds generation metadata, job retry timestamp and AIUsage.
- Added loopback-only Ollama `/api/chat` adapter with JSON Schema and Pydantic validation, no redirects/proxies/model pulls or cloud fallback; cloud-tagged models rejected. Official docs checked: https://docs.ollama.com/capabilities/structured-outputs and https://docs.ollama.com/faq . Operator must set `OLLAMA_NO_CLOUD=1` on the Ollama server and choose a genuinely local licensed model.
- Local safety assessment is not OpenAI moderation and cannot guarantee safety/rights. Every locally generated post requires manual review even if autoapproval flags are enabled. Malformed/truncated outputs, flags, rights uncertainty, feed permission failures and text overflow reject or fail closed.
- Added private filesystem storage, atomic no-overwrite uploads/replays, traversal checks, expiring HMAC-signed HTTPS media URLs and `/media/{key}` bearer-authenticated retrieval. Local uploads work without an HTTPS origin; signing for Meta requires an operator-provided reachable HTTPS origin. API, DB, Redis and Ollama must not be publicly exposed.
- Compose defaults local AI/storage, persistent media volume with non-root ownership, paid services false; generic job time limits/lease accommodate bounded local inference. No actual model downloaded or run.
- `\.venv\Scripts\python -m pytest -q` (with leading `.` in PowerShell): 160 passed,2 skipped,1 upstream Starlette/httpx warning in9.27s. Covers migrations/model parity, M1/M2 regressions, Ollama protocol/validation/safety, private media signing/tamper/expiry, feed permission/dedup/XML/private-address checks, generation/replay, local API-cost ledger0 (not electricity), image dimensions/overflow, standalone schedule/insights modules and workflow structure.
- Real PostgreSQL advisory-lock acceptance tests added under `tests/test_postgres_integration.py`, opt-in `TEST_POSTGRES_URL`, skipped locally because no PostgreSQL service. CI includes a PostgreSQL16 lock-test job but has not run. Six n8n exports have31 structural tests; import/execution/SMTP are unverified.

## Milestone 4 Implementation And Verification

- Connected `/jobs/schedule` and `/jobs/publish-due` to durable persisted202 jobs, general status, database outbox recovery, bounded retries and operator retry for failed non-publishing jobs only. Existing account advisory lock is reused within generic handlers, avoiding nested-connection lock conflicts. Checkpoints survive failures; paused accounts do not consume attempts.
- Scheduler reserves today/tomorrow local-time slots, converts to UTC, counts manual publications toward daily target and rejects malformed/insufficient slots. Defaults match office12/19, gaming13/21, IT11:30/20 Jakarta. Three accounts reserve6/day. Per-account schedule slots and exact media hashes are unique in DB; frozen migration0004 adds image dedup index. Source candidates and content lifecycle transitions are synchronized.
- Due scheduled posts pass the same kill/pause/token/rights/quota/uncertainty gates as manual posts. Future slots cannot publish early. Posts missed by more than1h return to review instead of catch-up bursts. A per-post durable publish job/container is reused; uncertain publish outcomes never retry blindly.
- Six inactive `n8n/*.json` exports contain only scheduling/filtering/HTTP/alerts. Header Auth credential placeholders keep keys out of exports. Asia/Jakarta cron schemas checked against official pinned n8n2.45.4 source. Monitoring uses configurable SMTP credentials and addresses; it is not a paid alert-service dependency.
- `.\.venv\Scripts\python -m pytest -q`:167 passed,2 PostgreSQL skips,1 upstream warning in9.55s. Includes6/day/account isolation, daily manual-count target, missed slots, real safety gates, idempotent outbox, generic crash/checkpoint/retry/recovery and SQL image uniqueness. Live PostgreSQL concurrency/n8n import/SMTP remain unverified.

## Milestone 5 Implementation And Verification

- Added durable analytics jobs and account-scoped summary/weekly APIs. Snapshots target24h/72h/7d with actual observation age/time, per-post checkpoint dedup and no invented historical backfill. Missing metrics remain null; zero reach yields unknown rates. Only same-age/on-time observations contribute comparable totals; small samples warn instead of auto-optimizing.
- Hourly housekeeping queues isolated insights/token maintenance and versioned weekly report jobs; weekly evidence/experiment guidance persists in job results. Refresh uses unexpired >=24h-old tokens within7d of expiry, validates identity and encrypts replacements. Worker/beat housekeeping and Meta insights remain mock-tested, not live.
- Monitoring API supplies failed/stale jobs, uncertainty, expiring tokens and unauthorized active accounts to the SMTP alert workflow. Paused accounts are filtered from recovery/stale counts so they do not consume other accounts' dispatch capacity. No email or cloud alert subscription provisioned.
- Frozen migration0005 adds database append-only audit triggers. Added authenticated private image previews independent of public HTTPS, candidate/content lifecycle synchronization, and publishing alt text forwarding.
- Python backup CLI uses binary-safe pg_dump, immutable backup filenames and isolated restore-check databases, refusing the application DB. Mocked command/file tests pass; no real dump or restore performed. README includes exact Windows commands, existing-HTTPS requirements, cost guard/local model setup, n8n credentials, recovery and production hardening.
- Verification caught an indentation error in a publishing edit; fixed before final suite. `.\.venv\Scripts\python -m pytest -q`:177 passed,2 PostgreSQL skips,1 upstream Starlette/httpx warning in10.91s (latest recorded full suite before documentation-only changes and settings error-hiding guard). `compileall -q app integrations migrations tests`: passed. `pip check`: no broken requirements. `git diff --check -- .`: no whitespace errors; Git reports LF->CRLF notices only.
- Not executed: Docker Compose config/build/startup, actual Python3.12, PostgreSQL server migrations/trigger/lock checks, real broker delivery, n8n import/execution/SMTP, model download/inference, live OAuth/media URL fetching/publishing/insights, backup/restore drill. No live Instagram post or three-account authorization claimed. Runtime prerequisites are listed above; all code remains uncommitted.

Final rerun after all code changes: `.\.venv\Scripts\python -m pytest -q` =>178 passed,2 PostgreSQL skips,1 upstream deprecation warning in10.89s; `compileall -q app integrations migrations tests` passed; `pip check` found no broken requirements. Added queued-worker-restart stale-slot terminal safety, conservative8,000,000-byte JPEG cap, settings-error input hiding, and ignored local media/backups/env variants to avoid accidental private-data commits. Source context now records the user's local-only cost override. No additional external calls or paid resources were used for acceptance.

## Remaining Operator Acceptance

- Install an eligible free container runtime and run Compose smoke; install/configure a licensed local Ollama model with `OLLAMA_NO_CLOUD=1`, then evaluate generation quality and review actual rendered images.
- Configure only permitted idea feeds and an existing reachable HTTPS origin limited to OAuth callback/signed media. Keep internal API, database, Redis, Ollama and n8n administration private.
- Register/approve Meta app, authorize3 professional accounts separately, configure credentials locally, and intentionally publish one manually reviewed test image before enabling the workflows/kill switch.
- Configure no-cost SMTP alerts; verify n8n execution, real PostgreSQL concurrent worker behavior, expiry/rate-limit/recovery drills and encrypted backups. Check billing/license/free-tier limits independently; ChatGPT Plus does not pay for Kilo, API calls, hosting, hardware or electricity.
- Production runtime DB-role separation, transitive dependency locking, orphan storage cleanup and optional dashboard/Reels/carousel remain outside this local MVP implementation. Audit triggers do not protect against a database owner deliberately disabling them. RSS sources are operator-trusted/allowlisted with private-address preflight, not a substitute for production egress restrictions against DNS rebinding.
