import httpx
import pytest

from integrations.storage import StorageClient, StorageError


@pytest.mark.parametrize("response", [httpx.Response(401, json={"message": "secret"}),
                                    httpx.Response(500, text="secret"), httpx.Response(200, json=["secret"])])
def test_storage_errors_are_sanitized(response):
    with StorageClient("https://example.supabase.co", "secret", "private", transport=httpx.MockTransport(lambda _: response)) as storage:
        with pytest.raises(StorageError) as error:
            storage.upload("account/image.jpg", b"jpeg")
    assert str(error.value) == "Storage unavailable"


def test_transport_failure_does_not_retain_request_exception():
    def fail(request):
        raise httpx.ReadTimeout("https://secret", request=request)
    with StorageClient("https://example.supabase.co", "secret", "private", transport=httpx.MockTransport(fail)) as storage:
        with pytest.raises(StorageError) as error:
            storage.signed_url("account/image.jpg")
    assert error.value.__context__ is None


def test_signed_url_is_short_lived_and_never_public():
    requests = []
    def handler(request):
        requests.append(request)
        if "/bucket/" in request.url.path:
            return httpx.Response(200, json={"public": False})
        return httpx.Response(200, json={"signedURL": "/object/sign/private/account/image.jpg?token=signed-secret"})
    with StorageClient("https://example.supabase.co", "secret", "private", transport=httpx.MockTransport(handler)) as storage:
        assert storage.signed_url("account/image.jpg") == "https://example.supabase.co/storage/v1/object/sign/private/account/image.jpg?token=signed-secret"
    assert requests[-1].content == b'{"expiresIn":3600}'
    assert requests[-1].headers["authorization"] == "Bearer secret"
