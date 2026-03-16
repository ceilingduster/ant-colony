"""Life Kernel — central orchestrator for the Life experiment."""

import asyncio
import copy
import json
import logging
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("KERNEL_LOG_LEVEL", "INFO")
LIFE_API_SECRET = os.getenv("LIFE_API_SECRET", "")
MAX_POPULATION = int(os.getenv("MAX_POPULATION", "10"))
STATE_DIR = Path(os.getenv("STATE_DIR", "/data/state"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))
DNA_PATH = os.getenv("DNA_PATH", "/data/dna/SKILLS.md")

CELL_RUNTIME_URL = os.getenv("CELL_RUNTIME_URL", "http://cell-runtime:8004")

# Mutation bounds for replication
MUTABLE_TRAITS = {
    "creativity": (0.0, 1.0),
    "exploration": (0.0, 1.0),
    "patience": (0.0, 1.0),
    "replication_threshold": (0.0, 1.0),
    "repair_persistence": (0.0, 1.0),
}
MUTATION_MAGNITUDE = float(os.getenv("MUTATION_MAGNITUDE", "0.1"))

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [kernel] %(levelname)s %(message)s")
log = logging.getLogger("kernel")

# ---------------------------------------------------------------------------
# In-memory state (persisted to disk)
# ---------------------------------------------------------------------------


class CellRecord(BaseModel):
    cell_id: str
    parent_id: str | None = None
    status: str = "initializing"  # initializing | alive | repairing | dead
    health: int = 100
    created_at: float = Field(default_factory=time.time)
    traits: dict = Field(default_factory=dict)


class ExperimentState(BaseModel):
    experiment_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    cells: dict[str, CellRecord] = Field(default_factory=dict)
    generation: int = 0
    events: list[dict] = Field(default_factory=list)


state = ExperimentState()

# Spawn lock — prevents race conditions when multiple replication requests arrive
_spawn_lock = asyncio.Lock()
# Replication rate limiting — max 1 replication per cell per 60 seconds
_cell_last_replication: dict[str, float] = {}
REPLICATION_COOLDOWN = 30  # seconds


def _state_path() -> Path:
    return STATE_DIR / "experiment.json"


def save_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(state.model_dump_json(indent=2), encoding="utf-8")


def load_state() -> None:
    global state
    p = _state_path()
    if p.exists():
        state = ExperimentState.model_validate_json(p.read_text(encoding="utf-8"))
        log.info("Loaded experiment state: %s (%d cells)", state.experiment_id, len(state.cells))
    else:
        log.info("No prior state found — starting fresh experiment %s", state.experiment_id)
        save_state()


# ---------------------------------------------------------------------------
# Event logging
# ---------------------------------------------------------------------------


def record_event(event_type: str, detail: dict | None = None) -> None:
    entry = {
        "ts": time.time(),
        "type": event_type,
        "detail": detail or {},
    }
    state.events.append(entry)
    _append_log(entry)
    save_state()


def _append_log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# DNA validation
# ---------------------------------------------------------------------------


def validate_dna() -> dict:
    """Load and validate the DNA file. Returns parsed summary."""
    from dna_parser import load_dna, validate_dna as _validate

    dna = load_dna(DNA_PATH)
    warnings = _validate(dna)
    if warnings:
        for w in warnings:
            log.warning("DNA validation: %s", w)
    else:
        log.info("DNA validated successfully (%d skills, %d guardians)", len(dna.skills), len(dna.guardians))
    record_event("dna_validated", {"warnings": warnings, "skills": len(dna.skills), "guardians": len(dna.guardians)})
    return {"skills": len(dna.skills), "guardians": len(dna.guardians), "warnings": warnings}


# ---------------------------------------------------------------------------
# Cell management
# ---------------------------------------------------------------------------

DEFAULT_TRAITS: dict = {
    "creativity": 0.7,
    "exploration": 0.6,
    "patience": 0.6,
    "replication_threshold": 0.4,
    "repair_persistence": 0.5,
}


def create_cell(parent_id: str | None = None, traits: dict | None = None) -> CellRecord:
    alive = [c for c in state.cells.values() if c.status in ("initializing", "alive", "repairing")]
    if len(alive) >= MAX_POPULATION:
        raise ValueError(f"Population limit reached ({MAX_POPULATION})")

    cell_id = f"cell-{uuid.uuid4().hex[:8]}"
    effective_traits = traits or copy.deepcopy(DEFAULT_TRAITS)
    cell = CellRecord(cell_id=cell_id, parent_id=parent_id, traits=effective_traits)
    state.cells[cell_id] = cell
    state.generation += 1
    record_event("cell_created", {
        "cell_id": cell_id,
        "parent_id": parent_id,
        "generation": state.generation,
        "traits": effective_traits,
    })
    log.info("Created cell %s (parent=%s, gen=%d)", cell_id, parent_id, state.generation)
    return cell


def mutate_traits(parent_traits: dict) -> dict:
    """Apply small random mutations to parent traits. Never mutates safety params."""
    child_traits = copy.deepcopy(parent_traits)
    for trait, (lo, hi) in MUTABLE_TRAITS.items():
        if trait in child_traits:
            delta = random.uniform(-MUTATION_MAGNITUDE, MUTATION_MAGNITUDE)
            child_traits[trait] = round(max(lo, min(hi, child_traits[trait] + delta)), 4)
    return child_traits


def kill_cell(cell_id: str, reason: str = "apoptosis") -> None:
    cell = state.cells.get(cell_id)
    if not cell:
        raise KeyError(f"Unknown cell: {cell_id}")
    cell.status = "dead"
    record_event("cell_terminated", {"cell_id": cell_id, "reason": reason})
    log.info("Terminated cell %s: %s", cell_id, reason)
    save_state()


def update_health(cell_id: str, delta: int) -> int:
    cell = state.cells.get(cell_id)
    if not cell:
        raise KeyError(f"Unknown cell: {cell_id}")
    cell.health = max(0, min(100, cell.health + delta))
    if cell.health == 0 and cell.status != "dead":
        cell.status = "dead"
        record_event("cell_died", {"cell_id": cell_id})
    save_state()
    return cell.health


async def _restart_alive_cells() -> None:
    """On startup, re-start loops for cells that were alive before shutdown."""
    alive = [c for c in state.cells.values() if c.status == "alive"]
    if not alive:
        return
    log.info("Restarting %d alive cell(s) on runtime (staggered)...", len(alive))
    # Give cell-runtime a moment to finish starting up
    await asyncio.sleep(5)
    for cell in alive:
        try:
            await _notify_runtime_start(cell)
            log.info("Restarted cell %s on runtime", cell.cell_id)
        except Exception as exc:
            log.error("Failed to restart cell %s: %s", cell.cell_id, exc)
        # Stagger starts so cells don't all race to claim the same projects
        await asyncio.sleep(3)


async def _notify_runtime_start(cell: CellRecord) -> None:
    """Tell the cell-runtime to start a cell. Non-fatal on failure."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{CELL_RUNTIME_URL}/cells/start",
                json={"cell_id": cell.cell_id, "traits": cell.traits},
                headers=_auth_headers(),
                timeout=10,
            )
        cell.status = "alive"
    except Exception as exc:
        log.error("Failed to start cell %s on runtime: %s", cell.cell_id, exc)
        cell.status = "alive"  # Mark alive — monitor will validate later
    save_state()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not LIFE_API_SECRET:
        log.warning("LIFE_API_SECRET not set — inter-service auth is DISABLED")
    load_state()
    try:
        validate_dna()
    except Exception as exc:
        log.error("DNA validation failed: %s", exc)
    # Restart any cells that were alive before shutdown
    asyncio.create_task(_restart_alive_cells())
    # Auto-init: if no alive cells exist, bootstrap the experiment
    asyncio.create_task(_auto_init())
    yield
    save_state()


async def _auto_init():
    """Automatically initialize the experiment if no cells are alive."""
    # Wait for cell-runtime and guardians to be ready
    await asyncio.sleep(8)
    if any(c.status != "dead" for c in state.cells.values()):
        return  # Already have alive cells (from restart or manual init)
    log.info("No alive cells found — auto-initializing experiment")
    try:
        dna_info = validate_dna()
        cell = create_cell()
        await _notify_runtime_start(cell)
        record_event("experiment_initialized", {"cell_id": cell.cell_id, "dna": dna_info, "auto": True})
        log.info("Auto-initialized experiment with cell %s", cell.cell_id)
    except Exception as exc:
        log.error("Auto-init failed: %s", exc)


app = FastAPI(title="Life Kernel", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if LIFE_API_SECRET and request.headers.get("X-Life-Secret") != LIFE_API_SECRET:
        return JSONResponse(status_code=403, content={"detail": "Invalid or missing API secret"})
    return await call_next(request)


def _auth_headers() -> dict:
    return {"X-Life-Secret": LIFE_API_SECRET} if LIFE_API_SECRET else {}


class SpawnRequest(BaseModel):
    parent_id: str | None = None
    traits: dict = Field(default_factory=dict)


class ReplicateRequest(BaseModel):
    parent_id: str
    traits: dict = Field(default_factory=dict)


class HealthDelta(BaseModel):
    delta: int


@app.get("/health")
async def health():
    alive = [c for c in state.cells.values() if c.status in ("alive", "initializing", "repairing")]
    return {"status": "ok", "experiment": state.experiment_id, "alive_cells": len(alive)}


@app.get("/state")
async def get_state():
    return state.model_dump()


@app.post("/cells/spawn")
async def spawn_cell(req: SpawnRequest):
    async with _spawn_lock:
        try:
            cell = create_cell(parent_id=req.parent_id, traits=req.traits if req.traits else None)
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc))

    await _notify_runtime_start(cell)
    return cell.model_dump()


@app.post("/cells/replicate")
async def replicate_cell(req: ReplicateRequest):
    """Replication with controlled mutation — called by cells through runtime."""
    parent = state.cells.get(req.parent_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent cell not found")
    if parent.status == "dead":
        raise HTTPException(status_code=400, detail="Dead cells cannot replicate")

    # Rate limit: one replication per cell per cooldown period
    now = time.time()
    last = _cell_last_replication.get(req.parent_id, 0)
    if now - last < REPLICATION_COOLDOWN:
        raise HTTPException(status_code=429, detail=f"Replication too frequent — wait {int(REPLICATION_COOLDOWN - (now - last))}s")

    # Mutate parent traits for child
    child_traits = mutate_traits(req.traits if req.traits else parent.traits)

    async with _spawn_lock:
        try:
            child = create_cell(parent_id=req.parent_id, traits=child_traits)
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc))

    _cell_last_replication[req.parent_id] = now
    record_event("replication", {
        "parent_id": req.parent_id,
        "child_id": child.cell_id,
        "parent_traits": parent.traits,
        "child_traits": child_traits,
    })
    log.info("Replication: %s -> %s", req.parent_id, child.cell_id)

    await _notify_runtime_start(child)
    return child.model_dump()


@app.post("/cells/{cell_id}/kill")
async def api_kill_cell(cell_id: str, reason: str = "manual"):
    try:
        kill_cell(cell_id, reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="Cell not found")
    # Also stop on runtime
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{CELL_RUNTIME_URL}/cells/{cell_id}/stop", headers=_auth_headers(), timeout=5)
    except Exception:
        pass
    return {"status": "terminated", "cell_id": cell_id}


@app.post("/cells/{cell_id}/health")
async def api_update_health(cell_id: str, body: HealthDelta):
    try:
        new_health = update_health(cell_id, body.delta)
    except KeyError:
        raise HTTPException(status_code=404, detail="Cell not found")
    return {"cell_id": cell_id, "health": new_health}


@app.get("/cells")
async def list_cells():
    return {cid: c.model_dump() for cid, c in state.cells.items()}


@app.get("/cells/{cell_id}")
async def get_cell(cell_id: str):
    cell = state.cells.get(cell_id)
    if not cell:
        raise HTTPException(status_code=404, detail="Cell not found")
    return cell.model_dump()


@app.post("/init")
async def initialize_experiment():
    """Bootstrap the experiment — validate DNA and spawn the first cell."""
    dna_info = validate_dna()
    if any(c.status != "dead" for c in state.cells.values()):
        alive = sum(1 for c in state.cells.values() if c.status != "dead")
        return {"status": "already_initialized", "alive_cells": alive}
    cell = create_cell()
    await _notify_runtime_start(cell)
    record_event("experiment_initialized", {"cell_id": cell.cell_id, "dna": dna_info})
    return {"status": "initialized", "cell_id": cell.cell_id, "experiment": state.experiment_id}


@app.get("/events")
async def get_events(limit: int = 50):
    return state.events[-limit:]


@app.post("/cells/{cell_id}/cleanup")
async def cleanup_dead_cell(cell_id: str):
    """Remove a dead cell record from state — called by monitor after grace period."""
    cell = state.cells.get(cell_id)
    if not cell:
        raise HTTPException(status_code=404, detail="Cell not found")
    if cell.status != "dead":
        raise HTTPException(status_code=400, detail="Cell is not dead — cannot clean up")
    del state.cells[cell_id]
    record_event("cell_cleaned_up", {"cell_id": cell_id})
    log.info("Cleaned up dead cell %s", cell_id)
    return {"status": "cleaned_up", "cell_id": cell_id}
