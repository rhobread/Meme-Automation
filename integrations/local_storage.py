"""Private local media with HMAC bearer URLs and immutable object paths."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote, urlsplit

from integrations.storage import StorageError


class LocalStorageClient:
    def __init__(self, directory, signing_key, public_base_url="", ttl=3600):
        self.root = Path(directory).resolve()
        self.key = signing_key.encode()
        self.base = public_base_url.rstrip("/")
        self.ttl = ttl

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def path(self, key):
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.jpg", key)
                or len(key) > 400):
            raise StorageError()
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError()
        return path

    def upload(self, key, data):
        self._upload(key, data, replay=False)

    def ensure_upload(self, key, data):
        self._upload(key, data, replay=True)

    def _upload(self, key, data, replay):
        path = self.path(key)
        if not data or len(data) > 8_000_000:
            raise StorageError()
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if not replay or path.stat().st_size != len(data) or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(data).digest():
                    raise StorageError() from None
        except OSError:
            raise StorageError() from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def signature(self, key, expires):
        return hmac.new(self.key, f"media-v1\n{key}\n{expires}".encode(), hashlib.sha256).hexdigest()

    def signed_url(self, key):
        parsed = urlsplit(self.base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise StorageError()
        if not self.path(key).is_file():
            raise StorageError()
        expires = int(time.time()) + self.ttl
        return f"{self.base}/media/{quote(key, safe='/')}?expires={expires}&signature={self.signature(key, expires)}"

    def verified_path(self, key, expires, signature):
        now = int(time.time())
        if (expires <= now or expires > now + self.ttl or not re.fullmatch(r"[a-f0-9]{64}", signature)
                or not hmac.compare_digest(self.signature(key, expires), signature)):
            raise StorageError()
        path = self.path(key)
        if not path.is_file():
            raise StorageError()
        return path
