from urllib.parse import parse_qs, urlsplit
import traceback

import httpx
import pytest

from integrations.meta import ContainerStatus, ErrorKind, MetaClient, MetaError


def client(handler, **kwargs):
    return MetaClient("123", "secret-value", "https://app.example/callback",
                      transport=httpx.MockTransport(handler), **kwargs)


def test_authorize_url():
    with client(lambda request: httpx.Response(200)) as meta:
        url = urlsplit(meta.authorize_url("csrf & value"))
        assert url.scheme == "https"
        assert url.netloc == "www.instagram.com"
        assert url.path == "/oauth/authorize"
        assert parse_qs(url.query) == {
            "client_id": ["123"], "redirect_uri": ["https://app.example/callback"],
            "response_type": ["code"], "state": ["csrf & value"],
            "scope": [",".join(MetaClient.SCOPES)], "enable_fb_login": ["0"], "force_reauth": ["1"],
        }
        with pytest.raises(ValueError):
            meta.authorize_url("")


@pytest.mark.parametrize("envelope", [True, False])
def test_code_exchange_form_then_long_lived_get(envelope):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            assert request.method == "POST"
            assert str(request.url) == "https://api.instagram.com/oauth/access_token"
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert parse_qs(request.content.decode()) == {
                "client_id": ["123"], "client_secret": ["secret-value"],
                "grant_type": ["authorization_code"], "code": ["code+value"],
                "redirect_uri": ["https://app.example/callback"],
            }
            body = {"access_token": "short-token", "user_id": 456}
            return httpx.Response(200, json={"data": [body]} if envelope else body)
        assert request.method == "GET"
        assert request.url.path == "/access_token"
        assert dict(request.url.params) == {
            "grant_type": "ig_exchange_token", "client_secret": "secret-value", "access_token": "short-token",
        }
        return httpx.Response(200, json={"access_token": "long-token", "expires_in": 5184000})

    with client(handler) as meta:
        token = meta.exchange_code("code+value")
    assert len(requests) == 2
    assert token.access_token == "long-token"
    assert token.user_id == "456"
    assert token.expires_in == 5184000
    assert "long-token" not in repr(token)


def test_refresh_has_no_secret_and_unversioned_endpoint():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/refresh_access_token"
        assert dict(request.url.params) == {"grant_type": "ig_refresh_token", "access_token": "old-token"}
        return httpx.Response(200, json={"access_token": "new-token", "expires_in": 500})
    with client(handler) as meta:
        assert meta.refresh("old-token").access_token == "new-token"


@pytest.mark.parametrize("envelope", [True, False])
def test_profile(envelope):
    def handler(request):
        assert request.url.path == "/v25.0/me"
        assert dict(request.url.params) == {"fields": "user_id,username"}
        assert request.headers["authorization"] == "Bearer token"
        data = {"user_id": "456", "username": "demo"}
        return httpx.Response(200, json={"data": [data]} if envelope else data)
    with client(handler, version="v25.0") as meta:
        profile = meta.get_profile("token")
        assert (profile.user_id, profile.username) == ("456", "demo")


def test_image_status_publish_requests():
    requests = []
    image_url = "https://storage.example/image?signature=private"

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer token"
        assert all(0 < value <= 30 for value in request.extensions["timeout"].values())
        if request.url.path.endswith("/media"):
            assert request.method == "POST"
            assert parse_qs(request.content.decode()) == {
                "image_url": [image_url], "caption": ["Caption & text"], "alt_text": ["Description"],
            }
            return httpx.Response(200, json={"id": "789"})
        if request.url.path.endswith("/789"):
            assert request.method == "GET"
            assert dict(request.url.params) == {"fields": "status_code,status"}
            return httpx.Response(200, json={"status_code": "FINISHED"})
        assert request.url.path == "/v26.0/456/media_publish"
        assert request.method == "POST"
        assert parse_qs(request.content.decode()) == {"creation_id": ["789"]}
        return httpx.Response(200, json={"id": "101112"})

    with client(handler) as meta:
        assert meta.create_jpeg_container("456", "token", image_url, "Caption & text", alt_text="Description") == "789"
        assert meta.get_status("789", "token") is ContainerStatus.FINISHED
        assert meta.publish("456", "token", "789") == "101112"
    assert len(requests) == 3


def test_limit_and_insights_preserve_missing_metrics():
    def handler(request):
        if request.url.path.endswith("/content_publishing_limit"):
            assert dict(request.url.params) == {"fields": "quota_usage,config"}
            return httpx.Response(200, json={"data": [{"quota_usage": 3, "config": {"quota_total": 50, "quota_duration": 86400}}]})
        assert request.url.path == "/v26.0/789/insights"
        assert dict(request.url.params) == {"metric": ",".join(MetaClient.METRICS)}
        return httpx.Response(200, json={"data": [{"name": "views", "values": [{"value": 14}]}]})
    with client(handler) as meta:
        assert meta.publishing_limit("456", "token")["config"]["quota_total"] == 50
        assert meta.media_insights("789", "token") == {"views": 14}


@pytest.mark.parametrize("status,body,kind", [
    (401, {}, ErrorKind.AUTH), (403, {}, ErrorKind.AUTH),
    (400, {"error": {"code": 190}}, ErrorKind.AUTH),
    (400, {"error_type": "OAuthException"}, ErrorKind.AUTH),
    (429, {}, ErrorKind.RATE_LIMIT),
    (400, {"error": {"code": 4}}, ErrorKind.RATE_LIMIT),
    (503, {}, ErrorKind.TRANSIENT),
    (400, {"error": {"is_transient": True}}, ErrorKind.TRANSIENT),
    (400, {"error": {"code": 100}}, ErrorKind.API),
    (302, {}, ErrorKind.API),
    (200, [], ErrorKind.INVALID_RESPONSE),
])
def test_error_classification_and_redaction(status, body, kind):
    def handler(request):
        if isinstance(body, dict):
            body["message"] = "secret-value token https://storage.example/?signature=private"
        return httpx.Response(status, json=body)
    with client(handler) as meta, pytest.raises(MetaError) as caught:
        meta.get_profile("token")
    assert caught.value.kind is kind
    assert caught.value.status_code == status
    assert "secret-value" not in str(caught.value)
    assert "signature" not in repr(caught.value)


@pytest.mark.parametrize("failure", ["timeout", "connect", "500", "invalid_json", "missing_id", "invalid_id"])
def test_ambiguous_publish_is_never_retried(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure in ("timeout", "connect"):
            error = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
            raise error("token secret-value https://storage.example/?signature=private", request=request)
        if failure == "500":
            return httpx.Response(500, json={"error": {"code": 190}})
        if failure == "invalid_json":
            return httpx.Response(200, text="secret-value")
        return httpx.Response(200, json={"id": "invalid"} if failure == "invalid_id" else {})

    with client(handler) as meta, pytest.raises(MetaError) as caught:
        meta.publish("456", "token", "789")
    assert caught.value.kind is ErrorKind.AMBIGUOUS_PUBLISH
    assert len(calls) == 1
    formatted = "".join(traceback.format_exception(caught.value))
    assert "secret-value https" not in formatted
    assert caught.value.__context__ is None


def test_transport_error_non_publish_and_client_ownership():
    def handler(request):
        raise httpx.ReadTimeout("sensitive", request=request)
    injected = httpx.Client(transport=httpx.MockTransport(handler), timeout=None)
    with MetaClient("123", "secret", "https://app.example/", client=injected) as meta:
        with pytest.raises(MetaError) as caught:
            meta.get_profile("token")
        assert caught.value.kind is ErrorKind.TRANSIENT
    assert not injected.is_closed
    injected.close()


@pytest.mark.parametrize("method,args,payload", [
    ("get_profile", ("token",), {"data": []}),
    ("get_profile", ("token",), {"user_id": True, "username": "demo"}),
    ("get_profile", ("token",), {"user_id": "123", "username": None}),
    ("refresh", ("token",), {"access_token": "token", "expires_in": True}),
    ("get_status", ("123", "token"), {"status_code": []}),
    ("get_status", ("123", "token"), {"status_code": "UNKNOWN"}),
    ("publishing_limit", ("123", "token"), {"data": [{"quota_usage": 1}]}),
    ("media_insights", ("123", "token"), {"data": [{"name": "reach", "values": []}]}),
    ("media_insights", ("123", "token"), {"data": [{"name": "reach", "values": [{"value": True}]}]}),
])
def test_malformed_response(method, args, payload):
    with client(lambda request: httpx.Response(200, json=payload)) as meta:
        with pytest.raises(MetaError) as caught:
            getattr(meta, method)(*args)
        assert caught.value.kind is ErrorKind.INVALID_RESPONSE


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 121])
def test_timeout_must_be_bounded(timeout):
    with pytest.raises(ValueError):
        client(lambda request: httpx.Response(200), timeout=timeout)
