"""Private Supabase Storage REST adapter; never exposes upstream exceptions."""
from urllib.parse import quote, urlsplit

import httpx


class StorageError(Exception):
    def __init__(self):
        super().__init__("Storage unavailable")


class StorageClient:
    def __init__(self, url: str, key: str, bucket: str, *, transport=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise StorageError()
        self.url = url.rstrip("/") + "/storage/v1"
        self.bucket = quote(bucket, safe="")
        self.client = httpx.Client(headers={"apikey": key, "Authorization": f"Bearer {key}"},
                                   timeout=30, transport=transport, follow_redirects=False)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def _request(self, method, path, **kwargs):
        result = None
        try:
            response = self.client.request(method, self.url + path, **kwargs)
            if 200 <= response.status_code < 300:
                result = response.json()
        except (httpx.RequestError, ValueError):
            pass
        if not isinstance(result, dict):
            raise StorageError()
        return result

    def upload(self, object_path: str, jpeg_bytes: bytes) -> None:
        bucket = self._request("GET", f"/bucket/{self.bucket}")
        if bucket.get("public") is not False:
            raise StorageError()
        self._request("POST", f"/object/{self.bucket}/{quote(object_path, safe='/')}",
                      content=jpeg_bytes, headers={"Content-Type": "image/jpeg", "x-upsert": "false"})

    def signed_url(self, object_path: str) -> str:
        bucket = self._request("GET", f"/bucket/{self.bucket}")
        if bucket.get("public") is not False:
            raise StorageError()
        result = self._request("POST", f"/object/sign/{self.bucket}/{quote(object_path, safe='/')}",
                               json={"expiresIn": 3600})
        signed = result.get("signedURL")
        if not isinstance(signed, str) or not signed.startswith("/object/sign/"):
            raise StorageError()
        return self.url + signed
