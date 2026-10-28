import logging

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from alembic.config import Config
from alembic.script import ScriptDirectory
from redis import Redis
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine
from app.auth import authenticate
from integrations.meta import MetaError
from integrations.storage import StorageError


for name in ("httpx", "httpcore"):
    logging.getLogger(name).setLevel(logging.CRITICAL)


def schema_head():
    return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()


def check_dependencies() -> dict[str, str]:
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if revision != schema_head():
            raise RuntimeError("Database revision mismatch")
    with Redis.from_url(get_settings().redis_url.get_secret_value(), socket_connect_timeout=3, socket_timeout=3) as redis:
        redis.ping()
    return {"database": "ok", "redis": "ok", "schema": revision}


app = FastAPI(title="Meme Automation Internal API", docs_url=None, redoc_url=None, openapi_url=None)

from app.api import router, oauth_router
app.include_router(router)
app.include_router(oauth_router)


@app.get("/media/{key:path}", include_in_schema=False)
def signed_media(key: str, expires: int, signature: str):
    from app.services import storage_client
    if get_settings().storage_provider != "local":
        raise HTTPException(404, "Media unavailable")
    try:
        with storage_client() as storage:
            path = storage.verified_path(key, expires, signature)
    except StorageError:
        raise HTTPException(404, "Media unavailable") from None
    return FileResponse(path, media_type="image/jpeg", headers={"X-Content-Type-Options": "nosniff"})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse(status_code=422, content={"detail": "Invalid request fields"})


@app.exception_handler(MetaError)
@app.exception_handler(StorageError)
async def upstream_error(request, exc):
    return JSONResponse(status_code=502, content={"detail": "External service unavailable"})


@app.middleware("http")
async def safe_errors(request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse(status_code=503, content={"detail": "Operation unavailable"})
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/v1/health", dependencies=[Depends(authenticate)])
def health() -> dict[str, object]:
    try:
        dependencies = check_dependencies()
    except Exception:
        # Do not expose connection URLs, credentials, or driver exception text.
        raise HTTPException(status_code=503, detail="Dependency unavailable") from None
    return {"status": "ok", "dependencies": dependencies}
