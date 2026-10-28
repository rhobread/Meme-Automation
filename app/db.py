from functools import lru_cache

from sqlalchemy import Engine, create_engine

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3, "options": "-c timezone=UTC -c statement_timeout=5000"},
    )
