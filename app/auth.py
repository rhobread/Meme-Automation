import secrets
from typing import Annotated

from fastapi import Header, HTTPException
from app.config import get_settings


def authenticate(x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().internal_api_key.get_secret_value()
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "Invalid API key")
