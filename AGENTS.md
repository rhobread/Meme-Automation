# Kilo project instructions — Instagram AI Meme Automation

Read `PROJECT_CONTEXT.md` and `IMPLEMENTATION_PROGRESS.md` before implementing anything. These are the project source of truth. Maintain `IMPLEMENTATION_PROGRESS.md` after each milestone.

## Working principles
- Build a working, tested repository; do not stop at advice or pseudocode.
- Work iteratively in vertical slices: infrastructure -> one Instagram test post -> AI generation -> scheduler -> three accounts -> analytics.
- Implement actual files, migrations, API routes, worker tasks, and n8n exportable workflows. Avoid stubbed success responses for essential production paths.
- Never claim an external integration works unless it has been tested. If credentials or app approval are missing, document the blocker and provide a mock/test path.
- Ask for credentials only through local environment variables; never request them in the chat, print them, or commit them.
- Keep secrets server-side; encrypt Instagram access tokens at rest; redact logs.
- Use official Meta APIs; no browser-based Instagram posting bots, evasion, spam or copying third-party meme media without rights.
- Enforce account isolation, idempotent scheduling and conservative publishing retries to avoid duplicate posts.
- Use Python 3.12, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Celery + Redis, n8n, Pillow, httpx, OpenAI API. Docker Compose for local setup.
- Put all operational code in Python. n8n only schedules and orchestrates HTTP requests; no business logic in n8n.
- Prefer small, testable modules, typed interfaces, clear error handling, structured logs and pytest.
- Check current official docs for Meta permissions, OAuth, publishing endpoints and current n8n config before coding integrations; make API versions configurable.
- Use timezone `Asia/Jakarta` for user-facing schedules and UTC timestamps in DB.
- Keep a `README.md` with exact Windows VS Code + Docker steps, local commands, and troubleshooting.
- Run tests and report exactly what was executed and what still requires user action.

## Start here
1. Inspect workspace, read context and progress files.
2. Produce a short implementation milestone plan and begin Phase 1 without repeatedly asking for permission.
3. Create Docker, schema/migrations, backend health endpoint, and smoke tests. Then continue to real Meta integration with a safe, manual test mode.
