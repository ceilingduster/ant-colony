"""Guardian API — unified FastAPI server exposing all guardian endpoints.

All operations are logged to /data/logs/guardian_audit.jsonl for the Admin.
"""

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from queen import read_file, write_file, append_file, delete_file, create_directory, list_directory
from antking import execute, validate_command
from wiseoldant import start_service, stop_service, get_logs, list_services
from nurse import http_get, http_post

LOG_LEVEL = os.getenv("KERNEL_LOG_LEVEL", "INFO")
LIFE_API_SECRET = os.getenv("LIFE_API_SECRET", "")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [guardians] %(levelname)s %(message)s")
log = logging.getLogger("guardians")

AUDIT_LOG = Path(os.getenv("LOG_DIR", "/data/logs")) / "guardian_audit.jsonl"
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "/data/workspace")
KERNEL_URL = os.getenv("KERNEL_URL", "http://kernel:8001")


def _audit(guardian: str, operation: str, params: dict, result: dict | None = None, error: str | None = None) -> None:
    """Append an audit entry for every guardian operation."""
    entry = {
        "ts": time.time(),
        "guardian": guardian,
        "operation": operation,
        "params": params,
        "error": error,
    }
    if result is not None:
        # Truncate large results to keep audit manageable
        entry["result_status"] = result.get("status", "unknown")
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Guardian layer online")
    yield
    log.info("Guardian layer shutting down")


app = FastAPI(title="Life Guardians", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if LIFE_API_SECRET and request.headers.get("X-Life-Secret") != LIFE_API_SECRET:
        return JSONResponse(status_code=403, content={"detail": "Invalid or missing API secret"})
    return await call_next(request)


# ---- Queen endpoints -----------------------------------------------------

class FileWriteReq(BaseModel):
    path: str
    content: str


class FileReadReq(BaseModel):
    path: str


class DirReq(BaseModel):
    path: str


@app.post("/queen/read")
async def api_read_file(req: FileReadReq):
    try:
        content = read_file(req.path)
        _audit("queen", "read", {"path": req.path}, {"status": "ok"})
        return {"status": "ok", "content": content}
    except (PermissionError, FileNotFoundError) as e:
        _audit("queen", "read", {"path": req.path}, error=str(e))
        raise HTTPException(status_code=403 if isinstance(e, PermissionError) else 404, detail=str(e))


@app.post("/queen/write")
async def api_write_file(req: FileWriteReq):
    try:
        result = write_file(req.path, req.content)
        _audit("queen", "write", {"path": req.path, "bytes": len(req.content)}, result)
        return result
    except PermissionError as e:
        _audit("queen", "write", {"path": req.path}, error=str(e))
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/queen/append")
async def api_append_file(req: FileWriteReq):
    try:
        result = append_file(req.path, req.content)
        _audit("queen", "append", {"path": req.path, "bytes": len(req.content)}, result)
        return result
    except PermissionError as e:
        _audit("queen", "append", {"path": req.path}, error=str(e))
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/queen/delete")
async def api_delete_file(req: FileReadReq):
    try:
        result = delete_file(req.path)
        _audit("queen", "delete", {"path": req.path}, result)
        return result
    except (PermissionError, FileNotFoundError) as e:
        _audit("queen", "delete", {"path": req.path}, error=str(e))
        raise HTTPException(status_code=403 if isinstance(e, PermissionError) else 404, detail=str(e))


@app.post("/queen/mkdir")
async def api_mkdir(req: DirReq):
    try:
        result = create_directory(req.path)
        _audit("queen", "mkdir", {"path": req.path}, result)
        return result
    except PermissionError as e:
        _audit("queen", "mkdir", {"path": req.path}, error=str(e))
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/queen/ls")
async def api_ls(req: DirReq):
    try:
        entries = list_directory(req.path)
        _audit("queen", "ls", {"path": req.path}, {"status": "ok"})
        return {"status": "ok", "entries": entries}
    except (PermissionError, FileNotFoundError) as e:
        _audit("queen", "ls", {"path": req.path}, error=str(e))
        raise HTTPException(status_code=403 if isinstance(e, PermissionError) else 404, detail=str(e))


# ---- Antking endpoints ---------------------------------------------------

class CmdReq(BaseModel):
    cmd: str
    cwd: str | None = None
    timeout: int | None = None


@app.post("/antking/exec")
async def api_exec(req: CmdReq):
    # Restrict working directory to workspace
    effective_cwd = req.cwd or WORKSPACE_DIR
    ws_root = Path(WORKSPACE_DIR).resolve()
    try:
        Path(effective_cwd).resolve().relative_to(ws_root)
    except ValueError:
        _audit("antking", "exec", {"cmd": req.cmd, "cwd": effective_cwd}, error="cwd outside workspace")
        raise HTTPException(status_code=403, detail=f"Working directory must be inside {WORKSPACE_DIR}")
    try:
        result = await execute(req.cmd, cwd=effective_cwd, timeout=req.timeout)
        _audit("antking", "exec", {"cmd": req.cmd, "cwd": effective_cwd}, result)
        return result
    except (PermissionError, ValueError) as e:
        _audit("antking", "exec", {"cmd": req.cmd, "cwd": effective_cwd}, error=str(e))
        raise HTTPException(status_code=403, detail=str(e))


# ---- Wiseoldant endpoints ------------------------------------------------

class ServiceReq(BaseModel):
    name: str
    cmd: str
    port: int | None = None   # port the service listens on, for external access via Traefik
    cwd: str | None = None


class ServiceNameReq(BaseModel):
    name: str


@app.post("/wiseoldant/start")
async def api_start_service(req: ServiceReq):
    try:
        result = await start_service(req.name, req.cmd, req.cwd, req.port)
        _audit("wiseoldant", "start", {"name": req.name, "cmd": req.cmd, "port": req.port}, result)
        return result
    except PermissionError as e:
        _audit("wiseoldant", "start", {"name": req.name, "cmd": req.cmd, "port": req.port}, error=str(e))
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        _audit("wiseoldant", "start", {"name": req.name}, error=str(e))
        raise HTTPException(status_code=429, detail=str(e))


@app.post("/wiseoldant/stop")
async def api_stop_service(req: ServiceNameReq):
    result = await stop_service(req.name)
    _audit("wiseoldant", "stop", {"name": req.name}, result)
    return result


@app.post("/wiseoldant/logs")
async def api_get_logs(req: ServiceNameReq, lines: int = 50):
    result = await get_logs(req.name, lines)
    _audit("wiseoldant", "logs", {"name": req.name, "lines": lines})
    return result


@app.get("/wiseoldant/services")
async def api_list_services():
    return list_services()


# ---- Nurse endpoints -----------------------------------------------------

class HttpReq(BaseModel):
    url: str
    headers: dict | None = None
    json_body: dict | None = None


@app.post("/nurse/get")
async def api_http_get(req: HttpReq):
    try:
        result = await http_get(req.url, req.headers)
        _audit("nurse", "get", {"url": req.url}, {"status": result.get("status_code")})
        return result
    except PermissionError as e:
        _audit("nurse", "get", {"url": req.url}, error=str(e))
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/nurse/post")
async def api_http_post(req: HttpReq):
    try:
        result = await http_post(req.url, req.json_body, req.headers)
        _audit("nurse", "post", {"url": req.url}, {"status": result.get("status_code")})
        return result
    except PermissionError as e:
        _audit("nurse", "post", {"url": req.url}, error=str(e))
        raise HTTPException(status_code=403, detail=str(e))


# ---- Cell-to-Kernel proxy endpoints ---------------------------------------
# Cells route kernel calls through guardians; guardians audit and forward.


class ReplicateReq(BaseModel):
    parent_id: str
    traits: dict = {}


class HealthDeltaReq(BaseModel):
    cell_id: str
    delta: int


@app.post("/cell/replicate")
async def api_cell_replicate(req: ReplicateReq):
    _audit("cell-proxy", "replicate", {"parent_id": req.parent_id})
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{KERNEL_URL}/cells/replicate",
                json={"parent_id": req.parent_id, "traits": req.traits},
                headers={"X-Life-Secret": LIFE_API_SECRET} if LIFE_API_SECRET else {},
            )
        result = resp.json()
        _audit("cell-proxy", "replicate_result", {"parent_id": req.parent_id}, {"status": resp.status_code})
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=result.get("detail", "Replication failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        _audit("cell-proxy", "replicate", {"parent_id": req.parent_id}, error=str(e))
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/cell/health")
async def api_cell_health(req: HealthDeltaReq):
    _audit("cell-proxy", "health", {"cell_id": req.cell_id, "delta": req.delta})
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{KERNEL_URL}/cells/{req.cell_id}/health",
                json={"delta": req.delta},
                headers={"X-Life-Secret": LIFE_API_SECRET} if LIFE_API_SECRET else {},
            )
        return resp.json()
    except Exception as e:
        _audit("cell-proxy", "health", {"cell_id": req.cell_id}, error=str(e))
        raise HTTPException(status_code=502, detail=str(e))


# ---- Health ---------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "guardians"}
