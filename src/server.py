"""DinkIQ — FastAPI server: app wiring, auth, SPA pages and health.

The API routes live in api/ (uploads, sessions, trash, insights, exports);
housekeeping in api/maintenance.py. This module owns what is app-wide: the
auth middleware, startup recovery, error handling, and serving the frontend.
"""

import asyncio
import base64
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

import jobs
import logsetup
import pipeline
from api import exports, insights, maintenance, sessions, trash, uploads
from api.common import MIN_FREE_BYTES, dir_bytes, free_bytes
from jsonio import sanitize
from pipeline import SESSIONS
from tracking import MODEL_NAME as POSE_MODEL

ROOT = Path(__file__).resolve().parent.parent


STATIC = ROOT / "static"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE per line, '#' comments) so secrets like
    DINKIQ_PASSWORD survive a server restart without depending on whatever
    shell/launcher started the process. Doesn't override an already-set
    real env var. No new dependency -- this is the only var that needs it."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv(ROOT / ".env")


class SafeJSONResponse(JSONResponse):
    """Starlette's default refuses NaN/Inf (-> a 500 for the whole endpoint) and
    other JSON writers emit bare `NaN`, which iOS Safari hard-fails on. Map them
    to null once, here, for every response."""

    def render(self, content) -> bytes:
        return json.dumps(sanitize(content), ensure_ascii=False, allow_nan=False,
                          indent=None, separators=(",", ":")).encode("utf-8")


log = logging.getLogger("dinkiq")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # The job queue is in memory only: repair whatever a restart/crash stranded
    # before serving. Must never stop the server from coming up.
    try:
        recovered = jobs.recover_interrupted()
        if any(recovered.values()):
            log.warning("startup recovery: %s", recovered)
    except Exception:
        log.exception("startup recovery failed (continuing)")
    try:
        maintenance.maintenance_once()
    except Exception:
        log.exception("startup maintenance failed (continuing)")
    task = asyncio.create_task(maintenance.maintenance_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="DinkIQ", default_response_class=SafeJSONResponse, lifespan=_lifespan)

app.include_router(uploads.router)
app.include_router(sessions.router)
app.include_router(trash.router)
app.include_router(insights.router)
app.include_router(exports.router)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    # FastAPI's built-in handler uses the plain JSONResponse, and the error body
    # echoes the offending `input` -- so a NaN in a request turned a clean 422
    # into a 500. Same body shape, routed through the NaN-safe response.
    return SafeJSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


# ── Optional HTTP Basic Auth for remote access (e.g. via Tailscale Funnel) ──
# Set DINKIQ_PASSWORD=somepassword (in .env, or the environment directly).
#
# A request skips the password only if it is
#   (a) from the reverse proxy -- carries DINKIQ_PROXY_SECRET in X-Dinkiq-Proxy
#       (Caddy overwrites that header on every proxied request, after its own
#       basic_auth has passed), or
#   (b) genuinely local -- loopback peer, no forwarding headers, loopback Host.
# The Host header alone is NOT trusted: any client picks its own, and the
# server used to listen on every interface, so `Host: 127.0.0.1` from anywhere
# on the LAN/tailnet got full access. (b) still checks Host, but only to keep
# DNS-rebinding pages (their own name resolving to 127.0.0.1) out.
DEFAULT_HOST = "127.0.0.1"  # Caddy proxies to 127.0.0.1:8100; nothing else needs the port


HOST = os.environ.get("DINKIQ_HOST", "").strip() or DEFAULT_HOST


_DINKIQ_PASSWORD = os.environ.get("DINKIQ_PASSWORD", "").strip() or None


_DINKIQ_PASSWORD_BYTES = _DINKIQ_PASSWORD.encode() if _DINKIQ_PASSWORD else None


_PROXY_SECRET = os.environ.get("DINKIQ_PROXY_SECRET", "").strip() or None


_PROXY_SECRET_BYTES = _PROXY_SECRET.encode() if _PROXY_SECRET else None


_LOCAL_PEERS = ("127.0.0.1", "::1")


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


_FORWARDING_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                       "x-real-ip", "forwarded")


def _host_without_port(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):  # [::1]:8100
        return host[:host.find("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def _from_trusted_proxy(request: Request) -> bool:
    if _PROXY_SECRET_BYTES is None:
        return False
    provided = request.headers.get("x-dinkiq-proxy", "").encode()
    return secrets.compare_digest(provided, _PROXY_SECRET_BYTES)


def _is_local_request(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    if peer not in _LOCAL_PEERS:
        return False
    if any(h in request.headers for h in _FORWARDING_HEADERS):
        return False
    return _host_without_port(request.headers.get("host", "")) in _LOCAL_HOSTS


@app.middleware("http")
async def _basic_auth(request, call_next):
    if _DINKIQ_PASSWORD_BYTES is None:
        return await call_next(request)
    if _from_trusted_proxy(request) or _is_local_request(request):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).split(b":", 1)
            provided = decoded[1] if len(decoded) == 2 else b""
            if secrets.compare_digest(provided, _DINKIQ_PASSWORD_BYTES):
                return await call_next(request)
        except Exception:
            pass
    return PlainTextResponse("Unauthorized", status_code=401,
                             headers={"WWW-Authenticate": 'Basic realm="DinkIQ"'})


def _mount_prefix(request: Request) -> str:
    """Path prefix this app is mounted under, from the reverse proxy.

    Served directly (localhost, LAN) this is "" and every URL stays
    root-absolute as before. Behind the vantage Caddy hub the app lives at
    /app/dinkiq/ with the prefix stripped before it reaches us, so the
    server never sees it in request.url — without echoing the proxy's
    X-Forwarded-Prefix back to the page, the frontend's root-absolute
    fetch('/api/...') calls resolve against the hub origin instead of this
    app and every one of them 404s.
    """
    return (request.headers.get("x-forwarded-prefix") or "").rstrip("/")


def _spa(path: Path, request: Request) -> HTMLResponse:
    """Serve an SPA page with window.__BASE__ set to the mount prefix."""
    html = path.read_text()
    prefix = _mount_prefix(request)
    if prefix:
        tag = f"<script>window.__BASE__={json.dumps(prefix)};</script>"
        html = html.replace("<head>", "<head>" + tag, 1)
    return HTMLResponse(html)


@app.get("/")
def index(request: Request):
    return _spa(STATIC / "dashboard.html", request)


@app.get("/backyard")
def backyard(request: Request):
    # self-contained page, no API calls of its own — no prefix needed, and it
    # rebuilds <head> at runtime anyway (see its own mobile-fix comment)
    return FileResponse(STATIC / "backyard.html")


STARTED_AT = time.time()


def _weights_present() -> bool:
    return (pipeline.MODELS / POSE_MODEL).exists()


def _data_dir_writable() -> bool:
    try:
        SESSIONS.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=SESSIONS.parent):
            return True
    except OSError:
        return False


def _tool_check(name: str) -> dict:
    found = shutil.which(name)
    return {"ok": bool(found),
            "detail": found or f"{name} not found on PATH — analysis would fail (launchd's PATH excludes Homebrew)"}


@app.get("/api/health")
def health():
    """Is this server able to do its job? 200 only when every check passes, so a
    watchdog can alert on the status code alone. The old failure mode was a server
    that answered requests perfectly while every analysis died on a missing tool."""
    free = free_bytes()
    worker = jobs.worker_status()
    writable = _data_dir_writable()
    weights = _weights_present()
    checks = {
        "ffmpeg": _tool_check("ffmpeg"),
        "ffprobe": _tool_check("ffprobe"),
        "disk": {"ok": free >= MIN_FREE_BYTES, "detail": f"{free / (1 << 30):.1f} GB free"},
        "data_dir": {"ok": writable, "detail": "writable" if writable else "not writable"},
        "weights": {"ok": weights, "detail": f"{POSE_MODEL} {'present' if weights else 'MISSING'}"},
        "worker": {"ok": worker["alive"] is not False,
                   "detail": "not started yet" if not worker["started"] else
                   ("running" if worker["alive"] else "DEAD — restart the service")},
    }
    ok = all(c["ok"] for c in checks.values())
    sessions_bytes = sum(dir_bytes(d) for d in SESSIONS.iterdir()
                         if d.is_dir() and not d.name.startswith(".")) if SESSIONS.exists() else 0
    return SafeJSONResponse(status_code=200 if ok else 503, content={
        "ok": ok, "checks": checks, "uptime_s": round(time.time() - STARTED_AT),
        "sessions_bytes": sessions_bytes,
        "queue": {"queued": worker["queued"], "running": worker["running"]}})


if __name__ == "__main__":
    if _DINKIQ_PASSWORD_BYTES and not _PROXY_SECRET_BYTES:
        print("warning: DINKIQ_PASSWORD is set but DINKIQ_PROXY_SECRET is not -- "
              "requests through the reverse proxy will be asked for the password again")
    logsetup.configure_logging()
    uvicorn.run(app, host=HOST, port=8100,
                log_config=logsetup.uvicorn_log_config() or uvicorn.config.LOGGING_CONFIG)
