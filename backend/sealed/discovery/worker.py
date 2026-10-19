"""Background, durable discovery: Fo first, Claude as an opt-in backup; HTTP polling never waits for a turn.

The backup runs only when the user allowed it and Fo's research failed, needs review, or left fewer than
MIN_DEALERS verified dealers. It never takes the Fo turn lock and never touches the Fo account state, so an
uncertain Fo reservation stays exactly as it was. Both reports go through the same validate_candidates.
"""

import json
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sealed.discovery.models import DiscoveryError, INLINE_REQUEST, Request, parse_report, prompt
from sealed.discovery.sources import PublicWeb, validate_candidates
from sealed.discovery.store import Store, thin
from sealed.fo.adapter import Store as FoStore, _TURN_LOCK
from sealed.fo.client import FoTimeout, LiveFoClient


class Discovery:
    def __init__(self, root, *, account, mode, fo_state, client_factory=None, web_factory=None, claude_factory=None):
        self.mode, self.fo_state = mode, fo_state
        self.claude_factory = claude_factory if mode == "live" else None   # simulation never calls Claude
        self.store = Store(root / "discovery.sqlite3", f"{account}:{fo_state.resolve()}:{mode}", mode)
        self.client_factory, self.web_factory = client_factory or LiveFoClient, web_factory or PublicWeb
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="sealed-discovery")
        self.thread.start()

    def submit(self, request, allow_claude=False):
        retry_unattempted = False
        if self.mode == "live" and _TURN_LOCK.acquire(blocking=False):
            try:
                retry_unattempted = not FoStore(self.fo_state).data.get("active")
            finally:
                _TURN_LOCK.release()
        id_, cached = self.store.create(request, retry_unattempted=retry_unattempted,
                                        allow_claude=allow_claude and self.claude_factory is not None)
        self.wake.set()
        return {**self.store.view(id_), "cached": cached}

    def validate_selection(self, data):
        for vendor in data["vendors"]:
            source = vendor["source"]
            if source["kind"] != "discovery":
                continue
            row = self.store.get(source.get("discovery_id"))
            public = Request.model_validate_json(row["request"])
            if (row["status"] != "complete" or time.time()-row["updated"] > 3600
                    or public.item != data["item"] or public.spec.details != data["spec"]["details"] or public.zip != data["zip"]):
                raise DiscoveryError("discovery_selection_changed")
            candidates = self.store.view(row["id"])["dealers"]
            selected = next((d for d in candidates if d["source"]["candidate_id"] == source.get("candidate_id")), None)
            if not selected or any(vendor.get(k) != selected.get(k) for k in ("name", "email", "phone")) or source.get("urls") != selected["source"]["urls"]:
                raise DiscoveryError("discovery_selection_changed")

    def close(self):
        self.stop.set()
        self.wake.set()
        # An interrupted network call keeps its durable reservation for polling.
        self.thread.join()

    def _loop(self):
        while not self.stop.is_set():
            self.wake.wait(.5)
            self.wake.clear()
            for id_ in self.store.pending():
                if self.stop.is_set():
                    break
                try:
                    self._run(id_)
                except Exception:
                    self.store.update(id_, status="needs_review", error="research_requires_review")

    def _run(self, id_):
        row = self.store.get(id_)
        request = Request.model_validate_json(row["request"])
        if self.mode == "sim":
            self.store.update(id_, status="complete", result=json.dumps({"dealers": [{
                "name": f"Simulated dealer {i}", "website": f"https://dealer{i}.example/",
                "address": f"Synthetic address near ZIP {request.zip}", "email": f"dealer{i}@example.org", "phone": None,
                "distance_miles": float(i*3), "distance_method": "synthetic fixture", "in_stock": None,
                "inventory_status": "unknown", "matching_listing_url": None,
                "source": {"kind": "discovery", "urls": [f"https://dealer{i}.example/"]},
                "flags": ["synthetic_evidence"]} for i in range(1, 4)], "dropped": 0}))
            return
        if row["phase"] == "claude":
            self._backup(id_, request)        # restart-safe: a Claude search contacts no one
            return
        if row["phase"] != "validate":
            while not self.stop.is_set():
                if _TURN_LOCK.acquire(timeout=.2):
                    try:
                        researched = self._research(id_, request)
                    finally:
                        _TURN_LOCK.release()
                    if not researched:
                        if self.store.get(id_)["phase"] == "claude":
                            self._backup(id_, request)     # after the Fo lock is released
                        return
                    break
            if self.stop.is_set():
                return
        self.store.update(id_, status="validating")
        row = self.store.get(id_)
        try:
            result = found_by(validate_candidates(request, parse_report(row["raw_reply"]), self.web_factory()), "fo")
            self._fo_done(id_, status="complete", result=json.dumps(result), error=None)
        except DiscoveryError as error:
            self._fo_done(id_, status="failed", error=str(error))
        if self.store.get(id_)["phase"] == "claude":
            self._backup(id_, request)

    def _fo_done(self, id_, **fields):
        """Records Fo's final outcome. If the backup will run, the same write moves the search to the backup
        phase instead, so a poller never sees a final status that is about to change."""
        row = {**self.store.get(id_), **fields}
        if (self.claude_factory is None or not row["allow_claude"] or row["fallback"] is not None
                or not thin(row)):
            self.store.update(id_, **fields)
            return
        fallback = {"reason": row["error"] or "too_few", "fo_status": row["status"], "fo_phase": row["phase"], "error": None}
        self.store.update(id_, **{**fields, "status": "researching", "phase": "claude", "fallback": json.dumps(fallback)})

    def _backup(self, id_, request):
        row = self.store.get(id_)
        fallback = json.loads(row["fallback"])
        fo = json.loads(row["result"]) if row["result"] else {"dealers": [], "dropped": 0}
        self.store.update(id_, status="researching")
        try:
            with self.claude_factory() as claude:
                report = claude.research(request)
            self.store.update(id_, status="validating")
            extra = found_by(validate_candidates(request, report, self.web_factory()), "claude")
        except Exception as error:
            # Claude failing leaves Fo's outcome (and any Fo review state) exactly as it was, including the
            # phase, so a search that never reached Fo can still be retried once the account is free.
            fallback["error"] = str(error) if isinstance(error, DiscoveryError) else "claude_failed"
            self.store.update(id_, status=fallback["fo_status"], phase=fallback.get("fo_phase", "claude"),
                              fallback=json.dumps(fallback))
            return
        self.store.update(id_, status="complete", result=json.dumps(merge(fo, extra)), fallback=json.dumps(fallback))

    def _research(self, id_, request):
        key = FoStore.key("dealer-discovery", id_, "research")
        state = FoStore(self.fo_state)
        row = self.store.get(id_)
        if state.data.get("active") not in (None, key):
            # Preserve any existing uncertain send/research reservation unchanged.
            self._fo_done(id_, status="needs_review", error="fo_account_requires_review")
            return False
        if row["phase"] in {"ask_pending", "inline_pending"} and not row["conversation_id"]:
            self._fo_done(id_, status="needs_review", error="research_requires_review")
            return False
        client = self.client_factory()
        try:
            if row["phase"] == "new":
                # Check connectivity before the durable non-idempotent claim.
                client.conversations()
                if self.stop.is_set():
                    return False
                state.set_status(key, "pending", active=True, channel="research", discovery_id=id_,
                                 attempted_at=datetime.now(timezone.utc).isoformat())
                self.store.update(id_, status="researching", phase="ask_pending")
                result = self._ask(client, prompt(request), None)
            else:
                result = client.reply(row["conversation_id"], wait_seconds=20)
            deadline = time.monotonic() + 900
            while True:
                row = self.store.get(id_)
                cid = result.get("conversationId") or row["conversation_id"]
                if row["conversation_id"] and cid != row["conversation_id"]:
                    raise DiscoveryError("conversation_mismatch")
                if cid:
                    self.store.update(id_, conversation_id=cid)
                status = result.get("status")
                if status == "complete":
                    text = result.get("response", "")
                    try:
                        parse_report(text)
                    except DiscoveryError:
                        if row["phase"] != "ask_pending" or not cid or self.stop.is_set():
                            self._fo_done(id_, status="needs_review", error="invalid_report")
                            state.set_status(key, "research_finished_unusable", active=False)
                            return False
                        # One distinct formatting request AFTER the original turn ends.
                        self.store.update(id_, phase="inline_pending")
                        result = self._ask(client, INLINE_REQUEST, cid)
                        continue
                    self.store.update(id_, phase="validate", status="validating", raw_reply=text)
                    state.set_status(key, "researched", active=False)
                    return True
                if status == "failed":
                    self._fo_done(id_, status="failed", error="fo_research_failed")
                    state.set_status(key, "research_failed", active=False)
                    return False
                if status != "working" or time.monotonic() >= deadline:
                    self._fo_done(id_, status="needs_review", error="research_requires_review")
                    return False
                if not cid:
                    self._fo_done(id_, status="needs_review", error="research_requires_review")
                    return False  # never poll an unspecified account-wide conversation
                if self.stop.is_set():
                    return False
                self.store.update(id_, status="researching", polls=row["polls"]+1)
                try:
                    result = client.reply(cid, wait_seconds=20)
                except FoTimeout:
                    result = {"status": "working"}
        finally:
            client.close()

    @staticmethod
    def _ask(client, message, cid):
        try:
            return client.ask(message, cid)
        except FoTimeout:
            return {"status": "working"}


def found_by(result, researcher):
    for dealer in result["dealers"]:
        dealer["found_by"] = researcher
    return result


def merge(first, second):
    """First researcher's dealers win; the backup adds only new websites (a repeat counts as dropped)."""
    def site(dealer):
        return urlsplit(dealer["website"]).hostname.removeprefix("www.")
    seen = {site(d) for d in first["dealers"]}
    added = [d for d in second["dealers"] if site(d) not in seen]
    dealers = sorted(first["dealers"] + added, key=lambda d: (d["inventory_status"] == "unknown", d["distance_miles"]))
    return {"dealers": dealers,
            "dropped": first["dropped"] + second["dropped"] + len(second["dealers"]) - len(added)}
