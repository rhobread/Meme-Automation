"""Synchronous Instagram Login adapter. No request is automatically retried.

Callers own OAuth state validation, token storage, refresh eligibility, and
publication reconciliation. FINISHED means ready, not published.
"""

from dataclasses import dataclass, field
from enum import StrEnum
import math
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx


class ErrorKind(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    AMBIGUOUS_PUBLISH = "ambiguous_publish"
    INVALID_RESPONSE = "invalid_response"
    API = "api"


class MetaError(Exception):
    """Safe to log: never retains a request, response, or upstream message."""

    def __init__(self, kind: ErrorKind, status_code: int | None = None):
        self.kind = kind
        self.status_code = status_code
        super().__init__(f"Meta request failed: {kind.value}")


@dataclass(frozen=True)
class Token:
    access_token: str = field(repr=False)
    expires_in: int
    user_id: str | None = None


@dataclass(frozen=True)
class Profile:
    user_id: str
    username: str


class ContainerStatus(StrEnum):
    FINISHED = "FINISHED"
    IN_PROGRESS = "IN_PROGRESS"
    PUBLISHED = "PUBLISHED"
    ERROR = "ERROR"
    EXPIRED = "EXPIRED"


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise MetaError(ErrorKind.INVALID_RESPONSE)
    return value


def _identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MetaError(ErrorKind.INVALID_RESPONSE)
    value = str(value)
    if not re.fullmatch(r"[0-9]+", value):
        raise MetaError(ErrorKind.INVALID_RESPONSE)
    return value


def _single(data: dict[str, Any]) -> dict[str, Any]:
    if "data" not in data:
        return data
    items = data["data"]
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise MetaError(ErrorKind.INVALID_RESPONSE)
    return items[0]


class MetaClient:
    SCOPES = (
        "instagram_business_basic",
        "instagram_business_content_publish",
        "instagram_business_manage_insights",
    )
    METRICS = ("views", "reach", "likes", "comments", "saved", "shares", "total_interactions")

    def __init__(
        self, app_id: str, app_secret: str, redirect_uri: str,
        version: str = "v26.0", *, client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None, timeout: float = 30.0,
    ):
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
            raise ValueError("Invalid Meta API version")
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("Timeout must be between zero and 120 seconds")
        if client is not None and transport is not None:
            raise ValueError("Provide client or transport, not both")
        self.app_id = app_id
        self._app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.base_url = f"https://graph.instagram.com/{version}"
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(transport=transport)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MetaClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def authorize_url(self, state: str, *, force_reauth: bool = True) -> str:
        if not state:
            raise ValueError("OAuth state is required")
        return "https://www.instagram.com/oauth/authorize?" + urlencode({
            "client_id": self.app_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": ",".join(self.SCOPES),
            "state": state, "enable_fb_login": "0",
            "force_reauth": "1" if force_reauth else "0",
        })

    def _request(self, method: str, url: str, *, token: str | None = None,
                 params: dict[str, Any] | None = None,
                 data: dict[str, Any] | None = None,
                 publish: bool = False) -> dict[str, Any]:
        # Never propagate httpx exceptions: their URLs can contain OAuth secrets.
        failure = None
        response = None
        try:
            response = self._client.request(
                method, url, params=params, data=data,
                headers={"Authorization": f"Bearer {token}"} if token else {},
                timeout=self._timeout, follow_redirects=False,
            )
        except httpx.RequestError:
            failure = MetaError(ErrorKind.AMBIGUOUS_PUBLISH if publish else ErrorKind.TRANSIENT)
        if failure is not None:
            raise failure
        assert response is not None
        status = response.status_code
        payload = None
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            pass
        payload = payload if isinstance(payload, dict) else None
        error = payload.get("error") if payload else None
        error_data = error if isinstance(error, dict) else {}
        code = error_data.get("code")
        if publish and status >= 500:
            raise MetaError(ErrorKind.AMBIGUOUS_PUBLISH, status)
        if status in (401, 403) or code in (102, 190) or (payload and payload.get("error_type") == "OAuthException"):
            raise MetaError(ErrorKind.AUTH, status)
        if status == 429 or code in (4, 17, 32, 613, 80002):
            raise MetaError(ErrorKind.RATE_LIMIT, status)
        if status >= 500 or error_data.get("is_transient") is True or code in (1, 2):
            raise MetaError(ErrorKind.TRANSIENT, status)
        if not 200 <= status < 300 or error is not None or (payload and "error_message" in payload):
            raise MetaError(ErrorKind.API, status)
        if payload is None:
            raise MetaError(ErrorKind.AMBIGUOUS_PUBLISH if publish else ErrorKind.INVALID_RESPONSE, status)
        return payload

    @staticmethod
    def _token(data: dict[str, Any], user_id: str | None = None) -> Token:
        expires = data.get("expires_in")
        if type(expires) is not int or expires <= 0:
            raise MetaError(ErrorKind.INVALID_RESPONSE)
        return Token(_text(data, "access_token"), expires, user_id)

    def exchange_code(self, code: str) -> Token:
        short = _single(self._request("POST", "https://api.instagram.com/oauth/access_token", data={
            "client_id": self.app_id, "client_secret": self._app_secret,
            "grant_type": "authorization_code", "redirect_uri": self.redirect_uri, "code": code,
        }))
        token = _text(short, "access_token")
        user_id = _identifier(short.get("user_id"))
        long = self._request("GET", "https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": self._app_secret, "access_token": token,
        })
        return self._token(long, user_id)

    def refresh(self, access_token: str) -> Token:
        """Caller must ensure token is unexpired and at least 24 hours old."""
        return self._token(self._request("GET", "https://graph.instagram.com/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": access_token,
        }))

    def get_profile(self, access_token: str) -> Profile:
        data = _single(self._request("GET", f"{self.base_url}/me", token=access_token,
                                     params={"fields": "user_id,username"}))
        return Profile(_identifier(data.get("user_id")), _text(data, "username"))

    def create_jpeg_container(self, user_id: str, access_token: str, image_url: str,
                              caption: str, *, alt_text: str | None = None) -> str:
        """The caller must supply a public-fetchable JPEG; no extension is required."""
        url = None
        try:
            url = urlsplit(image_url)
        except ValueError:
            pass
        if url is None:
            raise ValueError("Invalid image URL")
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Image URL must be HTTPS without credentials")
        data = {"image_url": image_url, "caption": caption}
        if alt_text is not None:
            data["alt_text"] = alt_text
        result = self._request("POST", f"{self.base_url}/{_identifier(user_id)}/media",
                               token=access_token, data=data)
        return _identifier(result.get("id"))

    def get_status(self, container_id: str, access_token: str) -> ContainerStatus:
        result = self._request("GET", f"{self.base_url}/{_identifier(container_id)}",
                               token=access_token, params={"fields": "status_code,status"})
        status = result.get("status_code")
        if not isinstance(status, str) or status not in {item.value for item in ContainerStatus}:
            raise MetaError(ErrorKind.INVALID_RESPONSE)
        return ContainerStatus(status)

    def publish(self, user_id: str, access_token: str, container_id: str) -> str:
        result = self._request("POST", f"{self.base_url}/{_identifier(user_id)}/media_publish",
                               token=access_token, data={"creation_id": _identifier(container_id)}, publish=True)
        # A successful HTTP response without a valid media ID is also uncertain.
        media_id = result.get("id")
        if not isinstance(media_id, (str, int)) or isinstance(media_id, bool) or not re.fullmatch(r"[0-9]+", str(media_id)):
            raise MetaError(ErrorKind.AMBIGUOUS_PUBLISH)
        return str(media_id)

    def publishing_limit(self, user_id: str, access_token: str) -> dict[str, Any]:
        result = self._request("GET", f"{self.base_url}/{_identifier(user_id)}/content_publishing_limit",
                               token=access_token, params={"fields": "quota_usage,config"})
        data = _single(result)
        if type(data.get("quota_usage")) is not int or data["quota_usage"] < 0:
            raise MetaError(ErrorKind.INVALID_RESPONSE)
        config = data.get("config")
        if not isinstance(config, dict) or any(type(config.get(key)) is not int or config[key] <= 0 for key in ("quota_total", "quota_duration")):
            raise MetaError(ErrorKind.INVALID_RESPONSE)
        return {"quota_usage": data["quota_usage"], "config": {key: config[key] for key in ("quota_total", "quota_duration")}}

    def media_insights(self, media_id: str, access_token: str) -> dict[str, int | float]:
        result = self._request("GET", f"{self.base_url}/{_identifier(media_id)}/insights",
                               token=access_token, params={"metric": ",".join(self.METRICS)})
        items = result.get("data")
        if not isinstance(items, list):
            raise MetaError(ErrorKind.INVALID_RESPONSE)
        metrics = {}
        for item in items:
            if not isinstance(item, dict):
                raise MetaError(ErrorKind.INVALID_RESPONSE)
            name = _text(item, "name")
            values = item.get("values")
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
                raise MetaError(ErrorKind.INVALID_RESPONSE)
            value = values[0].get("value")
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or name in metrics:
                raise MetaError(ErrorKind.INVALID_RESPONSE)
            metrics[name] = value
        return metrics
