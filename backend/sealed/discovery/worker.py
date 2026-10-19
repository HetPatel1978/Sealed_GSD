"""Background, durable Fo-only discovery; HTTP polling never waits for a Fo turn."""

import json
import threading
import time
from datetime import datetime, timezone

from sealed.discovery.models import DiscoveryError, INLINE_REQUEST, Request, parse_report, prompt
from sealed.discovery.sources import PublicWeb, validate_candidates
from sealed.discovery.store import Store
from sealed.fo.adapter import Store as FoStore, _TURN_LOCK
from sealed.fo.client import FoTimeout, LiveFoClient


class Discovery:
    def __init__(self, root, *, account, mode, fo_state, client_factory=None, web_factory=None):
        self.mode, self.fo_state = mode, fo_state
        self.store = Store(root / "discovery.sqlite3", f"{account}:{fo_state.resolve()}:{mode}", mode)
        self.client_factory, self.web_factory = client_factory or LiveFoClient, web_factory or PublicWeb
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="sealed-discovery")
        self.thread.start()

    def submit(self, request):
        retry_unattempted = False
        if self.mode == "live" and _TURN_LOCK.acquire(blocking=False):
            try:
                retry_unattempted = not FoStore(self.fo_state).data.get("active")
            finally:
                _TURN_LOCK.release()
        id_, cached = self.store.create(request, retry_unattempted=retry_unattempted)
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
        if row["phase"] != "validate":
            while not self.stop.is_set():
                if _TURN_LOCK.acquire(timeout=.2):
                    try:
                        if not self._research(id_, request):
                            return
                    finally:
                        _TURN_LOCK.release()
                    break
            if self.stop.is_set():
                return
        self.store.update(id_, status="validating")
        row = self.store.get(id_)
        try:
            result = validate_candidates(request, parse_report(row["raw_reply"]), self.web_factory())
            self.store.update(id_, status="complete", result=json.dumps(result), error=None)
        except DiscoveryError as error:
            self.store.update(id_, status="failed", error=str(error))

    def _research(self, id_, request):
        key = FoStore.key("dealer-discovery", id_, "research")
        state = FoStore(self.fo_state)
        row = self.store.get(id_)
        if state.data.get("active") not in (None, key):
            # Preserve any existing uncertain send/research reservation unchanged.
            self.store.update(id_, status="needs_review", error="fo_account_requires_review")
            return False
        if row["phase"] in {"ask_pending", "inline_pending"} and not row["conversation_id"]:
            self.store.update(id_, status="needs_review", error="research_requires_review")
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
                            self.store.update(id_, status="needs_review", error="invalid_report")
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
                    self.store.update(id_, status="failed", error="fo_research_failed")
                    state.set_status(key, "research_failed", active=False)
                    return False
                if status != "working" or time.monotonic() >= deadline:
                    self.store.update(id_, status="needs_review", error="research_requires_review")
                    return False
                if not cid:
                    self.store.update(id_, status="needs_review", error="research_requires_review")
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
