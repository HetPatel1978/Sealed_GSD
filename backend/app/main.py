"""Local dashboard API and worker composition root. Start with python -m app.run."""

import asyncio
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.jobs import APIError, JobService
from app.schemas import Advance, AllowCalls, Approve, Cancel, CreateJob, Discover, Reject, Restore, Start
from app.security import ALLOWED_HOST_HEADERS, DashboardRequests, LoopbackOnly, dashboard_origin
from sealed.core.engine import StaleProposal
from sealed.env import REPO_ROOT, load_env
from sealed.ingest.deliver import DeliveryError
from sealed.parser.quote import ParserError
from sealed.private import vault
from sealed.private.runtime import for_run
from sealed.discovery.models import DiscoveryError, Request as DiscoveryRequest
from sealed.discovery.worker import Discovery

load_env()
VERSION = "0.2.0"
DASHBOARD_ORIGIN = dashboard_origin()


def create_app(*, data_dir: Path | None = None, mode=None, background=True, interval=15, fo_state: Path | None = None):
    mode = mode or os.getenv("SEALED_MODE") or "sim"
    if mode not in {"sim", "live"}:
        raise ValueError("SEALED_MODE must be sim or live")
    if not 0.1 <= interval <= 60:
        raise ValueError("Worker interval must be between 0.1 and 60 seconds")
    root = data_dir or (Path(os.environ["SEALED_API_DATA_DIR"]) if os.getenv("SEALED_API_DATA_DIR") else REPO_ROOT / ".sealed" / f"api_{mode}")

    async def invoke(function, *args):
        return await asyncio.get_running_loop().run_in_executor(api.state.executor, partial(function, *args))

    async def work():
        while True:
            await asyncio.sleep(interval)
            try:
                await invoke(api.state.service.tick)
                api.state.worker_status = "ready"
            except Exception:
                api.state.worker_status = "review_required"

    @asynccontextmanager
    async def lifespan(api):
        api.state.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sealed-account")
        api.state.worker_status = "ready"
        task, service, discovery = None, None, None
        try:
            service = await invoke(lambda: JobService(root, mode=mode, runtime=for_run,
                                                      fo_state=fo_state or REPO_ROOT / ".sealed/fo_state.json"))
            api.state.service = service
            discovery = Discovery(root, account=service.account, mode=mode, fo_state=service.fo_state)
            api.state.discovery = discovery
            service.discovery = discovery
            task = asyncio.create_task(work()) if background else None
            yield
        finally:
            if discovery:
                await asyncio.to_thread(discovery.close)
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if service:
                def cleanup():
                    try:
                        service.close()
                    finally:
                        for job_id in service.owned_jobs:
                            vault.forget(job_id)
                await invoke(cleanup)
            api.state.executor.shutdown(wait=True)

    api = FastAPI(title="Sealed", version=VERSION, lifespan=lifespan)
    api.add_middleware(DashboardRequests, origin=DASHBOARD_ORIGIN)
    api.add_middleware(CORSMiddleware, allow_origins=[DASHBOARD_ORIGIN], allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Last-Event-ID"])
    api.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOST_HEADERS)
    api.add_middleware(LoopbackOnly)

    @api.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Default validation responses include rejected values, including ceiling.
        return JSONResponse({"error": "validation_error"}, status_code=422)

    @api.exception_handler(APIError)
    async def application_error(request, error):
        return JSONResponse({"error": error.code}, status_code=error.status)

    @api.exception_handler(DiscoveryError)
    async def discovery_error(request, error):
        return JSONResponse({"error": "discovery_not_found" if str(error) == "discovery_not_found" else "discovery_unavailable"},
                            status_code=404 if str(error) == "discovery_not_found" else 503)

    @api.exception_handler(StaleProposal)
    async def stale_proposal(request, error):
        id_ = request.path_params.get("job_id")
        view = await call("view", id_) if id_ else {}
        return JSONResponse({"error": "stale_proposal", "current_proposal": view.get("proposal")}, status_code=409)

    @api.exception_handler(DeliveryError)
    @api.exception_handler(ParserError)
    @api.exception_handler(sqlite3.Error)
    async def integration_error(request, error):
        return JSONResponse({"error": "integration_review_required"}, status_code=503)

    async def call(method, *args):
        if not hasattr(api.state, "service"):
            raise APIError("service_not_started", 503)
        return await invoke(getattr(api.state.service, method), *args)

    @api.get("/health")
    async def health():
        integrations = await call("integration_status") if hasattr(api.state, "service") else {"fo": "not_configured", "gmail": "not_configured"}
        return {"status": "ok", "version": VERSION, "mode": mode,
                **integrations,
                "worker": getattr(api.state, "worker_status", "stopped")}

    @api.get("/jobs")
    async def jobs():
        return await call("list_jobs")

    @api.post("/jobs", status_code=201)
    async def create(command: CreateJob):
        def register():
            id_ = "job_" + uuid4().hex[:16]
            vault.store(id_, command.ceiling)
            try:
                return api.state.service.create(id_, command.model_dump(mode="json"))
            except BaseException:
                vault.forget(id_)
                raise
        return await invoke(register)

    @api.get("/jobs/{job_id}")
    async def job(job_id: str):
        return await call("view", job_id)

    @api.get("/jobs/{job_id}/ceiling")
    async def ceiling(job_id: str):
        def read():
            eng = api.state.service.engine(job_id)
            return {"job_id": job_id, "ceiling": vault.for_owner(job_id), "currency": eng.job.currency}
        return JSONResponse(await invoke(read), headers={"Cache-Control": "no-store"})

    @api.post("/jobs/{job_id}/private-runtime")
    async def restore(job_id: str, command: Restore):
        def apply():
            svc = api.state.service
            eng = svc.engine(job_id)
            if vault.has(job_id):
                raise APIError("private_runtime_already_set")
            if command.ceiling <= eng.job.target:
                raise APIError("validation_error", 422)
            vault.store(job_id, command.ceiling)
            svc.owned_jobs.add(job_id)
            return {"restored": True}
        return await invoke(apply)

    @api.post("/jobs/{job_id}/start")
    async def start(job_id: str, command: Start = Start()):
        return await call("start", job_id, command.model_dump())

    @api.post("/jobs/{job_id}/approve")
    async def approve(job_id: str, command: Approve):
        return await call("approve", job_id, command.model_dump(mode="json"))

    @api.post("/jobs/{job_id}/reject")
    async def reject(job_id: str, command: Reject):
        return await call("reject", job_id, command.proposal_id, command.reason)

    @api.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str, command: Cancel = Cancel()):
        return await call("cancel", job_id)

    @api.post("/jobs/{job_id}/allow-calls")
    async def allow_calls(job_id: str, command: AllowCalls):
        return await call("allow_calls", job_id)

    @api.get("/jobs/{job_id}/receipt")
    async def receipt(job_id: str):
        return await call("receipt", job_id)

    @api.get("/jobs/{job_id}/events")
    async def events(job_id: str, request: Request, last_event_id: int = Query(default=0, ge=0), follow: bool = True):
        try:
            cursor = int(request.headers.get("Last-Event-ID", last_event_id))
        except ValueError:
            raise APIError("invalid_event_cursor", 422) from None
        view = await call("view", job_id)
        if not 0 <= cursor <= view["last_event_id"]:
            raise APIError("invalid_event_cursor", 422)

        async def stream():
            nonlocal cursor
            last_ping = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                batch = await call("events", job_id, cursor)
                for event in batch:
                    yield f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    cursor = event["id"]
                if not follow:
                    break
                now = asyncio.get_running_loop().time()
                if now - last_ping >= 15:
                    yield ": ping\n\n"
                    last_ping = now
                await asyncio.sleep(.5)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @api.post("/sim/advance")
    async def advance(command: Advance):
        if mode != "sim":
            raise APIError("not_found", 404)
        return await call("advance", command.job_id, command.steps)

    @api.post("/dealers/discover", status_code=202)
    async def discover(command: Discover):
        if mode == "live" and not command.allow_fo_research:
            raise APIError("discovery_permission_required")
        public = DiscoveryRequest.model_validate(command.model_dump(exclude={"allow_fo_research"}))
        return await asyncio.to_thread(api.state.discovery.submit, public)

    @api.get("/dealers/discover/{search_id}")
    async def discovery_status(search_id: str):
        return await asyncio.to_thread(api.state.discovery.store.view, search_id)

    return api


app = create_app()
