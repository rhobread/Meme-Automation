import io
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, ContentPost, DurableJob, InstagramAccount, MemeCandidate, PublishingJob


@pytest.fixture
def engine(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "test.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url)
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")
    yield engine
    engine.dispose()


def test_migration_matches_models_and_roundtrip(engine):
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        assert len(inspect(connection).get_table_names()) == 10
    command.downgrade(Config("alembic.ini"), "base")
    command.upgrade(Config("alembic.ini"), "head")


def test_postgresql_migration_compiles(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "FOREIGN KEY(account_id, candidate_id)" in sql
    assert "CREATE TABLE publishing_jobs" in sql


def seed(session):
    a = InstagramAccount(username="a", niche="work")
    b = InstagramAccount(username="b", niche="gaming")
    session.add_all([a, b])
    session.flush()
    candidate = MemeCandidate(account_id=a.id, source_name="original", title="Idea", idea="Original idea", fingerprint="a" * 64)
    session.add(candidate)
    session.commit()
    return a, b, candidate


def test_account_ownership_enforced(engine):
    with Session(engine) as session:
        a, b, candidate = seed(session)
        session.add(ContentPost(account_id=b.id, candidate_id=candidate.id))
        with pytest.raises(IntegrityError):
            session.commit()


def test_account_scoped_dedup(engine):
    with Session(engine) as session:
        a, b, candidate = seed(session)
        session.add(MemeCandidate(account_id=b.id, source_name="original", title="Other", idea="Idea", fingerprint=candidate.fingerprint))
        session.commit()
        session.add(MemeCandidate(account_id=a.id, source_name="original", title="Duplicate", idea="Idea", fingerprint=candidate.fingerprint))
        with pytest.raises(IntegrityError):
            session.commit()


def test_durable_job_idempotency(engine):
    with Session(engine) as session:
        a, b, candidate = seed(session)
        session.add_all([DurableJob(account_id=a.id, kind="discovery", idempotency_key="key"), DurableJob(account_id=b.id, kind="discovery", idempotency_key="key")])
        session.commit()
        session.add(DurableJob(account_id=a.id, kind="discovery", idempotency_key="key"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_publishing_job_unique_per_post(engine):
    with Session(engine) as session:
        a, b, candidate = seed(session)
        post = ContentPost(account_id=a.id, candidate_id=candidate.id, scheduled_at=datetime.now(timezone.utc))
        session.add(post)
        session.flush()
        session.add(PublishingJob(post_id=post.id))
        session.commit()
        session.add(PublishingJob(post_id=post.id))
        with pytest.raises(IntegrityError):
            session.commit()
