import contextvars
import logging
import logging.config
import os
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings

# --- observability -----------------------------------------------------------

#: The identifier of the current request — in every log line written while it is
#: being handled. A contextvar rather than a global variable: requests are
#: handled interleaved, and without a context the lines of neighbouring requests
#: would sign each other.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging() -> None:
    """The application's log: the level from LOG_LEVEL, the request id in every
    line.

    dictConfig rather than basicConfig: uvicorn configures logging itself, and
    basicConfig after it silently does nothing. disable_existing_loggers=False —
    uvicorn's loggers keep living, we merely add our own.
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": _RequestIdFilter}},
            "formatters": {
                "app": {
                    "format": "%(levelname)s %(name)s [%(request_id)s] %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "app",
                    "filters": ["request_id"],
                }
            },
            "loggers": {
                "app": {
                    "level": get_settings().log_level.upper(),
                    "handlers": ["console"],
                    # Do not pass records further up: uvicorn has a handler of
                    # its own, and without this every line would be printed twice.
                    "propagate": False,
                }
            },
        }
    )


configure_logging()
logger = logging.getLogger("app.main")
from app.api import (
    admin_routes,
    ai_routes,
    auth_routes,
    export_routes,
    invite_routes,
    live_routes,
    meta_routes,
    org_routes,
    project_routes,
    proposal_routes,
    public_routes,
    scorecard_routes,
    share_routes,
)

def refuse_a_multi_worker_start() -> None:
    """The live feed lives in the process's memory — there must be exactly one worker.

    A revision applied in worker A would not reach sockets opened in worker B:
    each process has its own hub rooms. This limitation is described in the
    README, but a description does not prevent anyone from passing --workers 4 —
    whereas this refusal does. The variables uvicorn and gunicorn take the worker
    count from are checked; once there really is a second replica, a shared bus
    (LISTEN/NOTIFY) will appear right here — see the remediation plan.
    """
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        value = os.getenv(name, "")
        if value.isdigit() and int(value) > 1:
            raise RuntimeError(
                f"{name}={value}: рассылка живой ленты живёт в памяти одного "
                "процесса, несколько воркеров молча потеряют события. "
                "Убери переменную или оставь одного воркера."
            )


def warn_about_a_default_public_base_url() -> None:
    """A PUBLIC_BASE_URL left at its default means localhost links in emails and
    public addresses. Not a refusal (local development is lawful), but not
    silence either: a production installation must see this in the log."""
    if get_settings().public_base_url == "http://localhost:8000":
        logger.warning(
            "PUBLIC_BASE_URL не задан: ссылки в письмах и публичные адреса "
            "будут указывать на localhost. Для боевой установки задай его в .env."
        )


@asynccontextmanager
async def _lifespan(_: FastAPI):
    refuse_a_multi_worker_start()
    warn_about_a_default_public_base_url()
    yield


# The documentation lives under /api: in a production layout (Caddy, Vercel)
# everything outside /api/* is intercepted by the fallback to index.html, and
# the standard /docs and /openapi.json were unreachable precisely where they are
# needed most.
app = FastAPI(
    title="Planora",
    lifespan=_lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)


@app.middleware("http")
async def stamp_request_id(request: Request, call_next):
    """The request id: accepted from the proxy or issued here.

    The answer carries it in X-Request-ID and the log carries it in every line:
    "send us the id from the response" is the only way to find in the log exactly
    the request a person is talking about.
    """
    incoming = request.headers.get("x-request-id", "")
    # A foreign value is truncated and cleaned: a header is user input, and it
    # must be able neither to write newlines into the log nor to break the answer
    # with a character outside latin-1. ASCII alphanumerics, hyphen and
    # underscore only.
    request_id = (
        "".join(ch for ch in incoming if ch.isascii() and (ch.isalnum() or ch in "-_"))[:64]
        or uuid.uuid4().hex
    )
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def reject_oversized_bodies(request: Request, call_next):
    """A ceiling on the body's size — before the JSON is parsed.

    Pydantic bounds the lengths of fields, but first the whole JSON has to arrive
    and be parsed — a gigabyte-sized body would eat memory and time before the
    first check. Content-Length is what is looked at: a client without it
    (chunked) is rare, and its body will hit the field ceilings anyway — the
    header closes the cheap path without claiming to be airtight.
    """
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > get_settings().max_body_bytes:
        return JSONResponse(status_code=413, content={"detail": "body_too_large"})
    return await call_next(request)


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_host(value: str) -> str | None:
    host = urlsplit(value).hostname
    return host.lower() if host else None


@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):
    """CSRF in depth: a writing request from another site is rejected by Origin.

    The first line is SameSite=Lax on the cookie, but it does not protect
    everyone: old browsers, embedding in a webview and future edits to the
    cookie's attributes must not leave a write without a second line. The browser
    sets Origin on every cross-site request with a body, and it cannot be forged
    from a page.

    The host is compared, not the whole string: the same site behind a proxy is
    seen by the application under an internal name, and comparing the scheme and
    port would produce false refusals. The expected hosts are our own Host,
    X-Forwarded-Host from the proxy and the host of PUBLIC_BASE_URL. A request
    with neither Origin nor Referer passes: it is not a browser (curl, tests,
    health), and CSRF does not threaten it — without a browser the cookie is not
    attached by itself.
    """
    if request.method in _WRITE_METHODS and request.url.path.startswith("/api/"):
        stated = request.headers.get("origin") or request.headers.get("referer")
        if stated:
            source = _origin_host(stated)
            allowed = {
                _origin_host(f"//{request.headers.get('host', '')}"),
                _origin_host(f"//{request.headers.get('x-forwarded-host', '')}"),
                _origin_host(get_settings().public_base_url),
            }
            allowed.discard(None)
            if source not in allowed:
                return JSONResponse(status_code=403, content={"detail": "csrf_origin_mismatch"})
    return await call_next(request)
app.include_router(meta_routes.router)
app.include_router(auth_routes.router)
app.include_router(org_routes.router)
app.include_router(invite_routes.router)
app.include_router(invite_routes.public_router)
app.include_router(project_routes.router)
app.include_router(proposal_routes.router)
app.include_router(scorecard_routes.router)
app.include_router(export_routes.router)
app.include_router(share_routes.router)
app.include_router(public_routes.router)
app.include_router(live_routes.router)
app.include_router(ai_routes.router)
app.include_router(admin_routes.router)


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness: the process is alive and answering. It deliberately does not
    touch the database — a database that is down is no reason to restart the
    process, and that is exactly what an orchestrator does to whoever fails
    liveness."""
    return {"status": "ok"}


@app.get("/api/health/ready")
def readiness():
    """Readiness: whether the process is genuinely ready to serve requests, that
    is, whether it can reach the database. Separate from liveness: this one
    answers "no" — and the load balancer moves traffic away without killing the
    process."""
    from sqlalchemy import text

    from app.db import engine

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness: база недоступна")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}


# Defined last — which means it wraps around the other middleware and fires
# first: CSRF and the body limit see an already rewritten path.
@app.middleware("http")
async def accept_the_v1_prefix(request: Request, call_next):
    """A version alias: /api/v1/* is served as /api/*.

    The version appears in the address before a second version exists: clients
    that build on /api/v1 will survive the arrival of /api/v2 without edits,
    while today's /api/* remains an alias of the first version. Only the HTTP
    request's path is rewritten; the WebSocket lives at
    /api/projects/{id}/live with no alias.
    """
    path = request.scope["path"]
    if path.startswith("/api/v1/"):
        request.scope["path"] = "/api/" + path[len("/api/v1/") :]
    return await call_next(request)
