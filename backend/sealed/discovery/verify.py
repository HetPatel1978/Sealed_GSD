"""The one check every discovered dealer passes, whichever search found it.

- A contact detail is kept only if it appears word for word on a page we fetched ourselves (the cited page,
  the dealer's home page, or its /contact page). Phones are compared as 10-digit numbers.
- The street address must be on a page too; its ZIP gives the distance (offline ZIP table, pgeocode), and
  dealers beyond the radius, or with no verifiable address, are dropped.
- Dealers left with neither an email nor a phone are dropped. Duplicates (same domain or phone) are merged.
"""

import re
from typing import Callable
from urllib.parse import urljoin, urlsplit

from sealed.discovery.fetch import Fetcher, Page
from sealed.discovery.models import Candidate

KM_PER_MILE = 1.609344
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?(\d{3})\)?[\s.\-]?(\d{3})[\s.\-]?(\d{4})(?!\d)")
ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

_geo = None


def zip_distance_miles(a: str, b: str) -> float | None:
    """Centroid distance between two US ZIPs. pgeocode downloads its table once, then works offline."""
    global _geo
    if _geo is None:
        import pgeocode
        _geo = pgeocode.GeoDistance("us")
    km = _geo.query_postal_code(a, b)
    return None if km != km else float(km) / KM_PER_MILE            # NaN = unknown ZIP


def phone_digits(s: str) -> str | None:
    m = PHONE.search(s or "")
    return "".join(m.groups()) if m else None


def _norm(s: str) -> str:
    return re.sub(r"[\s,.#]+", " ", s.lower()).strip()


def _domain(url: str | None) -> str | None:
    host = urlsplit(url).hostname if url else None
    return host.removeprefix("www.") if host else None


def _pages_for(c: Candidate) -> list[str]:
    urls = [c.page_url, c.website, urljoin(c.website, "/contact") if c.website else None]
    return list(dict.fromkeys(u for u in urls if u))


def check(c: Candidate, user_zip: str, radius_miles: float, fetcher: Fetcher,
          distance: Callable[[str, str], float | None]) -> dict | None:
    """The verified dealer record (docs/api.md), or None if it's dropped."""
    found: dict[str, str] = {}                          # field -> page url it was found on
    for url in _pages_for(c):
        if len(found) == 3:
            break
        page: Page | None = fetcher.get(url)
        if page is None:
            continue
        raw, text = page.html.lower(), _norm(page.text)
        if c.email and "email" not in found and c.email.strip().lower() in raw:
            found["email"] = page.url
        want = phone_digits(c.phone)
        if want and "phone" not in found and want in {"".join(m) for m in PHONE.findall(page.html)}:
            found["phone"] = page.url
        if c.address and "address" not in found and _norm(c.address) in text:
            found["address"] = page.url

    email = c.email.strip() if "email" in found else None
    phone = c.phone.strip() if "phone" in found else None
    if not (email or phone) or "address" not in found:
        return None
    zips = ZIP.findall(c.address)
    miles = distance(user_zip, zips[-1]) if zips else None
    if miles is None or miles > radius_miles:
        return None
    return {
        "name": c.name, "email": email, "phone": phone, "website": c.website, "address": c.address.strip(),
        "distance_miles": round(miles, 1), "in_stock": c.in_stock, "found_by": c.found_by,
        "source": {"kind": "discovery", "urls": list(dict.fromkeys(found.values()))},
    }


def same_dealer(a: dict, b: dict) -> bool:
    da, db = _domain(a.get("website")), _domain(b.get("website"))
    pa, pb = phone_digits(a.get("phone") or ""), phone_digits(b.get("phone") or "")
    return bool((da and da == db) or (pa and pa == pb))


def merge(existing: list[dict], new: list[dict]) -> list[dict]:
    out = list(existing)
    for d in new:
        if not any(same_dealer(d, e) for e in out):
            out.append(d)
    return out
