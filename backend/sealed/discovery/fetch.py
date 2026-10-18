"""Our own read-only page fetcher for checking discovered contact details.

- GET only: no POST, no forms, no cookies kept between hosts.
- URLs come from Fo or Claude, so they're untrusted: http(s) only, and hosts that resolve to loopback,
  private or link-local addresses are refused (the backend itself listens on 127.0.0.1).
- Respects robots.txt, uses a short timeout and a size cap, and caches pages for one discovery run.
"""

import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "SealedDiscovery/0.1 (read-only contact check)"
TIMEOUT_S = 8.0
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 4


@dataclass(frozen=True)
class Page:
    url: str
    html: str          # raw markup (mailto:/tel: links live here)
    text: str          # visible text, entities decoded, whitespace collapsed


def _public_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            return False
    return True


def to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", markup)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup))).strip()


class Fetcher:
    def __init__(self, client: httpx.Client | None = None, host_check=_public_host):
        self.client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False,
                                             headers={"User-Agent": USER_AGENT})
        self.host_check = host_check
        self._pages: dict[str, Page | None] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def get(self, url: str) -> Page | None:
        """The page, or None if it can't or mustn't be fetched. Never raises."""
        if url not in self._pages:
            try:
                self._pages[url] = self._get(url)
            except (httpx.HTTPError, ValueError, UnicodeError):
                self._pages[url] = None
        return self._pages[url]

    def _get(self, url: str) -> Page | None:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not self.host_check(parts.hostname):
                return None
            if not self._allowed(url):
                return None
            with self.client.stream("GET", url) as r:
                if r.is_redirect and "location" in r.headers:
                    url = urljoin(url, r.headers["location"])      # re-checked on the next pass
                    continue
                if r.status_code != 200 or "html" not in r.headers.get("content-type", "text/html"):
                    return None
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                markup = body[:MAX_BYTES].decode(r.encoding or "utf-8", errors="replace")
                return Page(url=url, html=markup, text=to_text(markup))
        return None

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.client.get(root + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._robots[root] = rp
        return self._robots[root].can_fetch(USER_AGENT, url)
