import pytest
from cryptography.fernet import Fernet

from app.config import get_settings


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key-" + "a" * 40)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
