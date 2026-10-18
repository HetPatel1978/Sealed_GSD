import inspect
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from sealed.discovery import dealers as dealers_mod
from sealed.discovery.claude_finder import ClaudeFinder
from sealed.discovery.dealers import discover
from sealed.discovery.fetch import Fetcher, Page, to_text
from sealed.discovery.fo_finder import FoFinder, parse
from sealed.discovery.models import Candidate, DiscoveryUnavailable, FindResult
from sealed.discovery.verify import check
from sealed.fo.adapter import FO_TURN_LOCK
from tests.test_fo_adapter import FakeFo

CAR, ZIP = "2026 Honda Civic Sport sedan, Crystal Black Pearl", "19104"
CEILING, TARGET = 32000, 30500
DIST = {"19104": 0.6, "19064": 9.8, "19462": 14.1, "08002": 25.0}


def distance(a, b):
    return DIST.get(b)


def page(url, body):
    return Page(url=url, html=body, text=to_text(body))


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url):
        self.asked.append(url)
        return self.pages.get(url)


VALLEY = page("https://valleyhonda.example/contact",
              '<a href="mailto:sales@valleyhonda.example">Email</a> <a href="tel:+12155550101">(215) 555-0101</a>'
              "<p>3900 Chestnut St, Philadelphia, PA 19104</p>")
NITTANY = page("https://nittanyhonda.example/about", "Call 215.555.0144 · 1200 Baltimore Pike, Springfield, PA 19064")
EAGLE = page("https://baldeaglehonda.example/contact-us",
             "internetsales@baldeaglehonda.example 500 W Germantown Pike, Plymouth Meeting, PA 19462")
FAR = page("https://farhonda.example/contact", "sales@farhonda.example 1 Main St, Cherry Hill, NJ 08002")
PAGES = {p.url: p for p in (VALLEY, NITTANY, EAGLE, FAR)}

FO_REPLY = """Here are dealers near 19104 (I haven't contacted anyone):
| name | website URL | sales email | sales phone | street address with ZIP | page URL |
|---|---|---|---|---|---|
| Valley Honda | https://valleyhonda.example | sales@valleyhonda.example | 215-555-0101 | 3900 Chestnut St, Philadelphia, PA 19104 | https://valleyhonda.example/contact |
| Nittany Honda | nittanyhonda.example | - | (215) 555-0144 | 1200 Baltimore Pike, Springfield, PA 19064 | https://nittanyhonda.example/about |
| Bald Eagle Honda | https://baldeaglehonda.example | internetsales@baldeaglehonda.example | - | 500 W Germantown Pike, Plymouth Meeting, PA 19462 | https://baldeaglehonda.example/contact-us |
"""


def fo_finder(*script, **kw):
    fo = FakeFo(list(script))
    return fo, FoFinder(fo, wait_seconds=1, **kw)


class FakeClaude:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps({"dealers": self.rows}))])


CLAUDE_ROWS = [{"name": "Far Honda", "website": "https://farhonda.example", "email": "sales@farhonda.example",
                "phone": None, "address": "1 Main St, Cherry Hill, NJ 08002",
                "page_url": "https://farhonda.example/contact", "in_stock": None},
               {"name": "Bald Eagle Honda", "website": "https://www.baldeaglehonda.example",
                "email": "internetsales@baldeaglehonda.example", "phone": None,
                "address": "500 W Germantown Pike, Plymouth Meeting, PA 19462",
                "page_url": "https://baldeaglehonda.example/contact-us", "in_stock": True}]


def run(finders, pages=PAGES):
    return discover(CAR, ZIP, finders, FakeFetcher(pages), distance=distance)


# ---- prompts: only car, ZIP, radius ---------------------------------------------------------------------
def test_no_ceiling_or_target_reaches_either_search():
    assert not {"ceiling", "target"} & set(inspect.signature(discover).parameters)
    fo, f = fo_finder({"status": "complete", "response": FO_REPLY})
    claude = FakeClaude(CLAUDE_ROWS)
    f.find(CAR, ZIP, 20)
    ClaudeFinder(claude).find(CAR, ZIP, 20)
    sent = fo.asks[0][0] + json.dumps(claude.calls[0]["messages"])
    for secret in (str(CEILING), f"{CEILING:,}", str(TARGET), f"{TARGET:,}"):
        assert secret not in sent


def test_fo_turn_is_a_new_conversation_and_takes_the_shared_lock():
    seen = []

    class SpyLock:
        def __enter__(self):
            seen.append("in")

        def __exit__(self, *a):
            seen.append("out")

    fo, f = fo_finder({"status": "complete", "response": FO_REPLY}, lock=SpyLock())
    f.find(CAR, ZIP, 20)
    assert fo.asks[0][1] is None and seen == ["in", "out"]
    assert FoFinder(fo).lock is FO_TURN_LOCK


# ---- parsing and verification -----------------------------------------------------------------------------
def test_parse_reads_dealer_lines_and_skips_headers():
    rows = parse(FO_REPLY)
    assert [r.name for r in rows] == ["Valley Honda", "Nittany Honda", "Bald Eagle Honda"]
    assert rows[1].email is None and rows[1].website == "https://nittanyhonda.example"


def test_details_not_on_a_fetched_page_are_dropped():
    c = Candidate("Valley Honda", "https://valleyhonda.example", "made-up@valleyhonda.example", "215-555-0101",
                  "3900 Chestnut St, Philadelphia, PA 19104", "https://valleyhonda.example/contact", "fo")
    r = check(c, ZIP, 20, FakeFetcher(PAGES), distance)
    assert r["email"] is None and r["phone"] == "215-555-0101"
    assert r["source"] == {"kind": "discovery", "urls": ["https://valleyhonda.example/contact"]}
    wrong_phone = Candidate("Nittany", None, None, "215-555-9999", "1200 Baltimore Pike, Springfield, PA 19064",
                            "https://nittanyhonda.example/about", "fo")
    assert check(wrong_phone, ZIP, 20, FakeFetcher(PAGES), distance) is None


def test_dealers_outside_radius_or_without_verified_address_are_dropped():
    far = Candidate("Far Honda", None, "sales@farhonda.example", None, "1 Main St, Cherry Hill, NJ 08002",
                    "https://farhonda.example/contact", "claude")
    assert check(far, ZIP, 20, FakeFetcher(PAGES), distance) is None
    moved = Candidate("Valley Honda", None, "sales@valleyhonda.example", None, "1 Other Rd, Philadelphia, PA 19104",
                      "https://valleyhonda.example/contact", "fo")
    assert check(moved, ZIP, 20, FakeFetcher(PAGES), distance) is None


# ---- order and fallback -----------------------------------------------------------------------------------
def test_healthy_fo_result_does_not_call_claude():
    _, f = fo_finder({"status": "complete", "response": FO_REPLY})
    claude = FakeClaude(CLAUDE_ROWS)
    out = run([f, ClaudeFinder(claude)])
    assert [d["name"] for d in out["dealers"]] == ["Valley Honda", "Nittany Honda", "Bald Eagle Honda"]
    assert {d["found_by"] for d in out["dealers"]} == {"fo"}
    assert out["search"] == {"primary": "fo", "fallback_used": False, "fallback_reason": None, "warnings": []}
    assert claude.calls == []


@pytest.mark.parametrize("script,reason", [
    ([{"status": "failed", "response": "boom"}], "fo_failed"),
    ([{"status": "working"}] + [{"status": "working"}] * 50, "fo_timeout"),
    ([{"status": "complete", "response": "Sorry, I couldn't find any."}], "fo_unparsed"),
    ([{"status": "complete", "response": "I've emailed Valley Honda for you.\n" + FO_REPLY}], "fo_acted"),
    ([{"status": "complete", "response": FO_REPLY.split("| Nittany")[0]}], "too_few"),
])
def test_each_fallback_trigger_calls_claude(script, reason):
    ticks = iter(range(0, 10_000, 30))
    fo, f = fo_finder(*script, budget_s=90, clock=lambda: float(next(ticks)))
    claude = FakeClaude(CLAUDE_ROWS)
    out = run([f, ClaudeFinder(claude)])
    assert len(claude.calls) == 1
    assert out["search"]["fallback_used"] and out["search"]["fallback_reason"] == reason
    assert "Bald Eagle Honda" in [d["name"] for d in out["dealers"]]
    assert "Far Honda" not in [d["name"] for d in out["dealers"]]           # 25 mi
    if reason == "fo_acted":
        assert out["search"]["warnings"][0]["code"] == "needs_human"
        assert {d["found_by"] for d in out["dealers"]} == {"claude"}


def test_fo_not_configured_falls_back():
    out = run([FoFinder(None), ClaudeFinder(FakeClaude(CLAUDE_ROWS))])
    assert out["search"]["fallback_reason"] == "fo_not_configured"


def test_merge_dedupes_across_searches():
    _, f = fo_finder({"status": "complete", "response": FO_REPLY.split("| Nittany")[0]
                      + FO_REPLY.split("Springfield, PA 19064 | https://nittanyhonda.example/about |\n")[1]})
    out = run([f, ClaudeFinder(FakeClaude(CLAUDE_ROWS))])
    names = [d["name"] for d in out["dealers"]]
    assert names.count("Bald Eagle Honda") == 1
    assert next(d for d in out["dealers"] if d["name"] == "Bald Eagle Honda")["found_by"] == "fo"


def test_all_searches_failing_is_unavailable():
    _, f = fo_finder({"status": "failed", "response": "x"})
    with pytest.raises(DiscoveryUnavailable):
        run([f, ClaudeFinder(FakeClaude(error=RuntimeError("down")))])


def test_order_can_be_flipped(monkeypatch):
    monkeypatch.setenv("SEALED_DISCOVERY_ORDER", "claude")
    assert dealers_mod.order_from_env() == ("claude",)


def test_only_radius_20_in_v1():
    class Never:
        name = "x"

        def find(self, *a):
            raise AssertionError

    with pytest.raises(ValueError):
        discover(CAR, ZIP, [Never()], FakeFetcher({}), radius_miles=50, distance=distance)


# ---- fetcher ----------------------------------------------------------------------------------------------
def test_fetcher_only_sends_get_and_respects_robots():
    methods = []

    def handler(req):
        methods.append(req.method)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/contact"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>sales@x.example</p>")

    f = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), host_check=lambda h: True)
    assert f.get("https://x.example/old").text == "sales@x.example"
    assert f.get("https://x.example/private/a") is None
    assert f.get("ftp://x.example/") is None
    assert set(methods) == {"GET"}


def test_fetcher_refuses_local_and_private_hosts():
    def handler(req):
        raise AssertionError("should not fetch")

    f = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)))
    for url in ("http://127.0.0.1:8000/health", "http://localhost/", "http://10.0.0.5/", "http://169.254.169.254/"):
        assert f.get(url) is None


# ---- route ------------------------------------------------------------------------------------------------
def test_route_validates_and_serves_mock_in_sim(monkeypatch):
    from tests.test_health import local_client
    monkeypatch.setenv("SEALED_MODE", "sim")
    c = local_client()
    body = {"item": "2026 Honda Civic Sport", "spec": {"details": CAR}, "zip": ZIP}
    r = c.post("/dealers/discover", json=body)
    assert r.status_code == 200 and r.json()["search"]["primary"] == "fo"
    assert c.post("/dealers/discover", json={**body, "radius_miles": 50}).status_code == 422
    assert c.post("/dealers/discover", json={**body, "zip": "1910"}).status_code == 422


def test_route_live_runs_discovery_and_reports_503(monkeypatch):
    import app.main as main
    from tests.test_health import local_client
    monkeypatch.setenv("SEALED_MODE", "live")
    body = {"item": "2026 Honda Civic Sport", "spec": {"details": CAR}, "zip": ZIP}
    monkeypatch.setattr(main, "_finders", lambda: [FoFinder(None)])
    assert local_client().post("/dealers/discover", json=body).status_code == 503
    seen = {}

    def fake_discover(car, zip_code, finders, fetcher, radius_miles):
        seen.update(car=car, zip=zip_code)
        return {"zip": zip_code, "radius_miles": radius_miles, "dealers": [], "dropped": 0, "search": {}}

    monkeypatch.setattr(main, "discover", fake_discover)
    r = local_client().post("/dealers/discover", json=body)
    assert r.status_code == 200 and r.json()["mode"] == "live" and seen == {"car": CAR, "zip": ZIP}
