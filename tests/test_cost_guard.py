from types import SimpleNamespace

import pytest

from app.config import get_settings
from integrations import ai


def test_paid_services_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_PAID_SERVICES", raising=False)
    get_settings.cache_clear()
    assert get_settings().allow_paid_services is False


def test_openai_client_not_created_without_spending_authorization(monkeypatch):
    monkeypatch.setattr(ai, "OpenAI", lambda **kwargs: pytest.fail("Paid client must not be created"))
    with pytest.raises(ai.AIError):
        ai.AIClient(SimpleNamespace(allow_paid_services=False))
