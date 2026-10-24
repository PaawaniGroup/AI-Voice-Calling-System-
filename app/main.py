"""MME_TELEPHONY application factory."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from app.api import routes, routes_live, routes_platform
from app.ai.media import UnavailableBridge, WsForkBridge
from app.ai.service import AiService
from app.services.sessions import SessionService
from app.core.errors import ApiError
from app.core.logging import RingHandler, correlation_id_var, request_id_var, setup_logging
from app.events.webhooks import WebhookEmitter, WebhookWorker
from app.observability import metrics as m
from app.observability.activity import ActivityBus
from app.persistence.db import Database
from app.persistence.repos import Repos
from app.providers.base import FreeSwitchGatewayProvider, MockProvider
from app.providers.plivo import PlivoApiProvider
from app.security.auth import RateLimiter
from app.services import recordings as rec_svc
from app.telephony.controller import Controller
from app.telephony.esl import EslCommandClient, EslEventClient
from config.settings import Settings, get_settings

log = logging.getLogger("mme_telephony")


def build_provider(s: Settings, esl_cmd):
    if s.provider == "mock":
        return MockProvider()
    if s.provider == "plivo_api":
        return PlivoApiProvider(s)
    return FreeSwitchGatewayProvider(esl_cmd, s)


def create_app(settings: Settings | None = None, *, db: Database | None = None, provider=None, ai_transport=None,
               providers: dict | None = None) -> FastAPI:
    """`providers`: extra provider adapters keyed by call.provider (dependency injection for tests / future providers)."""
    s = settings or get_settings()
    setup_logging(s.log_level, s.log_format, s.app_name, s.env)
    errlog = RingHandler(s.error_log_buffer)
    logging.getLogger().addHandler(errlog)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = db or Database(s)
        if db is None:
            await database.connect()
        repos = Repos(database, s)
        esl_cmd = esl_events = None
        if s.fs_enabled:
            common = dict(connect_timeout=s.esl_connect_timeout_s, reconnect_initial=s.esl_reconnect_initial_s,
                          reconnect_max=s.esl_reconnect_max_s, jitter=s.esl_reconnect_jitter)
            esl_cmd = EslCommandClient(s.esl_host, s.esl_port, s.esl_password.get_secret_value(),
                                       command_timeout=s.esl_command_timeout_s, **common)
        prov = provider or build_provider(s, esl_cmd)
        emitter = WebhookEmitter(database, s)
        ctrl = Controller(s, repos, prov, emitter, esl_cmd, None)
        if s.plivo_inbound_enabled and prov.name != "plivo_api":
            # Calls delivered by Plivo are controlled through Plivo (REST hangup), never through the FreeSWITCH gateway,
            # while outbound calling keeps using the configured provider.
            ctrl.providers["plivo_api"] = PlivoApiProvider(s)
        ctrl.providers.update(providers or {})
        if s.fs_enabled:
            esl_events = EslEventClient(s.esl_host, s.esl_port, s.esl_password.get_secret_value(),
                                        handler=ctrl.on_esl_event, on_connect=ctrl.on_esl_connected,
                                        queue_size=s.esl_event_queue_size, concurrency=s.esl_event_concurrency,
                                        idle_timeout=s.esl_idle_timeout_s, **common)
            ctrl.esl_events = esl_events
        wworker = WebhookWorker(database, s)
        sessions = SessionService(database, s, emitter)
        bridge = WsForkBridge(esl_cmd, s) if s.ai_media_bridge == "ws_fork" else UnavailableBridge()
        ai = AiService(database, repos, s, sessions, emitter, bridge, transport=ai_transport)
        ctrl.sessions, ctrl.ai = sessions, ai
        app.state.sessions, app.state.ai = sessions, ai
        repos.activity = sessions.activity = ai.activity = wworker.activity = app.state.activity  # live dashboard feed
        storage = rec_svc.make_storage(s)
        app.state.repos, app.state.controller, app.state.webhook_worker = repos, ctrl, wworker
        app.state.storage, app.state.db = storage, database
        app.state.draining = False
        m.DRAINING.set(0)

        async def drain(timeout: float | None = None) -> dict:
            """Stop accepting new inbound calls (readiness 503, Plivo answers get <Hangup busy>), give live AI conversations
            `drain_timeout_s` to end on their own, then close the rest and ask the provider to hang those callers up so
            nobody is left in silence. Idempotent; also runs on shutdown."""
            if not app.state.draining:
                app.state.draining = True
                m.DRAINING.set(1)
                log.info("draining: no new calls will be accepted")
            end = asyncio.get_running_loop().time() + (s.drain_timeout_s if timeout is None else timeout)
            while ai.runtime and asyncio.get_running_loop().time() < end:
                await asyncio.sleep(0.25)
            stopped = await ai.stop_all("service_shutdown")
            hung = 0
            for tenant, sid in stopped:
                call = await database["calls"].find_one({"tenant_id": tenant, "session_id": sid})
                if call and call["state"] not in ("completed", "failed", "cancelled", "busy", "no_answer"):
                    hung += 1 if await ctrl._kill_quietly(call, "NORMAL_CLEARING") else 0
            if stopped:
                log.warning("drain closed %d AI conversation(s); %d provider hangup(s) requested", len(stopped), hung)
            return {"stopped": len(stopped), "hangups_requested": hung}

        app.state.drain = drain
        try:  # a restart leaves AI sessions / streams of the previous process behind: resolve them explicitly
            rec = await ai.recover_on_startup()
            rec.update(await ctrl.recover_provider_streams())
            if any(rec.values()):
                log.warning("startup recovery: %s", rec)
        except Exception:
            log.exception("startup recovery failed (service continues; sweepers cover the remainder)")
        if esl_cmd:
            await esl_cmd.start()
        if esl_events:
            await esl_events.start()
        ctrl.start()
        wworker.start()
        housekeeping = asyncio.create_task(_housekeeping(s, repos, storage, wworker))
        log.info("service started")
        try:
            yield
        finally:
            try:
                await drain()
            except Exception:
                log.exception("drain failed")
            housekeeping.cancel()
            await asyncio.gather(housekeeping, return_exceptions=True)
            await wworker.stop()
            await ai.shutdown()
            await ctrl.stop()
            if esl_events:
                await esl_events.stop()
            if esl_cmd:
                await esl_cmd.stop()
            if db is None:
                await database.close()
            log.info("service stopped")

    app = FastAPI(title="MME Telephony", version=s.app_version, lifespan=lifespan,
                  docs_url="/docs" if s.docs_enabled else None, redoc_url=None,
                  openapi_url="/openapi.json" if s.docs_enabled else None)
    app.state.settings = s
    app.state.limiter = RateLimiter()
    app.state.activity = ActivityBus()
    app.state.errlog = errlog
    app.state.last_ok = {}
    if s.csv(s.cors_origins):
        app.add_middleware(CORSMiddleware, allow_origins=s.csv(s.cors_origins), allow_methods=["GET", "POST", "PUT", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "Idempotency-Key"], allow_credentials=False)

    @app.middleware("http")
    async def mw(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        request_id_var.set(rid)
        correlation_id_var.set(request.headers.get("x-correlation-id", "")[:64] or rid)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > s.max_body_bytes:
            return JSONResponse({"error": {"code": "payload_too_large", "message": "request body too large"}}, 413)
        t0 = time.perf_counter()
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("unhandled error")
            resp = JSONResponse({"error": {"code": "internal_error", "message": "internal error", "request_id": rid}}, 500)
        group = request.url.path.split("/")[2] if request.url.path.startswith("/v1/") and request.url.path.count("/") > 1 else "other"
        m.API_LATENCY.labels(group[:20], f"{resp.status_code // 100}xx").observe(time.perf_counter() - t0)
        resp.headers["X-Request-Id"] = rid
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/dashboard"):
            resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.exception_handler(ApiError)
    async def api_error(request: Request, e: ApiError):
        return JSONResponse({"error": {"code": e.code, "message": e.message, "details": e.details,
                                       "request_id": getattr(request.state, "request_id", "-")}}, e.status)

    @app.exception_handler(PyMongoError)
    async def db_error(request: Request, e: PyMongoError):
        log.error("database error: %s", type(e).__name__)
        return JSONResponse({"error": {"code": "database_unavailable", "message": "temporarily unavailable",
                                       "request_id": getattr(request.state, "request_id", "-")}}, 503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, e: RequestValidationError):
        errs = [{"loc": [str(x) for x in er["loc"]], "msg": er["msg"]} for er in e.errors()]
        return JSONResponse({"error": {"code": "validation_error", "message": "invalid request", "details": {"errors": errs},
                                       "request_id": getattr(request.state, "request_id", "-")}}, 422)

    app.include_router(routes.health)
    app.include_router(routes.v1)
    app.include_router(routes_platform.router)
    app.include_router(routes_live.router)
    app.include_router(routes_platform.ws_router)
    if s.dashboard_enabled:
        from pathlib import Path
        from fastapi.staticfiles import StaticFiles
        app.mount("/dashboard", StaticFiles(directory=str(Path(__file__).parent / "static" / "dashboard"), html=True), name="dashboard")
    return app


async def _housekeeping(s: Settings, repos: Repos, storage, wworker) -> None:
    """Periodic: recording upload/verification, retention of recordings, stuck webhook claims."""
    from app.core.ids import utcnow
    from datetime import timedelta

    while True:
        try:
            await asyncio.sleep(min(s.retention_interval_s, 30))
            await rec_svc.finalize_recordings(repos, storage, s)
            await rec_svc.purge_expired(repos, storage)
            await repos.db["webhook_deliveries"].update_many(
                {"status": "sending", "claimed_at": {"$lt": utcnow() - timedelta(minutes=2)}}, {"$set": {"status": "retrying"}})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("housekeeping error")
