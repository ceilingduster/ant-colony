"""Colony Monitor — observes and governs the Life experiment.

Tracks cell health, enforces population ceilings, detects stalled cells,
triggers repairs, and exposes status for the Admin.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

LOG_LEVEL = os.getenv("KERNEL_LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [monitor] %(levelname)s %(message)s")
log = logging.getLogger("monitor")

LIFE_API_SECRET = os.getenv("LIFE_API_SECRET", "")

KERNEL_URL = os.getenv("KERNEL_URL", "http://kernel:8001")
CELL_RUNTIME_URL = os.getenv("CELL_RUNTIME_URL", "http://cell-runtime:8004")
CHECK_INTERVAL = int(os.getenv("MONITOR_CHECK_INTERVAL", "15"))  # seconds
HEALTH_THRESHOLD = int(os.getenv("MONITOR_HEALTH_THRESHOLD", "20"))
REPAIR_THRESHOLD = int(os.getenv("MONITOR_REPAIR_THRESHOLD", "40"))
STALL_SECONDS = int(os.getenv("MONITOR_STALL_SECONDS", "120"))  # no cycle progress → stalled
MAX_POPULATION = int(os.getenv("MAX_POPULATION", "10"))
DEAD_CLEANUP_SECONDS = int(os.getenv("DEAD_CLEANUP_SECONDS", "300"))  # remove dead cells after 5 min
LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))

# ---------------------------------------------------------------------------
# Metrics & tracking
# ---------------------------------------------------------------------------


class Metrics(BaseModel):
    checks: int = 0
    repairs_triggered: int = 0
    cells_terminated: int = 0
    stalls_detected: int = 0
    last_check: float = 0
    events: list[dict] = Field(default_factory=list)


metrics = Metrics()

# Track last-known cycle per cell to detect stalls
_last_cycle: dict[str, int] = {}
_last_cycle_ts: dict[str, float] = {}
# Track when cells died for cleanup scheduling
_dead_since: dict[str, float] = {}


def _log_event(event_type: str, detail: dict) -> None:
    entry = {"ts": time.time(), "type": event_type, "detail": detail}
    metrics.events.append(entry)
    if len(metrics.events) > 500:
        metrics.events = metrics.events[-500:]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "monitor.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------


async def _monitor_loop() -> None:
    """Periodic check on colony health."""
    log.info("Monitor loop started (interval=%ds)", CHECK_INTERVAL)
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        metrics.checks += 1
        metrics.last_check = time.time()

        try:
            async with httpx.AsyncClient(timeout=10, headers=_auth_headers()) as client:
                # Get cell list from kernel
                kernel_resp = await client.get(f"{KERNEL_URL}/cells")
                if kernel_resp.status_code != 200:
                    log.warning("Could not fetch cells from kernel: %d", kernel_resp.status_code)
                    continue
                kernel_cells = kernel_resp.json()

                # Get runtime cell status for stall detection
                runtime_cells = {}
                try:
                    rt_resp = await client.get(f"{CELL_RUNTIME_URL}/cells")
                    if rt_resp.status_code == 200:
                        runtime_cells = rt_resp.json()
                except Exception:
                    pass

                alive_count = 0
                for cell_id, cell_data in kernel_cells.items():
                    status = cell_data.get("status", "dead")
                    health = cell_data.get("health", 0)

                    if status == "dead":
                        continue
                    alive_count += 1

                    # ---- Stall detection ----
                    rt_cell = runtime_cells.get(cell_id, {})
                    current_cycle = rt_cell.get("cycle", 0)
                    now = time.time()

                    if cell_id in _last_cycle:
                        if current_cycle == _last_cycle[cell_id]:
                            stall_duration = now - _last_cycle_ts[cell_id]
                            if stall_duration > STALL_SECONDS:
                                log.warning("Cell %s stalled (cycle=%d for %.0fs)",
                                            cell_id, current_cycle, stall_duration)
                                metrics.stalls_detected += 1
                                _log_event("stall_detected", {
                                    "cell_id": cell_id,
                                    "cycle": current_cycle,
                                    "stall_seconds": round(stall_duration),
                                })
                                # Try to restart it
                                await _attempt_repair(client, cell_id, "stall")
                                continue
                        else:
                            _last_cycle_ts[cell_id] = now
                    else:
                        _last_cycle_ts[cell_id] = now
                    _last_cycle[cell_id] = current_cycle

                    # ---- Health checks ----
                    if health <= HEALTH_THRESHOLD:
                        # Critical — terminate
                        log.warning("Cell %s health critical (%d) — terminating", cell_id, health)
                        await client.post(
                            f"{KERNEL_URL}/cells/{cell_id}/kill",
                            params={"reason": "health_critical"},
                        )
                        await client.post(f"{CELL_RUNTIME_URL}/cells/{cell_id}/stop")
                        metrics.cells_terminated += 1
                        _log_event("cell_terminated", {"cell_id": cell_id, "health": health})
                    elif health <= REPAIR_THRESHOLD:
                        # Moderate — attempt repair before killing
                        log.info("Cell %s health low (%d) — attempting repair", cell_id, health)
                        await _attempt_repair(client, cell_id, "low_health")

                log.info("Monitor check #%d: %d alive cells", metrics.checks, alive_count)
                _log_event("check", {"alive": alive_count})

                # ---- Dead cell cleanup ----
                now = time.time()
                for cell_id, cell_data in kernel_cells.items():
                    if cell_data.get("status") == "dead":
                        if cell_id not in _dead_since:
                            _dead_since[cell_id] = now
                        elif now - _dead_since[cell_id] > DEAD_CLEANUP_SECONDS:
                            try:
                                await client.post(
                                    f"{KERNEL_URL}/cells/{cell_id}/cleanup",
                                    timeout=5,
                                )
                                _log_event("dead_cell_cleaned", {"cell_id": cell_id})
                                _dead_since.pop(cell_id, None)
                                _last_cycle.pop(cell_id, None)
                                _last_cycle_ts.pop(cell_id, None)
                            except Exception as exc:
                                log.warning("Cleanup failed for %s: %s", cell_id, exc)
                    else:
                        _dead_since.pop(cell_id, None)

                # ---- Population ceiling enforcement ----
                if alive_count > MAX_POPULATION:
                    # Kill the weakest cells until at or below limit
                    alive_cells = [
                        (cid, cd) for cid, cd in kernel_cells.items()
                        if cd.get("status") not in ("dead", None)
                    ]
                    alive_cells.sort(key=lambda x: x[1].get("health", 0))
                    excess = alive_count - MAX_POPULATION
                    for cid, cd in alive_cells[:excess]:
                        log.warning("Population over limit — culling cell %s (health=%d)",
                                    cid, cd.get("health", 0))
                        await client.post(
                            f"{KERNEL_URL}/cells/{cid}/kill",
                            params={"reason": "population_ceiling"},
                        )
                        await client.post(f"{CELL_RUNTIME_URL}/cells/{cid}/stop")
                        metrics.cells_terminated += 1
                        _log_event("cell_culled", {"cell_id": cid, "reason": "population_ceiling"})

        except Exception as exc:
            log.error("Monitor check failed: %s", exc)


async def _attempt_repair(client: httpx.AsyncClient, cell_id: str, reason: str) -> None:
    """Try to repair a cell by stopping and restarting it on the runtime."""
    metrics.repairs_triggered += 1
    _log_event("repair_attempt", {"cell_id": cell_id, "reason": reason})

    try:
        # Stop current execution
        await client.post(f"{CELL_RUNTIME_URL}/cells/{cell_id}/stop", timeout=5)
        await asyncio.sleep(2)

        # Fetch cell traits from kernel to restart with
        cell_resp = await client.get(f"{KERNEL_URL}/cells/{cell_id}", timeout=5)
        if cell_resp.status_code == 200:
            cell_data = cell_resp.json()
            traits = cell_data.get("traits", {})
            # Restart on runtime
            await client.post(
                f"{CELL_RUNTIME_URL}/cells/start",
                json={"cell_id": cell_id, "traits": traits},
                timeout=10,
            )
            # Boost health slightly
            await client.post(
                f"{KERNEL_URL}/cells/{cell_id}/health",
                json={"delta": 15},
                timeout=5,
            )
            log.info("Repair succeeded for cell %s", cell_id)
            _log_event("repair_success", {"cell_id": cell_id})
        else:
            log.warning("Could not fetch cell %s for repair", cell_id)
    except Exception as exc:
        log.error("Repair failed for cell %s: %s", cell_id, exc)
        _log_event("repair_failed", {"cell_id": cell_id, "error": str(exc)})


_monitor_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _monitor_task
    _monitor_task = asyncio.create_task(_monitor_loop())
    log.info("Colony monitor online")
    yield
    if _monitor_task:
        _monitor_task.cancel()
    log.info("Colony monitor shutting down")


app = FastAPI(title="Life Colony Monitor", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if LIFE_API_SECRET and request.headers.get("X-Life-Secret") != LIFE_API_SECRET:
        return JSONResponse(status_code=403, content={"detail": "Invalid or missing API secret"})
    return await call_next(request)


def _auth_headers() -> dict:
    return {"X-Life-Secret": LIFE_API_SECRET} if LIFE_API_SECRET else {}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "monitor"}


@app.get("/status")
async def status():
    """Full colony status for the Admin."""
    colony = {}
    runtime = {}
    try:
        async with httpx.AsyncClient(timeout=10, headers=_auth_headers()) as client:
            resp = await client.get(f"{KERNEL_URL}/state")
            if resp.status_code == 200:
                colony = resp.json()
            rt_resp = await client.get(f"{CELL_RUNTIME_URL}/cells")
            if rt_resp.status_code == 200:
                runtime = rt_resp.json()
    except Exception as exc:
        colony = {"error": str(exc)}

    return {
        "monitor": metrics.model_dump(),
        "colony": colony,
        "runtime": runtime,
    }


@app.get("/metrics")
async def get_metrics():
    return metrics.model_dump()


@app.get("/events")
async def get_events(limit: int = 50):
    return metrics.events[-limit:]
