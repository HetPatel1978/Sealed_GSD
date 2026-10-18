"""Bounded public HTTPS reads with DNS pinning; never execute page scripts."""

import html
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from sealed.discovery.models import DiscoveryError

MAX_PAGE = 2_000_000


def safe_url(url):
    if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 33 for c in url) or "\\" in url:
        raise DiscoveryError("unsafe_source")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        if (parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host
                or host.endswith((".localhost", ".local", ".internal", ".example", ".test"))):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        if re.search(r"(?:^|[&?])(?:token|access_token|key|api_key|password|secret|signature|credential|code)=", parts.query, re.I):
            raise ValueError
        return parts._replace(netloc=host, fragment="").geturl()
    except (ValueError, UnicodeError):
        raise DiscoveryError("unsafe_source") from None


def same_site(url, website):
    return urlsplit(safe_url(url)).hostname.removeprefix("www.") == urlsplit(safe_url(website)).hostname.removeprefix("www.")


def public_addresses(host):
    values = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not values or any(not ipaddress.ip_address(value).is_global or ipaddress.ip_address(value).is_multicast for value in values):
        raise DiscoveryError("unsafe_source")
    return sorted(values)


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once, reject mixed public/private answers, then connect to that IP.
        # TLS still verifies the original hostname; it is never disabled.
        addresses = public_addresses(self.host)
        stream = socket.create_connection((addresses[0], 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(stream, server_hostname=self.host)
        except BaseException:
            stream.close()
            raise


@dataclass(frozen=True)
class Page:
    url: str
    text: str


class PublicWeb:
    def __init__(self):
        self.pages = {}

    def fetch(self, url):
        url = safe_url(url)
        if url in self.pages:
            page = self.pages[url]
            if page is None:
                raise DiscoveryError("source_unavailable")
            return page
        self.pages[url] = None
        original = url
        try:
            for _ in range(3):
                parts = urlsplit(url)
                connection = PinnedHTTPS(parts.hostname, timeout=8, context=ssl.create_default_context())
                try:
                    path = parts.path or "/"
                    if parts.query:
                        path += "?" + parts.query
                    connection.request("GET", path, headers={"User-Agent": "Sealed/0.2 public-source-verification", "Accept-Encoding": "identity"})
                    response = connection.getresponse()
                    if response.status in {301, 302, 303, 307, 308}:
                        target = safe_url(urljoin(url, response.getheader("Location", "")))
                        if not same_site(target, original):
                            raise DiscoveryError("unsafe_source")
                        url = target
                        continue
                    if (response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity"
                            or response.getheader("Content-Type", "").split(";")[0] not in {"text/html", "text/plain", "application/json", "application/xhtml+xml"}):
                        raise DiscoveryError("source_unavailable")
                    body = response.read(MAX_PAGE + 1)
                    if len(body) > MAX_PAGE:
                        raise DiscoveryError("source_too_large")
                    page = Page(url, body.decode("utf-8", errors="replace"))
                    self.pages[original] = page
                    return page
                finally:
                    connection.close()
            raise DiscoveryError("source_unavailable")
        except (OSError, ValueError, http.client.HTTPException):
            raise DiscoveryError("source_unavailable") from None

    def zip_point(self, code):
        if not re.fullmatch(r"\d{5}", code):
            raise DiscoveryError("invalid_postal_code")
        try:
            # A public ZIP centroid lookup, not another AI or a Google API/key.
            value = json.loads(self.fetch(f"https://api.zippopotam.us/us/{code}").text)
            if value["post code"] != code or value["country abbreviation"] != "US":
                raise ValueError
            place = value["places"][0]
            lat, lon = float(place["latitude"]), float(place["longitude"])
            if not math.isfinite(lat + lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError
            return lat, lon
        except (KeyError, IndexError, TypeError, ValueError):
            raise DiscoveryError("postal_lookup_failed") from None


def miles(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 3958.7613 * 2 * math.asin(min(1, math.sqrt(h)))


def canonical(text):
    return re.sub(r"[^a-z0-9]", "", html.unescape(text).casefold())


def phone_digits(value):
    digits = re.sub(r"\D", "", value)
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def validate_candidates(request, report, web):
    result, seen, dropped = [], set(), 0
    origin = web.zip_point(request.zip)
    for candidate in report.dealers:
        try:
            website = safe_url(candidate.website)
            domain = urlsplit(website).hostname.removeprefix("www.")
            if domain in seen:
                raise DiscoveryError("duplicate_dealer")
            seen.add(domain)
            urls = list(dict.fromkeys([*([candidate.matching_listing_url] if candidate.matching_listing_url else []), *candidate.sources]))[:3]
            pages = []
            for url in urls:
                try:
                    if same_site(url, website):
                        pages.append(web.fetch(url))
                except DiscoveryError:
                    continue
            # Only accept name/address grounded literally in fetched official-site HTML.
            raw = " ".join(page.text for page in pages)
            normalized = canonical(re.sub(r"<[^>]+>", " ", raw)) + canonical(raw)
            if canonical(candidate.name) not in normalized or canonical(candidate.address) not in normalized:
                raise DiscoveryError("dealer_not_grounded")
            postal = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", candidate.address)
            if not postal:
                raise DiscoveryError("postal_lookup_failed")
            distance = miles(origin, web.zip_point(postal[1]))
            if distance > request.radius_miles:
                raise DiscoveryError("outside_radius")
            email = candidate.email
            if email and (not re.fullmatch(r"[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+", email)
                          or email.casefold() not in html.unescape(raw).casefold()):
                email = None
            phone = candidate.phone
            published_phones = {phone_digits(p) for p in re.findall(r"(?<!\d)(?:\+?1[ .()-]*)?\d{3}[ .()-]*\d{3}[ .()-]*\d{4}(?!\d)", raw)}
            if phone and (len(phone_digits(phone)) != 10 or phone_digits(phone) not in published_phones):
                phone = None
            if not email and not phone:
                raise DiscoveryError("no_grounded_contact")
            inventory, listing = "unknown", None
            if candidate.matching_listing_url:
                for page in pages:
                    text = html.unescape(re.sub(r"<[^>]+>", " ", page.text)).lower()
                    words = set(re.findall(r"[a-z0-9]+", request.spec.details.lower())) - {"the", "and", "with", "a", "in", "of", "for"}
                    if (page.url == safe_url(candidate.matching_listing_url)
                            and re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", page.text)
                            and len(words) >= 3 and words <= set(re.findall(r"[a-z0-9]+", text))):
                        inventory = "listed_in_transit" if re.search(r"\bin transit\b|\bpre.?order\b", text) else "listed_unconfirmed"
                        listing = page.url
            result.append({"name": candidate.name, "website": website, "address": candidate.address,
                "email": email, "phone": phone, "distance_miles": round(distance, 1),
                "distance_method": "approximate ZIP-centroid straight-line distance", "inventory_status": inventory,
                "in_stock": None, "matching_listing_url": listing,
                "source": {"kind": "discovery", "urls": list(dict.fromkeys(p.url for p in pages))},
                "flags": ["availability_unconfirmed"] + (["phone_only"] if not email else [])})
        except DiscoveryError:
            dropped += 1
    result.sort(key=lambda item: (item["inventory_status"] == "unknown", item["distance_miles"]))
    return {"dealers": result, "dropped": dropped}
