"""Fetch only operator-permitted RSS/Atom titles. Never fetch item media or HTML."""
import hashlib
import ipaddress
import re
import socket
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree


class DiscoveryError(Exception):
    def __init__(self):
        super().__init__("Feed unavailable or invalid")


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def clean_title(value):
    parser = PlainText()
    parser.feed(value)
    return " ".join(unicodedata.normalize("NFKC", " ".join(parser.parts)).split())


def normalized_idea(value):
    return " ".join(re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()).split())


def fingerprint(value):
    return hashlib.sha256(normalized_idea(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Idea:
    title: str
    fingerprint: str
    source_id: str | None
    source_url: str | None


class DiscoveryClient:
    def __init__(self, *, transport=None, resolver=socket.getaddrinfo):
        self.client = httpx.Client(timeout=10, follow_redirects=False, transport=transport, trust_env=False,
                                   headers={"User-Agent": "OriginalMemeIdeas/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
        self.resolver = resolver

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def fetch(self, feed):
        if not feed.ideas_use_permitted:
            raise DiscoveryError()
        result = None
        try:
            host = urlsplit(feed.url).hostname
            addresses = self.resolver(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ValueError()
            with self.client.stream("GET", feed.url) as response:
                if response.status_code != 200:
                    raise ValueError()
                content = bytearray()
                for part in response.iter_bytes():
                    content.extend(part)
                    if len(content) > 1024 * 1024:
                        raise ValueError()
            root = ElementTree.fromstring(bytes(content), forbid_dtd=True, forbid_entities=True, forbid_external=True)
            local = lambda node: node.tag.rsplit("}", 1)[-1]
            if local(root) not in ("rss", "feed", "RDF"):
                raise ValueError()
            result = []
            seen = set()
            for node in root.iter():
                if local(node) not in ("item", "entry"):
                    continue
                children = {local(child): child for child in node}
                title_node = children.get("title")
                title = clean_title("".join(title_node.itertext())) if title_node is not None else ""
                if not 3 <= len(title) <= 400 or not normalized_idea(title):
                    continue
                digest = fingerprint(title)
                if digest in seen:
                    continue
                seen.add(digest)
                link = children.get("link")
                url = (link.get("href") or link.text or "").strip() if link is not None else ""
                parsed = urlsplit(url)
                url = url if parsed.scheme == "https" and parsed.hostname and not parsed.username and len(url) <= 2000 else None
                identifier = children.get("guid")
                if identifier is None:
                    identifier = children.get("id")
                source_id = (identifier.text or "")[:500] if identifier is not None else None
                result.append(Idea(title, digest, source_id, url))
                if len(result) >= 50:
                    break
        except Exception:
            # No upstream response, URL, XML or resolver exception reaches logs.
            pass
        if result is None:
            raise DiscoveryError()
        return result
