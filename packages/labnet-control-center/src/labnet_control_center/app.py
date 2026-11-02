"""The LabNet Control Center: a web front end that is itself a LabNet client.

    labnet-control-center --env-file control-center.env

The browser only talks to this process. This process holds the Central API key
and asks Central for the network picture, so no key ever reaches a browser.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictInt
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware

from labnet.client import AsyncLabClient, LabPCInfo
from labnet.client.core.models import DeviceInfo
from labnet.contracts import registry
from labnet.identifiers import LABNET_IDENTIFIER_REGEX

from .agent import MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS, AgentRequestError, PhotoAgent
from .automation_api import create_automation_router
from .automation_grants import GrantManager
from .automation_literature import Literature
from .automation_runner_api import create_runner_router
from .automation_runner_api import create_gateway_router
from .automation_runners import RunnerRegistry
from .automation_runs import RunStore, sweep_forever
from .automation_store import PROJECT_ID, AutomationStore
from .config import ControlCenterSettings
from .contracts import clean_hints, describe_device_type, driver_status, facade_class_name, type_label
from .experiment_api import create_experiment_router
from .experiment_schema import EXPERIMENT_ID
from .experiment_store import ExperimentError, ExperimentStore
from .ingress import HumanIngress
from .panels import driver_panel, panel_for
from labnet.redaction import redact
from .request_checks import request_user
from .scripting import MAX_SCRIPT_CHARACTERS
from .sessions import MAX_CALL_SECONDS, SessionError, SessionManager


LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
AGENT_NAME = "Photo-Agent"
# First path segments the app itself uses; a device with one of these ids
# could not have a control page at /<device-id>.
RESERVED_PATHS = frozenset(
    {"api", "automation", "devices", "experiments", "favicon.ico", "health", "static", "status"}
)
# Pages: scripts and styles only from this server, no plugins, not framed by
# other sites. (No page has inline scripts; inline styles are set from JS.)
PAGE_POLICY = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
)
ClientFactory = Callable[[ControlCenterSettings], AsyncLabClient]
AgentFactory = Callable[..., PhotoAgent]
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _default_client(settings: ControlCenterSettings) -> AsyncLabClient:
    return AsyncLabClient(
        settings.server_url,
        settings.api_key,
        ca_file=settings.ca_file,
        authenticate_on_start=False,
    )


class NetworkStatus:
    """Cached network picture, refreshed from Central at most every N seconds.

    However many browsers poll, Central sees one request per interval. If
    Central stops answering, the last good picture is kept and marked stale.
    """

    def __init__(self, client: AsyncLabClient, settings: ControlCenterSettings) -> None:
        self._client = client
        self._settings = settings
        self._lock = asyncio.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._fetched_at = 0.0

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            if (
                self._snapshot is not None
                and time.monotonic() - self._fetched_at < self._settings.refresh_seconds
            ):
                return self._snapshot
            try:
                lab_pcs = await self._client.list_lab_pcs()
            except Exception as exc:  # show any failure on the page instead of a 500
                LOGGER.warning("Could not read the network from Central: %s", exc)
                previous = self._snapshot or _snapshot([], central_url=self._settings.server_url)
                self._snapshot = {
                    **previous,
                    "central": {
                        "url": self._settings.server_url,
                        "ok": False,
                        "error": redact(f"{type(exc).__name__}: {exc}"),
                    },
                }
            else:
                self._snapshot = _snapshot(lab_pcs, central_url=self._settings.server_url)
            self._fetched_at = time.monotonic()
            return self._snapshot

    def invalidate(self) -> None:
        """Ask Central again on the next request (a lease was just taken or freed)."""

        self._fetched_at = 0.0

    @property
    def central_ok(self) -> bool:
        return bool(self._snapshot and self._snapshot["central"]["ok"])


def _snapshot(lab_pcs: Sequence[LabPCInfo], *, central_url: str) -> dict[str, Any]:
    pcs = [_lab_pc(pc) for pc in lab_pcs]
    devices = [device for pc in pcs for device in pc["devices"]]
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "central": {"url": central_url, "ok": True, "error": None},
        "summary": {
            "lab_pcs": len(pcs),
            "online": sum(pc["online"] for pc in pcs),
            "devices": len(devices),
            "available": sum(device["state"] == "available" for device in devices),
            "leased": sum(device["state"] == "leased" for device in devices),
        },
        "lab_pcs": pcs,
    }


def _lab_pc(pc: LabPCInfo) -> dict[str, Any]:
    target = pc.endpoint.target
    address, _, port = target.rpartition(":")
    return {
        "id": pc.manager_id,
        "host": pc.host,
        "address": address.strip("[]") or target,
        "port": port,
        "secure": pc.endpoint.secure,
        "online": pc.online,
        "last_seen": pc.last_seen.isoformat() if pc.last_seen else None,
        "devices": [_device(device, lab_pc_online=pc.online) for device in pc.devices],
    }


def _device(device: DeviceInfo, *, lab_pc_online: bool) -> dict[str, Any]:
    if device.effective_available:
        state, detail = "available", "Ready to lease"
    elif device.leased:
        state, detail = "leased", "In use by another client"
    elif not lab_pc_online:
        state, detail = "unavailable", "Lab PC is offline"
    else:
        state = "unavailable"
        detail = device.status_text or f"{device.runtime_state}, {device.availability}"
    methods = describe_device_type(device.device_type)
    contract = registry.find(device.device_type)
    panel, panel_url = panel_for(device.device_type)
    return {
        # Which control panel the device page loads (see panels.py), and from where.
        "panel": panel,
        "panel_url": panel_url,
        "id": device.device_id,
        "type": device.device_type,
        "type_label": type_label(device.device_type),
        "state": state,
        "detail": detail,
        "runtime_state": device.runtime_state,
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "control_url": f"/{device.device_id}",
        "known_contract": methods is not None,
        "methods": methods or [],
        # The class a Python client uses for it, e.g. DummyAOM.
        "python_class": facade_class_name(contract) if contract is not None else None,
        # Channels, ranges and choices the driver publishes (contracts don't say).
        "hints": clean_hints(device.metadata),
        # Whether the Lab PC's driver for the type is this Control Center's (ok, missing, mismatch).
        "driver": driver_status(device.device_type, device.metadata),
    }


def _with_activity(snapshot: dict[str, Any], held: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Mark the devices this Control Center holds, so any viewer can pause them.

    ``control_center`` is null for every other device; a leased device without
    it is held by some other LabNet client. A device an Automation grant holds
    has ``control_center.automation`` (project, task, end time and the Stop URL).
    """

    def device(item: dict[str, Any]) -> dict[str, Any]:
        activity = held.get(item["id"])
        if activity is None:
            return {**item, "control_center": None}
        automation = activity.get("automation")
        return {
            **item,
            "state": "leased",  # the cached picture may predate the lease
            "detail": f"Held by Automation ({automation['label']})" if automation else "Held by the Control Center",
            "control_center": {**activity, "page": activity.get("page") or f"/{activity['session']}"},
        }

    pcs = [{**pc, "devices": [device(item) for item in pc["devices"]]} for pc in snapshot["lab_pcs"]]
    devices = [item for pc in pcs for item in pc["devices"]]
    summary = {
        **snapshot["summary"],
        "available": sum(item["state"] == "available" for item in devices),
        "leased": sum(item["state"] == "leased" for item in devices),
    }
    return {**snapshot, "summary": summary, "lab_pcs": pcs}


# Base64 makes a file a third larger.
_MAX_ATTACHMENT_BASE64 = (MAX_ATTACHMENT_BYTES + 2) // 3 * 4


class AgentAttachment(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    data: str = Field(max_length=_MAX_ATTACHMENT_BASE64)  # the file, base64-encoded


class AgentMessage(BaseModel):
    message: str = Field(default="", max_length=8000)
    # From an earlier answer's events: continue that conversation.
    conversation_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    model: str | None = Field(default=None, max_length=100)
    effort: str | None = Field(default=None, max_length=20)
    attachments: list[AgentAttachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    # The experiment on the person's canvas: Photo-Agent sees it (as saved).
    experiment_id: str | None = Field(default=None, pattern=rf"^{EXPERIMENT_ID.pattern}$")
    # The Automation project the person has open: Photo-Agent gets a copy of its text files.
    project_id: str | None = Field(default=None, pattern=rf"^{PROJECT_ID.pattern}$")


class _SecurityHeaders:
    """nosniff on every response, and the page policy on HTML (pure ASGI)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _value in headers}
                if b"x-content-type-options" not in names:
                    headers.append((b"x-content-type-options", b"nosniff"))
                content_type = dict(headers).get(b"content-type", b"")
                if content_type.startswith(b"text/html") and b"content-security-policy" not in names:
                    headers.append((b"content-security-policy", PAGE_POLICY.encode("ascii")))
                    headers.append((b"referrer-policy", b"same-origin"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)


class _BodyLimit:
    """Refuse an oversized request (pure ASGI, so streamed responses and
    disconnects pass through untouched).

    A Content-Length over the limit is refused before anything is read. A body
    sent without one (chunked) is counted as it arrives; past the limit,
    reading it raises a 413 HTTPException, which FastAPI answers.
    """

    def __init__(
        self,
        app: Any,
        *,
        path: str,
        limit: int,
        prefix: bool = False,
        message: str = "The attached files are too large.",
    ) -> None:
        self.app = app
        self.path = path
        self.limit = limit
        self.prefix = prefix
        self.message = message

    def _applies(self, path: str) -> bool:
        return path == self.path or (self.prefix and path.startswith(self.path + "/"))

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self._applies(scope["path"]):
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length", "0")
        if length.isdigit() and int(length) > self.limit:
            response = JSONResponse({"detail": self.message}, status_code=413)
            await response(scope, receive, send)
            return
        received = 0

        async def counted() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise HTTPException(status_code=413, detail=self.message)
            return message

        await self.app(scope, counted, send)


class SettingValue(BaseModel):
    method: str = Field(max_length=128)
    value: float


class StreamOptions(BaseModel):
    method: str = Field(max_length=128)
    sample_rate_hz: float
    channel: str | None = Field(default=None, max_length=128)


class PlayRequest(BaseModel):
    """Settings are applied in order before the stream starts."""

    settings: list[SettingValue] = Field(default_factory=list, max_length=32)
    stream: StreamOptions


class ScriptRequest(BaseModel):
    code: str = Field(min_length=1, max_length=MAX_SCRIPT_CHARACTERS)


CallArgument = StrictBool | StrictInt | Annotated[float, Field(strict=True, allow_inf_nan=False)] | Annotated[
    str, Field(max_length=1024)
]


class CallRequest(BaseModel):
    """One method for a control panel: its arguments by name, and how long it may take."""

    method: str = Field(min_length=1, max_length=128)
    args: dict[str, CallArgument] = Field(default_factory=dict, max_length=32)
    timeout_seconds: float = Field(default=30.0, gt=0, le=MAX_CALL_SECONDS)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def _coalesce(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge neighbouring sample batches so a burst becomes one message."""

    merged: list[dict[str, Any]] = []
    for event in events:
        if event["type"] == "samples" and merged and merged[-1]["type"] == "samples":
            merged[-1] = {"type": "samples", "points": merged[-1]["points"] + event["points"]}
        else:
            merged.append(event)
    return merged


def create_app(
    settings: ControlCenterSettings,
    *,
    client_factory: ClientFactory = _default_client,
    agent_factory: AgentFactory = PhotoAgent,
) -> FastAPI:
    async def network_with_activity() -> dict[str, Any]:
        # A device a grant holds can't also be a session's (sessions refuse it), but the grant wins.
        grants = app.state.grants.activity() if settings.automation_enabled else {}
        return _with_activity(await app.state.status.snapshot(), {**app.state.sessions.activity(), **grants})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = client_factory(settings)
        app.state.status = NetworkStatus(client, settings)
        # Play and scripts are refused on a device an Automation grant holds.
        blocked = (lambda device_id: app.state.grants.blocked(device_id)) if settings.automation_enabled else None
        app.state.sessions = SessionManager(client, settings, blocked=blocked)
        app.state.experiments = ExperimentStore(settings.experiments_dir)
        app.state.agent = agent_factory(settings, network_with_activity) if settings.photo_agent_enabled else None
        if not settings.automation_enabled:
            try:
                yield
            finally:
                if app.state.agent is not None:
                    app.state.agent.close()
                await app.state.sessions.close()  # releases every lease
                await client.aclose()
            return
        app.state.automation = AutomationStore(settings.automation_dir, settings.agent_literature_dir)
        app.state.runners = RunnerRegistry(app.state.automation.state / "runners")
        app.state.literature = Literature(app.state.automation)  # its indexing thread starts on first use
        app.state.runs = RunStore(
            app.state.automation, quota_bytes=int(settings.automation_run_quota_mb * 2**20),
            stale_minutes=settings.automation_run_stale_minutes, models=settings.agent_models,
            default_model=settings.agent_model, default_effort=settings.agent_effort,
            approval_hours=settings.automation_approval_hours,
            grant_max_minutes=settings.automation_grant_max_minutes,
        )
        # Device grants (automation_grants.py): ones a restart cut off are marked interrupted, and
        # a run that stops being active (from any thread) has its grant ended on this loop.
        app.state.grants = GrantManager(client, app.state.automation, app.state.runs, settings)
        await asyncio.to_thread(app.state.grants.mark_interrupted)
        app.state.grants.start_monitor()  # follows grants a restart handed to the safe-state watchdog
        loop = asyncio.get_running_loop()
        grant_endings: set[asyncio.Task[Any]] = set()

        def end_grant_of(_project_id: str, run_id: str, status: str) -> None:
            def schedule() -> None:
                task = loop.create_task(app.state.grants.end_for_run(run_id, reason=f"the run ended ({status})"))
                grant_endings.add(task)
                task.add_done_callback(grant_endings.discard)

            with suppress(RuntimeError):  # the loop has closed: shutdown ends every grant anyway
                loop.call_soon_threadsafe(schedule)

        app.state.runs.on_run_ended = end_grant_of
        # Fails runs whose runner went quiet, even when nobody is looking at them.
        app.state.run_sweeper = asyncio.create_task(sweep_forever(app.state.runs), name="automation-run-sweeper")
        try:
            yield
        finally:
            app.state.run_sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await app.state.run_sweeper
            await asyncio.to_thread(app.state.literature.close)  # stops a running PDF extractor
            if app.state.agent is not None:
                app.state.agent.close()
            await app.state.grants.close()  # safe state and release first, while the client is open
            if grant_endings:
                await asyncio.gather(*grant_endings, return_exceptions=True)
            await app.state.sessions.close()  # releases every lease
            await client.aclose()

    app = FastAPI(
        title=settings.title,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.add_middleware(
        _BodyLimit, path="/api/agent/messages", limit=MAX_ATTACHMENTS * _MAX_ATTACHMENT_BASE64 + 64 * 1024
    )
    app.add_middleware(
        _BodyLimit, path="/api/experiments", prefix=True, limit=512 * 1024,
        message="This experiment is too large to save.",
    )
    max_file_bytes = int(settings.automation_max_file_mb * 1024 * 1024)
    app.add_middleware(
        _BodyLimit, path="/api/automation", prefix=True, limit=max_file_bytes + 64 * 1024,
        message=f"Files can be at most {settings.automation_max_file_mb:g} MB.",
    )
    # Outermost last: security headers on every reply, then the host check,
    # then logins (see ingress.py), then the body limits above.
    app.add_middleware(HumanIngress, secret=settings.proxy_secret)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts), www_redirect=False)
    app.add_middleware(_SecurityHeaders)
    app.include_router(create_experiment_router(network_with_activity))
    if settings.automation_enabled:  # off: no Automation routes at all
        app.include_router(create_automation_router(network_with_activity, max_file_bytes=max_file_bytes))
        app.include_router(create_gateway_router())  # grant tokens, not runner tokens
        app.include_router(create_runner_router(max_file_bytes=max_file_bytes, network=network_with_activity))

    @app.exception_handler(SessionError)
    async def session_error(_request: Request, exc: SessionError) -> JSONResponse:
        return JSONResponse({"detail": redact(str(exc))}, status_code=exc.status_code)

    @app.exception_handler(ExperimentError)
    async def experiment_error(_request: Request, exc: ExperimentError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), **exc.extra}, status_code=exc.status_code)

    def session_for(device_id: str):
        if LABNET_IDENTIFIER_REGEX.fullmatch(device_id) is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return app.state.sessions.get(device_id)

    def page(name: str) -> FileResponse:
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse("/status")

    @app.get("/status", include_in_schema=False)
    async def status_page() -> FileResponse:
        return page("status.html")

    @app.get("/devices", include_in_schema=False)
    async def devices_page() -> FileResponse:
        return page("devices.html")

    @app.get("/experiments", include_in_schema=False)
    async def experiments_page() -> FileResponse:
        return page("experiments.html")

    @app.get("/experiments/{experiment_id}", include_in_schema=False)
    async def experiment_page(experiment_id: str) -> FileResponse:
        """The canvas for one experiment, e.g. /experiments/aom-scan-k7q2mx."""

        if EXPERIMENT_ID.fullmatch(experiment_id) is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return page("experiment.html")

    if settings.automation_enabled:

        @app.get("/automation", include_in_schema=False)
        async def automation_page() -> FileResponse:
            return page("automation.html")

        @app.get("/automation/{project_id}", include_in_schema=False)
        async def automation_project_page(project_id: str) -> FileResponse:
            """One project's files, linked experiments and tasks, e.g. /automation/aom-study-k7q2mx."""

            if PROJECT_ID.fullmatch(project_id) is None:
                raise HTTPException(status_code=404, detail="Not found.")
            return page("automation-project.html")

    else:

        @app.get("/automation/{rest:path}", include_in_schema=False)
        async def automation_off(rest: str) -> Response:
            # Without this, /automation would be taken for a device id (/{device_id}).
            raise HTTPException(status_code=404, detail="Automation is not enabled on this Control Center.")

    features = {
        "site_name": settings.site_name,
        "title": settings.title,
        "automation": settings.automation_enabled,
        "photo_agent": settings.photo_agent_enabled,
    }

    @app.get("/api/features")
    async def feature_list() -> JSONResponse:
        """What this Control Center offers; the pages show only these tabs and panels."""

        return JSONResponse(features, headers={"Cache-Control": "no-store"})

    @app.get("/api/features.js", include_in_schema=False)
    async def feature_script() -> Response:
        # Loaded before shell.js, so the page frame is built right the first time (no flicker).
        return Response(
            f"window.LabNetFeatures = {json.dumps(features)};\n",
            media_type="text/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        # Answer here so browsers' automatic request never reaches /{device_id}.
        return Response(status_code=204)

    @app.get("/api/status")
    async def network_status() -> JSONResponse:
        snapshot = await network_with_activity()
        return JSONResponse(
            {**snapshot, "refresh_seconds": settings.refresh_seconds},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/me")
    async def me(request: Request) -> JSONResponse:
        """Who is signed in (through Caddy), and whether this Control Center has logins at all."""

        user = request_user(request)
        return JSONResponse(
            {"user": user, "logins": settings.proxy_secret is not None},
            headers={"Cache-Control": "no-store"},
        )

    # Photo-Agent's chat API, only when LABNET_PHOTO_AGENT_ENABLED is on.
    if settings.photo_agent_enabled:

        @app.get("/api/agent")
        async def agent_info() -> dict[str, Any]:
            return {"agent": AGENT_NAME, **app.state.agent.info()}

        @app.post("/api/agent/messages", response_model=None)
        async def agent_message(body: AgentMessage) -> StreamingResponse | dict[str, Any]:
            """Server-sent events: session, delta (answer text), activity, status, done.

            A request the agent refuses (unknown model, unreadable file, ...) is
            answered with an error status before anything streams.
            """

            agent = app.state.agent
            if not agent.enabled:
                if not body.message.strip():
                    raise HTTPException(status_code=422, detail="Message is empty.")
                return {
                    "agent": AGENT_NAME,
                    "connected": False,
                    "reply": f"{AGENT_NAME} isn't connected: {agent.info()['reason']}",
                }
            try:
                files = [(item.name, base64.b64decode(item.data, validate=True)) for item in body.attachments]
            except (binascii.Error, ValueError):
                raise HTTPException(status_code=422, detail="An attachment is not valid base64.") from None
            experiment = None
            if body.experiment_id:
                experiment = await asyncio.to_thread(app.state.experiments.get, body.experiment_id)  # 404 if gone
            project = None
            if body.project_id:
                if not settings.automation_enabled:
                    raise HTTPException(status_code=404, detail="Automation is not enabled on this Control Center.")
                project = await asyncio.to_thread(app.state.automation.agent_snapshot, body.project_id)  # 404 if gone
            try:
                request = agent.prepare(
                    body.message,
                    body.conversation_id,
                    model=body.model,
                    effort=body.effort,
                    attachments=files,
                    experiment=experiment,
                    project=project,
                )
            except AgentRequestError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

            async def stream():
                # Closing this generator (the browser left or pressed Stop) ends
                # ask(), which kills the claude process.
                async for event in agent.ask(request):
                    yield _sse(event)

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

    # ---- device control (one session per device) --------------------------------

    @app.get("/api/devices/{device_id}/events")
    async def device_events(device_id: str) -> StreamingResponse:
        """Server-sent events: a snapshot, then state, samples and script output."""

        session = session_for(device_id)
        subscriber = session.subscribe()

        async def stream():
            try:
                yield "retry: 2000\n\n" + _sse(session.snapshot_event())
                while not subscriber.dropped:
                    try:
                        first = await asyncio.wait_for(subscriber.queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    events = [first]
                    while not subscriber.queue.empty() and len(events) < 1000:
                        events.append(subscriber.queue.get_nowait())
                    yield "".join(_sse(event) for event in _coalesce(events))
            finally:
                session.unsubscribe(subscriber)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/devices/{device_id}/play")
    async def device_play(device_id: str, body: PlayRequest) -> dict[str, Any]:
        try:
            return await session_for(device_id).play(body.model_dump())
        finally:
            app.state.status.invalidate()

    @app.post("/api/devices/{device_id}/pause")
    async def device_pause(device_id: str) -> dict[str, Any]:
        state = await session_for(device_id).pause()
        app.state.status.invalidate()
        return state

    @app.post("/api/devices/{device_id}/release")
    async def device_release(device_id: str) -> dict[str, Any]:
        """Pause from the Network or Devices page: stops the stream and any script.

        Open to every viewer on purpose, so a page left running on some other
        computer can be stopped by whoever notices.
        """

        state = await session_for(device_id).release("Paused from the device list.")
        app.state.status.invalidate()
        return state

    @app.post("/api/devices/{device_id}/call")
    async def device_call(device_id: str, body: CallRequest) -> dict[str, Any]:
        """One method for the page's panel (a move, a setting, one spectrum): ``{"value": result}``.

        It shares the page's lease, which it takes if needed; Pause releases it.
        The Lab PC checks the arguments against the driver's and the site's limits.
        """

        try:
            value = await session_for(device_id).call(body.method, body.args, body.timeout_seconds)
        finally:
            app.state.status.invalidate()
        return {"value": value}

    @app.get("/api/panels/{device_type}.js", include_in_schema=False)
    async def panel_script(device_type: str) -> Response:
        """The control panel a driver package ships for its device type (see panels.py)."""

        source = driver_panel(device_type) if LABNET_IDENTIFIER_REGEX.fullmatch(device_type) else None
        if source is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return Response(source, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.post("/api/devices/{device_id}/script")
    async def device_script(device_id: str, body: ScriptRequest) -> dict[str, Any]:
        return await session_for(device_id).run_script(body.code)

    @app.post("/api/devices/{device_id}/script/stop")
    async def device_script_stop(device_id: str) -> dict[str, Any]:
        session = session_for(device_id)
        session.stop_script()
        return session.state_event()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive", "service": "labnet-control-center"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        await app.state.status.snapshot()
        ok = app.state.status.central_ok
        return JSONResponse(
            {"status": "ok" if ok else "central_unreachable"},
            status_code=200 if ok else 503,
        )

    # Registered last: every fixed path above wins over a device id.
    @app.get("/{device_id}", include_in_schema=False)
    async def device_page(device_id: str) -> FileResponse:
        """The device control page, e.g. /dev02-wavemeter."""

        if device_id in RESERVED_PATHS or LABNET_IDENTIFIER_REGEX.fullmatch(device_id) is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return page("device.html")

    return app


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="labnet-control-center",
        description="Serve the LabNet Control Center web interface.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Settings file such as control-center.env (process environment wins).",
    )
    arguments = parser.parse_args(argv)
    try:
        settings = ControlCenterSettings.from_env(arguments.env_file)
    except (ValueError, FileNotFoundError) as exc:
        parser.exit(2, f"labnet-control-center: error: {exc}\n")
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.port)


__all__ = ["NetworkStatus", "create_app", "main"]
