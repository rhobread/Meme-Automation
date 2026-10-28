"""Private Supabase Storage REST adapter; never exposes upstream exceptions."""
from urllib.parse import quote, urlsplit
import hashlib

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

    def ensure_upload(self, object_path: str, jpeg_bytes: bytes) -> None:
        """Replay-safe, no overwrite: accept an existing object only if bytes match."""
        bucket = self._request("GET", f"/bucket/{self.bucket}")
        if bucket.get("public") is not False:
            raise StorageError()
        path = f"/{self.bucket}/{quote(object_path, safe='/')}"
        accepted = False
        try:
            response = self.client.post(self.url + "/object" + path, content=jpeg_bytes,
                                        headers={"Content-Type": "image/jpeg", "x-upsert": "false"})
            if 200 <= response.status_code < 300:
                accepted = True
            else:
                # Supabase may encode a duplicate as HTTP 400 with statusCode 409.
                duplicate = response.status_code == 409 or (response.status_code == 400 and
                            str(response.json().get("statusCode")) == "409")
                if duplicate:
                    with self.client.stream("GET", self.url + "/object/authenticated" + path) as existing:
                        if existing.status_code == 200:
                            digest = hashlib.sha256()
                            size = 0
                            for part in existing.iter_bytes():
                                size += len(part)
                                if size > len(jpeg_bytes):
                                    raise ValueError()
                                digest.update(part)
                            accepted = size == len(jpeg_bytes) and digest.digest() == hashlib.sha256(jpeg_bytes).digest()
        except (httpx.RequestError, ValueError, AttributeError):
            pass
        if not accepted:
            raise StorageError()
