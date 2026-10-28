import pytest
from cryptography.fernet import InvalidToken
from pydantic import ValidationError

from app.config import Settings
from app.security import decrypt_token, encrypt_token


def test_token_roundtrip():
    encrypted = encrypt_token("sensitive-token")
    assert b"sensitive-token" not in encrypted
    assert decrypt_token(encrypted) == "sensitive-token"
    with pytest.raises(InvalidToken):
        decrypt_token(encrypted[:-1] + b"!")


def test_placeholder_rejected():
    with pytest.raises(ValidationError):
        Settings(internal_api_key="replace-with-at-least-32-random-characters")
