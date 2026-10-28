"""Opt-in real PostgreSQL checks; never touch application tables."""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from app import publishing


@pytest.fixture
def postgres_engine(monkeypatch):
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to run real PostgreSQL lock acceptance")
    engine = create_engine(url, pool_size=4, max_overflow=0)
    assert engine.dialect.name == "postgresql", "PostgreSQL test URL required"
    monkeypatch.setattr(publishing, "get_engine", lambda: engine)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_setting('server_version_num')::int")) >= 160000
        yield engine
    finally:
        engine.dispose()


def test_account_lock_survives_commit_and_is_released(postgres_engine):
    account = uuid.uuid4()
    other = uuid.uuid4()
    with publishing.account_session(account) as first:
        assert first is not None
        first.execute(text("SELECT 1"))
        first.commit()
        with publishing.account_session(account) as duplicate:
            assert duplicate is None
        with publishing.account_session(other) as independent:
            assert independent is not None
    with publishing.account_session(account) as recovered:
        assert recovered is not None


def test_account_lock_released_on_exception(postgres_engine):
    account = uuid.uuid4()
    with pytest.raises(RuntimeError, match="worker stopped"):
        with publishing.account_session(account) as session:
            session.execute(text("SELECT 1"))
            raise RuntimeError("worker stopped")
    with publishing.account_session(account) as recovered:
        assert recovered is not None
