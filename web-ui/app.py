"""Web UI — real-time colony dashboard.

Serves a live dashboard at the Traefik root path (/).
Receives cell activity events from cell-runtime and broadcasts
them to connected browsers via WebSocket.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse

LOG_LEVEL = os.getenv("KERNEL_LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [web-ui] %(levelname)s %(message)s")
log = logging.getLogger("web-ui")

LIFE_API_SECRET = os.getenv("LIFE_API_SECRET", "")
KERNEL_URL = os.getenv("KERNEL_URL", "http://kernel:8001")
CELL_RUNTIME_URL = os.getenv("CELL_RUNTIME_URL", "http://cell-runtime:8004")
GUARDIAN_URL = os.getenv("GUARDIAN_URL", "http://guardians:8002")
TRAEFIK_DYNAMIC_DIR = os.getenv("TRAEFIK_DYNAMIC_DIR", "")

STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# WebSocket hub
# ---------------------------------------------------------------------------

_clients: set[WebSocket] = set()
_event_buffer: list[dict] = []
MAX_BUFFER = 300


async def _broadcast(event: dict) -> None:
    """Send event to all connected WebSocket clients."""
    _event_buffer.append(event)
    if len(_event_buffer) > MAX_BUFFER:
        del _event_buffer[: len(_event_buffer) - MAX_BUFFER]
    dead: set[WebSocket] = set()
    msg = json.dumps(event)
    for ws in _clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


# ---------------------------------------------------------------------------
# Traefik route registration
# ---------------------------------------------------------------------------

_ROUTE_CONFIG = """\
http:
  routers:
    web-ui:
      rule: "PathPrefix(`/`)"
      entryPoints:
        - web
      service: web-ui
      priority: 1
  services:
    web-ui:
      loadBalancer:
        servers:
          - url: "http://web-ui:8006"
"""


def _register_traefik_route() -> None:
    """Write our Traefik dynamic config so / routes here."""
    if not TRAEFIK_DYNAMIC_DIR:
        log.warning("TRAEFIK_DYNAMIC_DIR not set — skipping route registration")
        return
    try:
        d = Path(TRAEFIK_DYNAMIC_DIR)
        d.mkdir(parents=True, exist_ok=True)
        (d / "web-ui.yml").write_text(_ROUTE_CONFIG, encoding="utf-8")
        log.info("Registered Traefik route: / -> web-ui:8006")
    except Exception as exc:
        log.warning("Failed to write Traefik route: %s", exc)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    _register_traefik_route()
    log.info("Web UI online")
    yield
    log.info("Web UI shutting down")


app = FastAPI(title="Life Web UI", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth middleware — /ingest requires secret, user-facing endpoints are open
# ---------------------------------------------------------------------------


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    path = request.url.path
    if path in ("/health", "/", "/ws", "/api/state", "/api/command"):
        return await call_next(request)
    if path == "/ingest":
        if LIFE_API_SECRET and request.headers.get("X-Life-Secret") != LIFE_API_SECRET:
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)


def _auth_headers() -> dict:
    return {"X-Life-Secret": LIFE_API_SECRET} if LIFE_API_SECRET else {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/ingest")
async def ingest_event(request: Request):
    """Receive cell activity events from cell-runtime."""
    body = await request.json()
    body["server_ts"] = time.time()
    await _broadcast(body)
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _clients.add(websocket)
    log.info("WebSocket client connected (%d total)", len(_clients))
    # Send buffered events to catch up
    for event in _event_buffer:
        try:
            await websocket.send_text(json.dumps(event))
        except Exception:
            break
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)
        log.info("WebSocket client disconnected (%d remaining)", len(_clients))


@app.get("/api/state")
async def get_colony_state():
    """Fetch full colony state from kernel, cell-runtime, and wiseoldant."""
    state: dict = {"cells": {}, "services": [], "kernel": {}}
    async with httpx.AsyncClient(timeout=5) as client:
        # Parallel fetch
        tasks = {
            "cells": client.get(f"{CELL_RUNTIME_URL}/cells", headers=_auth_headers()),
            "services": client.get(f"{GUARDIAN_URL}/wiseoldant/services", headers=_auth_headers()),
            "kernel": client.get(f"{KERNEL_URL}/cells", headers=_auth_headers()),
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                log.warning("Failed to fetch %s: %s", key, result)
                continue
            if result.status_code == 200:
                state[key] = result.json()
    return state


@app.get("/")
async def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/api/command")
async def send_command(request: Request):
    """Post a Admin command to the colony bulletin board."""
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse(status_code=400, content={"detail": "Empty message"})
    line = f"[ADMIN] {message}\n"
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(
            f"{GUARDIAN_URL}/queen/append",
            json={"path": "/data/workspace/.colony_board", "content": line},
            headers=_auth_headers(),
        )
        if resp.status_code != 200:
            return JSONResponse(status_code=502, content={"detail": "Failed to write to bulletin board"})
    await _broadcast({
        "type": "command",
        "cell_id": "ADMIN",
        "thought": message,
        "cycle": "-",
        "health": "-",
        "server_ts": time.time(),
    })
    log.info("Admin command posted: %s", message[:100])
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "web-ui", "clients": len(_clients)}
