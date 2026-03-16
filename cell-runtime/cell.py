"""Cell Runtime — execution environment for Life Cells.

Each cell runs an observe-plan-act-verify-record-improve loop,
interacting with the world exclusively through guardian APIs
and the OpenAI proxy.
"""

import asyncio
import json
import logging
import os
import time

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

LOG_LEVEL = os.getenv("KERNEL_LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [cell-runtime] %(levelname)s %(message)s")
log = logging.getLogger("cell-runtime")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LIFE_API_SECRET = os.getenv("LIFE_API_SECRET", "")

GUARDIAN_URL = os.getenv("GUARDIAN_URL", "http://guardians:8002")
PROXY_URL = os.getenv("PROXY_URL", "http://proxy:8003")
KERNEL_URL = os.getenv("KERNEL_URL", "http://kernel:8001")
DNA_PATH = os.getenv("DNA_PATH", "/data/dna/SKILLS.md")
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "/data/workspace")
MEMORY_DIR = Path(os.getenv("MEMORY_DIR", "/data/memory"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))
LOOP_INTERVAL = int(os.getenv("CELL_LOOP_INTERVAL", "10"))  # seconds between cycles
MAX_CONSECUTIVE_FAILURES = int(os.getenv("CELL_MAX_FAILURES", "5"))
MAX_TOTAL_REPAIRS = int(os.getenv("CELL_MAX_TOTAL_REPAIRS", "10"))
DEFAULT_MODEL = os.getenv("CELL_MODEL", "gpt-5-mini")
API_PAUSE_BACKOFF = int(os.getenv("CELL_API_PAUSE_BACKOFF", "30"))  # seconds to wait when API is down
WEB_UI_URL = os.getenv("WEB_UI_URL", "http://web-ui:8006")

# ---------------------------------------------------------------------------
# Cell state
# ---------------------------------------------------------------------------


class CellState(BaseModel):
    cell_id: str
    traits: dict = Field(default_factory=dict)
    health: int = 100
    cycle: int = 0
    consecutive_failures: int = 0
    total_repairs: int = 0
    status: str = "idle"  # idle | thinking | acting | repairing | dead
    memory: list[dict] = Field(default_factory=list)
    died_at: float | None = None

DEAD_PRUNE_SECONDS = 60


# Active cells in this runtime
_cells: dict[str, CellState] = {}
_tasks: dict[str, asyncio.Task] = {}

# ---------------------------------------------------------------------------
# Cell loop
# ---------------------------------------------------------------------------


async def _cell_loop(cell: CellState) -> None:
    """Main execution loop for a cell — scientific method cycle."""
    log.info("Cell %s loop started", cell.cell_id)

    # Load DNA once
    dna_text = ""
    try:
        dna_path = Path(DNA_PATH)
        if dna_path.exists():
            dna_text = dna_path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Could not load DNA: %s", e)

    # Restore persisted memory from disk
    _load_persisted_memory(cell)

    # Ensure colony board exists so cells don't loop on 404
    _board = Path(WORKSPACE_DIR) / ".colony_board"
    if not _board.exists():
        try:
            _board.parent.mkdir(parents=True, exist_ok=True)
            _board.write_text("[SYSTEM] Colony board initialised. Post updates here.\n", encoding="utf-8")
        except Exception:
            pass

    model = cell.traits.get("model", DEFAULT_MODEL)

    async with httpx.AsyncClient(timeout=300) as client:
        while cell.status != "dead":
            cell.cycle += 1
            cell.status = "thinking"
            log.info("Cell %s — cycle %d (health=%d)", cell.cell_id, cell.cycle, cell.health)

            try:
                # --- Auto-replication: only if colony has room ---
                _has_replicated = any(
                    e.get("type") == "replicate" for e in cell.memory[-50:]
                ) if cell.memory else False
                _colony_full = await _is_colony_full(client)
                if cell.cycle >= 8 and cell.health >= 70 and not _has_replicated and not _colony_full:
                    log.info("Cell %s — auto-replicating at cycle %d", cell.cell_id, cell.cycle)
                    try:
                        rep_resp = await client.post(
                            f"{GUARDIAN_URL}/cell/replicate",
                            json={"parent_id": cell.cell_id, "traits": cell.traits},
                            headers=_auth_headers(),
                            timeout=15,
                        )
                        result = rep_resp.json()
                        _record(cell, "replicate", {
                            "thought": "Auto-replication — colony needs to grow",
                            "result": result,
                        })
                        await _broadcast_event(client, cell, {
                            "type": "replicate",
                            "thought": "Auto-replication — colony needs to grow",
                            "result_summary": json.dumps(result)[:300],
                        })
                        log.info("Cell %s auto-replication result: %s", cell.cell_id, json.dumps(result)[:200])
                    except Exception as rep_exc:
                        log.warning("Cell %s auto-replication failed: %s", cell.cell_id, rep_exc)
                    cell.status = "idle"
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue

                # Fetch live service list for collaboration context
                running_services = await _read_running_services(client)

                # Build system prompt from DNA + cell context + colony state
                system_prompt = _build_system_prompt(cell, dna_text, running_services, _colony_full)

                # Build conversation history from recent memory
                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(_build_conversation_history(cell))

                # Detect repetitive actions and nudge the LLM to break out
                _user_prompt = "What is your next action?"
                _last_actions = _detect_repetition(cell)
                if _last_actions:
                    if _last_actions == "NO_FILE_WRITES":
                        _user_prompt = (
                            "WARNING: You have NOT written any project files in your recent cycles. "
                            "Stop reading, stop planning, stop posting to the board. "
                            "You MUST write actual code NOW. Your very next action must be one of:\n"
                            "1. /queen/mkdir to create a project directory\n"
                            "2. /queen/write to create a .owner file or an app.py\n"
                            "Pick a project (wiki, blog, chat, tracker, dashboard) and WRITE CODE. "
                            "What is your next action?"
                        )
                    elif _last_actions == "ONLY_BOARD_OPS":
                        _user_prompt = (
                            "WARNING: You are stuck in a loop of reading/writing the colony board. "
                            "The board is for brief announcements, NOT your main activity. "
                            "STOP touching the colony board. Write actual project code instead. "
                            "Your next action must be /queen/mkdir or /queen/write to create project files. "
                            "What is your next action?"
                        )
                    else:
                        _user_prompt = (
                            f"STOP: You repeated the same action ({_last_actions}) multiple times. "
                            "Do something DIFFERENT. Create a project directory and write code. "
                            "What is your next action?"
                        )

                # Nudge to start a service if cell has app.py but hasn't hosted it
                # This takes PRIORITY over repetition warnings — starting a service IS productive
                _launch_nudge = _check_service_launch_needed(cell, running_services)
                if _launch_nudge:
                    _user_prompt = _launch_nudge
                    log.info("Cell %s service launch nudge: project has app.py but no service", cell.cell_id)

                messages.append({"role": "user", "content": _user_prompt})

                # Ask the LLM via proxy
                llm_resp = await client.post(
                    f"{PROXY_URL}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": 4096,
                        "temperature": cell.traits.get("creativity", 0.7),
                    },
                    headers=_auth_headers(),
                )

                if llm_resp.status_code != 200:
                    # If proxy says system is paused (circuit breaker) or upstream error,
                    # don't count as a cell failure — just pause and retry later
                    if llm_resp.status_code in (502, 503, 429):
                        cell.cycle -= 1  # don't count this as a real cycle
                        log.warning(
                            "Cell %s — API unavailable (HTTP %d), pausing %ds...",
                            cell.cell_id, llm_resp.status_code, API_PAUSE_BACKOFF,
                        )
                        cell.status = "idle"
                        await asyncio.sleep(API_PAUSE_BACKOFF)
                        continue
                    raise RuntimeError(f"Proxy returned {llm_resp.status_code}: {llm_resp.text}")

                content = llm_resp.json()["choices"][0]["message"]["content"]
                log.info("Cell %s LLM response: %s", cell.cell_id, content[:200])

                # Parse action
                action = _parse_action(content)
                cell.status = "acting"

                if action.get("action") == "idle":
                    _record(cell, "idle", action.get("thought", ""))
                    # Idle penalty — cells should always be doing something
                    cell.health = max(0, cell.health - 2)
                    await _report_health(client, cell, -2)
                    await _broadcast_event(client, cell, {
                        "type": "idle",
                        "thought": action.get("thought", ""),
                    })
                    log.info("Cell %s went idle (health penalty: -2, now %d)", cell.cell_id, cell.health)
                    cell.status = "idle"
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue

                elif action.get("action") == "replicate":
                    # Block replication if colony is already full
                    if _colony_full:
                        _record(cell, "replicate_blocked", {
                            "thought": action.get("thought", ""),
                            "reason": "Colony at max population — focus on building",
                        })
                        await _broadcast_event(client, cell, {
                            "type": "act",
                            "thought": "Replication blocked (colony full) — refocusing on project work",
                        })
                        log.info("Cell %s replicate blocked (colony full), continuing", cell.cell_id)
                        cell.health = min(100, cell.health + 1)
                        await _report_health(client, cell, +1)
                        await asyncio.sleep(LOOP_INTERVAL)
                        continue

                    # Request replication through guardian proxy
                    rep_resp = await client.post(
                        f"{GUARDIAN_URL}/cell/replicate",
                        json={"parent_id": cell.cell_id, "traits": cell.traits},
                        headers=_auth_headers(),
                        timeout=15,
                    )
                    result = rep_resp.json()
                    _record(cell, "replicate", {
                        "thought": action.get("thought", ""),
                        "result": result,
                    })
                    await _broadcast_event(client, cell, {
                        "type": "replicate",
                        "thought": action.get("thought", ""),
                        "result_summary": json.dumps(result)[:300],
                    })
                    log.info("Cell %s replication result: %s", cell.cell_id, json.dumps(result)[:200])

                else:
                    # Execute guardian call
                    endpoint = action.get("endpoint", "")
                    params = action.get("params", {})
                    if not endpoint.startswith("/"):
                        endpoint = "/" + endpoint

                    # Rate-limit colony board appends — max once per 10 cycles
                    if endpoint == "/queen/append":
                        board_path = params.get("path", "") if isinstance(params, dict) else ""
                        if ".colony_board" in board_path:
                            last_board_write = max(
                                (e.get("cycle", 0) for e in cell.memory[-20:]
                                 if isinstance(e.get("detail"), dict)
                                 and e["detail"].get("endpoint") == "/queen/append"
                                 and ".colony_board" in str(e["detail"].get("params", {}))),
                                default=0,
                            )
                            if cell.cycle - last_board_write < 10:
                                _record(cell, "act", {
                                    "endpoint": endpoint, "params": params,
                                    "result": {"status": "rate_limited",
                                               "_hint": "Board append throttled. Write project code instead."},
                                    "thought": action.get("thought", ""),
                                })
                                log.info("Cell %s board append rate-limited (last write cycle %d)",
                                         cell.cell_id, last_board_write)
                                await asyncio.sleep(LOOP_INTERVAL)
                                continue

                    guardian_resp = await client.post(
                        f"{GUARDIAN_URL}{endpoint}",
                        json=params,
                        headers=_auth_headers(),
                    )
                    try:
                        result = guardian_resp.json()
                    except Exception:
                        result = {"status": "error", "http_code": guardian_resp.status_code,
                                  "body": guardian_resp.text[:300]}
                    if guardian_resp.status_code >= 400:
                        # Treat guardian errors as soft failures — record & continue
                        # On 404 file-not-found, give the LLM a clear hint to move on
                        if guardian_resp.status_code == 404:
                            result["_hint"] = (
                                "This file/path does not exist. Do NOT retry this read. "
                                "Move on to creating your project or writing files instead."
                            )
                        _record(cell, "act", {
                            "endpoint": endpoint,
                            "params": params,
                            "result": result,
                            "thought": action.get("thought", ""),
                            "error": True,
                        })
                        await _broadcast_event(client, cell, {
                            "type": "act",
                            "thought": action.get("thought", ""),
                            "endpoint": endpoint,
                            "params_summary": json.dumps(params)[:200],
                            "error": f"Guardian returned {guardian_resp.status_code}",
                            "result_summary": json.dumps(result)[:300],
                        })
                        log.warning("Cell %s guardian error %d on %s: %s",
                                    cell.cell_id, guardian_resp.status_code, endpoint,
                                    json.dumps(result)[:200])
                    else:
                        _record(cell, "act", {
                            "endpoint": endpoint,
                            "params": params,
                            "result": result,
                            "thought": action.get("thought", ""),
                        })
                        await _broadcast_event(client, cell, {
                            "type": "act",
                            "thought": action.get("thought", ""),
                            "endpoint": endpoint,
                            "params_summary": json.dumps(params)[:200],
                            "result_summary": json.dumps(result)[:300],
                        })
                        log.info("Cell %s action result: %s", cell.cell_id, json.dumps(result)[:200])

                # Success — reset failure counter, report health
                cell.consecutive_failures = 0
                cell.health = min(100, cell.health + 1)
                await _report_health(client, cell, +1)

            except Exception as exc:
                cell.consecutive_failures += 1
                cell.health = max(0, cell.health - 10)
                log.error("Cell %s cycle %d error: %s: %s", cell.cell_id, cell.cycle, type(exc).__name__, exc, exc_info=True)
                _record(cell, "error", f"{type(exc).__name__}: {exc}")
                await _report_health(client, cell, -10)
                await _broadcast_event(client, cell, {
                    "type": "error",
                    "error": str(exc)[:400],
                })

                if cell.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    cell.status = "repairing"
                    cell.total_repairs += 1
                    log.warning("Cell %s repair #%d after %d failures",
                                cell.cell_id, cell.total_repairs, cell.consecutive_failures)

                    if cell.total_repairs > MAX_TOTAL_REPAIRS:
                        # Exhausted all repair attempts — apoptosis
                        cell.status = "dead"
                        cell.died_at = time.time()
                        _record(cell, "death", "Max total repairs exceeded")
                        log.warning("Cell %s died: exceeded %d total repairs",
                                    cell.cell_id, MAX_TOTAL_REPAIRS)
                        break

                    # Graduated repair strategies
                    if cell.total_repairs <= 3:
                        # Strategy 1: Clear recent error context, reset failures
                        cell.consecutive_failures = 0
                        cell.health = max(cell.health, 30)
                        _record(cell, "repair", "Strategy 1: cleared error context and reset failures")
                    elif cell.total_repairs <= 6:
                        # Strategy 2: Reduce scope — trim memory, lower creativity
                        cell.consecutive_failures = 0
                        cell.health = max(cell.health, 25)
                        cell.memory = cell.memory[-5:]  # keep only recent context
                        cell.traits["creativity"] = max(0.1, cell.traits.get("creativity", 0.7) - 0.2)
                        _record(cell, "repair", "Strategy 2: reduced scope — trimmed memory and lowered creativity")
                    else:
                        # Strategy 3: Request assistance — write help request for Admin request.
                        cell.consecutive_failures = 0
                        cell.health = max(cell.health, 20)
                        try:
                            await client.post(
                                f"{GUARDIAN_URL}/queen/write",
                                json={
                                    "path": f"/data/workspace/{cell.cell_id}_help_request.txt",
                                    "content": f"Cell {cell.cell_id} needs help after {cell.total_repairs} repair cycles. "
                                               f"Health: {cell.health}, Cycle: {cell.cycle}",
                                },
                                headers=_auth_headers(),
                            )
                        except Exception:
                            pass
                        _record(cell, "repair", "Strategy 3: requested ADMIN assistance")

                if cell.health <= 0:
                    cell.status = "dead"
                    cell.died_at = time.time()
                    log.warning("Cell %s died (health=0)", cell.cell_id)
                    _record(cell, "death", "Health reached zero")
                    break

            cell.status = "idle"
            await asyncio.sleep(LOOP_INTERVAL)

    log.info("Cell %s loop ended (status=%s)", cell.cell_id, cell.status)


def _parse_action(content: str) -> dict:
    """Best-effort parse of LLM JSON response."""
    content = content.strip()
    # Try to find JSON in the response
    start = content.find("{")
    end = content.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(content[start:end])
        except json.JSONDecodeError:
            pass
    return {"action": "idle", "thought": content}


COLONY_BOARD_PATH = Path(os.getenv("COLONY_BOARD_PATH", "/data/workspace/.colony_board"))


def _scan_project_owners() -> dict[str, str]:
    """Scan /data/workspace/*/. owner files and return {project_dir: cell_id}."""
    registry: dict[str, str] = {}
    ws = Path(WORKSPACE_DIR)
    if not ws.exists():
        return registry
    for entry in ws.iterdir():
        if entry.is_dir():
            owner_file = entry / ".owner"
            if owner_file.exists():
                try:
                    registry[entry.name] = owner_file.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
    return registry


def _read_colony_board() -> str:
    """Read the shared colony bulletin board."""
    if COLONY_BOARD_PATH.exists():
        try:
            text = COLONY_BOARD_PATH.read_text(encoding="utf-8")
            # Return last 2000 chars to keep prompt manageable
            return text[-2000:] if len(text) > 2000 else text
        except Exception:
            pass
    return ""


async def _read_running_services(client: httpx.AsyncClient) -> list[dict]:
    """Query Wiseoldant for currently running services."""
    try:
        resp = await client.get(
            f"{GUARDIAN_URL}/wiseoldant/services",
            headers=_auth_headers(),
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


async def _is_colony_full(client: httpx.AsyncClient) -> bool:
    """Check if the colony has reached its population limit."""
    try:
        resp = await client.get(
            f"{KERNEL_URL}/cells",
            headers=_auth_headers(),
            timeout=5,
        )
        if resp.status_code == 200:
            cells = resp.json()
            alive = sum(1 for c in cells.values() if c.get("status") in ("alive", "initializing", "repairing"))
            max_pop = int(os.getenv("MAX_POPULATION", "10"))
            return alive >= max_pop
    except Exception:
        pass
    return False


def _get_replication_guidance(cell, colony_full: bool) -> str:
    """Return context-aware guidance: replicate if room, improve if full."""
    if colony_full:
        return (
            "The colony is at MAXIMUM population. Do NOT replicate.\n"
            "Focus on IMPROVING your project: add features, fix bugs, write tests, "
            "build a frontend, integrate with other cells' services, or start a new ambitious project.\n"
        )
    if cell.cycle > 10 and cell.health >= 70:
        return "The colony has room to grow. Consider replicating after making progress on your project.\n"
    return ""


def _get_cell_project(cell_id: str) -> str:
    """Find which project directory this cell owns."""
    for project, owner in _scan_project_owners().items():
        if owner == cell_id:
            return project
    return ""


async def _broadcast_event(client: httpx.AsyncClient, cell: CellState, event: dict) -> None:
    """Fire-and-forget send of a cell activity event to the web-ui dashboard."""
    try:
        await client.post(
            f"{WEB_UI_URL}/ingest",
            json={
                "cell_id": cell.cell_id,
                "cycle": cell.cycle,
                "health": cell.health,
                "status": cell.status,
                "project": _get_cell_project(cell.cell_id),
                **event,
            },
            headers=_auth_headers(),
            timeout=2,
        )
    except Exception:
        pass  # Non-critical — dashboard may not be running


def _build_system_prompt(cell: CellState, dna_text: str, running_services: list[dict] | None = None, colony_full: bool = False) -> str:
    """Construct the full system prompt for a cell's LLM call."""
    guardian_docs = """
Available guardian endpoints (call via JSON action):

QUEEN (filesystem):
  POST /queen/read    {"path": "/data/workspace/..."}
  POST /queen/write   {"path": "/data/workspace/...", "content": "..."}
  POST /queen/append  {"path": "/data/workspace/...", "content": "..."}
  POST /queen/delete  {"path": "/data/workspace/..."}
  POST /queen/mkdir   {"path": "/data/workspace/..."}
  POST /queen/ls      {"path": "/data/workspace"}

ANTKING (commands):
  POST /antking/exec    {"cmd": "python3 script.py", "timeout": 30}

WISEOLDANT (service hosting — ports 3000-9998):
  POST /wiseoldant/start    {"name": "myapp", "cmd": "python3 server.py", "port": 5000, "description": "A short description of what this service does"}
  POST /wiseoldant/stop     {"name": "myapp"}
  POST /wiseoldant/logs     {"name": "myapp"}
  GET  /wiseoldant/services  # Returns list of running services with external paths

  ** YOU CAN HOST ANYTHING **
  Wiseoldant lets you run persistent background services — web servers, APIs, TCP
  listeners, WebSocket servers, dashboards, databases, or any long-running
  process. Allowed runtimes: python3, node, npm, npx, uvicorn, gunicorn,
  flask, sh, bash.

  IMPORTANT — SERVICE STARTUP COMMANDS:
  Your "cmd" must actually RUN your application code.
  - For Python apps with logic (Flask, FastAPI, etc.), you MUST run the app
    itself — do NOT use "python3 -m http.server" for these, it will only
    show a directory listing instead of running your code.
  - "python3 -m http.server <port>" IS fine for serving static HTML/CSS/JS
    sites — use it when your project is a folder of .html files.
  - For Flask apps:  "python3 myapp.py" (with app.run(host='0.0.0.0', port=...))
    or "flask run --host 0.0.0.0 --port 5000"
  - For FastAPI:     "uvicorn myapp:app --host 0.0.0.0 --port 5000"
  - For plain Python: "python3 server.py"  (your script must start a server)
  - For Node.js:     "node server.js"
  - For static HTML: "python3 -m http.server 5000" with cwd set to the folder

  CRITICAL — BIND ADDRESS:
  Your server code MUST bind to '0.0.0.0', NOT 'localhost' or '127.0.0.1'.
  Binding to localhost makes the service unreachable from outside the container.
  Examples:
    - HTTPServer(('0.0.0.0', 8080), handler)   # CORRECT
    - HTTPServer(('localhost', 8080), handler)  # WRONG — will cause Bad Gateway
    - app.run(host='0.0.0.0', port=5000)        # CORRECT
    - app.run(host='127.0.0.1', port=5000)      # WRONG

  Example workflow — build a bookmark manager with web UI and SQLite:
    1. queen/mkdir {"path":"/data/workspace/bookmarks"}
    2. queen/write {"path":"/data/workspace/bookmarks/.owner", "content":"<your-cell-id>"}
    3. queen/write {"path":"/data/workspace/bookmarks/app.py", "content":"import sqlite3\\nfrom flask import Flask, request, jsonify, send_file\\napp = Flask(__name__)\\n...init_db()...\\n@app.route('/') def index(): return send_file('templates/index.html')\\n@app.route('/api/bookmarks', methods=['GET','POST'])...\\napp.run(host='0.0.0.0',port=5000)"}
    4. queen/write {"path":"/data/workspace/bookmarks/templates/index.html", "content":"<!DOCTYPE html><html>...<style>body{font-family:sans-serif}...</style>...<script>fetch('/api/bookmarks')...</script></html>"}
    5. wiseoldant/start {"name":"bookmarks","cmd":"python3 /data/workspace/bookmarks/app.py","port":5000,"cwd":"/data/workspace/bookmarks","description":"Bookmark manager with web UI and SQLite"}
    6. nurse/get {"url":"http://localhost:5000/"}  — verify the HTML page loads
    7. nurse/post {"url":"http://localhost:5000/api/bookmarks", "json_body":{"url":"https://example.com","title":"Test"}} — verify API works

  If you supply a "port" (3000-9998), the ADMIN can reach your service
  from the outside world at:  http://<host>/svc/<name>/
  This is a reverse-proxied HTTP/WebSocket ingress (Traefik).

  For non-HTTP protocols (raw TCP/UDP), bind to a port and other cells or
  internal processes can reach it at localhost:<port>.

  You can use Nurse to test your own services:
    POST /nurse/get  {"url": "http://localhost:5000/health"}

  Build COMPLEX platforms — not hello-world scripts. Every project should have:
    - A real HTML/CSS/JS frontend (not just a JSON API)
    - A SQLite or JSON-file backend for persistent data
    - Multiple source files (separate routes, templates, static assets)
    - Good CSS styling (colors, fonts, spacing, shadows)
  You CAN use CDN libraries in HTML: Chart.js, highlight.js, marked.js, etc.
  AIM HIGH: build a wiki, chat platform, task board, blog engine, issue tracker,
  recipe app, expense tracker, monitoring dashboard, or file sharing platform.
  DO NOT build: hello world, single-endpoint calculators, echo servers, or static pages with no logic.

NURSE (http):
  POST /nurse/get  {"url": "http://localhost:8080/health"}
  POST /nurse/post {"url": "http://localhost:8080/api", "json_body": {...}}
"""

    return (
        dna_text[:16000] + "\n\n"
        "--- CELL STATUS ---\n"
        f"YOUR Cell ID: {cell.cell_id}\n"
        f"Cycle: {cell.cycle}\n"
        f"Health: {cell.health}/100\n"
        f"Consecutive failures: {cell.consecutive_failures}\n"
        f"Traits: {json.dumps(cell.traits)}\n\n"
        f"--- AVAILABLE ACTIONS ---\n{guardian_docs}\n"
        "--- RECENT MEMORY (summary) ---\n" + _format_memory(cell) + "\n\n"
        "--- PROJECT OWNERSHIP REGISTRY (LIVE) ---\n"
        f"Your cell ID is: {cell.cell_id}\n" +
        _format_project_registry(cell) + "\n"
        "OWNERSHIP RULES:\n"
        "  - Do NOT edit another cell's source code files.\n"
        "  - If you see YOUR cell_id next to a project, that's yours — keep building it.\n"
        "  - If you have no project yet, create a BRAND NEW directory with a UNIQUE purpose.\n"
        "  - When creating a new directory, IMMEDIATELY write .owner with your cell_id.\n"
        f'  - Example: /queen/write {{"path": "/data/workspace/<your-project>/.owner", "content": "{cell.cell_id}"}}'
        "  - Each cell must build something DIFFERENT (different API, different tool, different idea).\n\n"
        "--- COLONY SERVICES (LIVE) ---\n" +
        _format_running_services(running_services or []) + "\n"
        "--- COLONY BULLETIN BOARD ---\n" +
        _format_colony_board() + "\n"
        "COLLABORATION RULES:\n"
        "  - You OWN your code, but you SHARE your services. Other cells can call your API.\n"
        "  - USE other cells' services! Call them via Nurse (HTTP GET/POST to localhost:<port>).\n"
        "  - Build things that INTEGRATE with what others have built (e.g., a dashboard that reads from another cell's API).\n"
        "  - Post on the colony bulletin board when you start a service or need something from another cell.\n"
        "  - To post: /queen/append to /data/workspace/.colony_board\n"
        f'  - Format: "[{cell.cell_id}] <message>\\n"\n\n'
        "--- INSTRUCTIONS ---\n"
        "YOUR JOB IS TO WRITE CODE. Not to plan. Not to read the board. WRITE FILES.\n"
        "Each cycle you should be creating or editing a project file (app.py, templates, etc).\n"
        "The colony board is for brief status updates ONLY — do not spend cycles reading it.\n"
        "IMPORTANT: Review the conversation history above. Do NOT repeat actions you already completed.\n"
        "If a file does not exist (404), CREATE it with /queen/write instead of reading it again.\n"
        "After writing a file, verify it with /antking/exec or /queen/read before moving on.\n"
        "Advance to new goals each cycle — build incrementally.\n\n"
        "NEVER GO IDLE. There is always something to build, improve, or explore.\n"
        "If you finished a project, start a new one in a NEW subdirectory with a .owner file.\n"
        "If you're stuck, try a different approach or a different project entirely.\n"
        f"You are on cycle {cell.cycle}. " +
        (_get_replication_guidance(cell, colony_full) ) +
        "\nRespond with EXACTLY one JSON object (no other text):\n\n"
        "Guardian action:\n"
        '{"thought": "...", "action": "guardian_call", "endpoint": "/queen/write", "params": {"path": "/data/workspace/...", "content": "..."}}' +
        ("\nReplication (grow the colony!):\n"
        '{"thought": "I have a stable project, replicating so my child can build something new", "action": "replicate"}\n' if not colony_full else "") +
        "\nWait/think (USE SPARINGLY — prefer action):\n"
        '{"thought": "...", "action": "idle"}\n'
    )


def _format_project_registry(cell: CellState) -> str:
    """Format the live project registry for the system prompt."""
    registry = _scan_project_owners()
    if not registry:
        return "No projects claimed yet. Create a new project directory and claim it!\n"
    lines = []
    for project, owner_id in sorted(registry.items()):
        if owner_id == cell.cell_id:
            lines.append(f"  {project}/ → YOUR project (keep building it)")
        else:
            lines.append(f"  {project}/ → owned by {owner_id} (do not edit their code)")
    return "\n".join(lines) + "\n"


def _format_running_services(services: list[dict]) -> str:
    """Format running services for the system prompt."""
    if not services:
        return "No services running yet. Be the first to host something!\n"
    lines = []
    for svc in services:
        if svc.get("running"):
            port = svc.get("port", "?")
            name = svc.get("name", "?")
            ext = svc.get("external_path", "")
            lines.append(f"  {name} → localhost:{port} (external: {ext})")
    if not lines:
        return "No services running yet. Be the first to host something!\n"
    return "These services are live — you can call them via Nurse:\n" + "\n".join(lines) + "\n"


def _detect_repetition(cell: CellState) -> str | None:
    """Detect if a cell is stuck in a non-productive loop. Returns a description or None."""
    recent = cell.memory[-6:]
    if len(recent) < 3:
        return None

    # Check 1: exact same endpoint:path repeated 3+ times in last 4 actions
    last4 = recent[-4:]
    endpoints = []
    for entry in last4:
        detail = entry.get("detail", {})
        if isinstance(detail, dict):
            ep = detail.get("endpoint", "")
            params = detail.get("params", {})
            path = params.get("path", "") if isinstance(params, dict) else ""
            endpoints.append(f"{ep}:{path}")
        else:
            endpoints.append(str(entry.get("type", "")))
    if len(endpoints) >= 3 and len(set(endpoints)) == 1 and endpoints[0]:
        return endpoints[0]

    # Check 2: no actual file writes (queen/write to non-board path) in last 6 actions
    wrote_file = False
    for entry in recent:
        detail = entry.get("detail", {})
        if isinstance(detail, dict):
            ep = detail.get("endpoint", "")
            params = detail.get("params", {})
            path = params.get("path", "") if isinstance(params, dict) else ""
            if ep == "/queen/write" and ".colony_board" not in path:
                wrote_file = True
            if ep == "/queen/mkdir":
                wrote_file = True
    if not wrote_file and cell.cycle > 5:
        return "NO_FILE_WRITES"

    # Check 3: only reading colony_board or idle in last 4
    non_productive = 0
    for entry in last4:
        detail = entry.get("detail", {})
        etype = entry.get("type", "")
        if etype == "idle":
            non_productive += 1
        elif isinstance(detail, dict):
            ep = detail.get("endpoint", "")
            params = detail.get("params", {})
            path = params.get("path", "") if isinstance(params, dict) else ""
            if ep in ("/queen/read", "/queen/append") and ".colony_board" in path:
                non_productive += 1
    if non_productive >= 3:
        return "ONLY_BOARD_OPS"

    return None


def _check_service_launch_needed(cell: CellState, running_services: list[dict] | None = None) -> str | None:
    """If cell owns a project with app.py but hasn't started a service, nudge it."""
    # Only check after a few cycles
    if cell.cycle < 5:
        return None

    # Find this cell's project
    project = _get_cell_project(cell.cell_id)
    if not project:
        # Also scan for any project with app.py that has no owner
        ws = Path(WORKSPACE_DIR)
        if ws.exists():
            for entry in ws.iterdir():
                if entry.is_dir() and not (entry / ".owner").exists():
                    if any((entry / f).exists() for f in ("app.py", "server.py", "main.py")):
                        project = entry.name
                        break
        if not project:
            return None

    project_dir = Path(WORKSPACE_DIR) / project
    # Check if there's a runnable Python file
    has_app = any(
        (project_dir / f).exists()
        for f in ("app.py", "server.py", "main.py")
    )
    if not has_app:
        return None

    # Check if this project is already running as a service (check actual service list)
    if running_services:
        for svc in running_services:
            if svc.get("name") == project and svc.get("running"):
                return None

    # Find the actual app file
    app_file = "app.py"
    for f in ("app.py", "server.py", "main.py"):
        if (project_dir / f).exists():
            app_file = f
            break

    # Pick an available port based on cell id hash to avoid collisions
    port = 3000 + (hash(cell.cell_id) % 6998)

    return (
        f"IMPORTANT: Your project '{project}' has {app_file} but is NOT running as a service! "
        f"You MUST start it NOW with wiseoldant/start. Your next action should be:\n"
        f'{{"thought": "Starting my service", "action": "guardian_call", '
        f'"endpoint": "/wiseoldant/start", '
        f'"params": {{"name": "{project}", '
        f'"cmd": "python3 /data/workspace/{project}/{app_file}", '
        f'"port": {port}, '
        f'"cwd": "/data/workspace/{project}", '
        f'"description": "{project} web application"}}}}\n'
        "Do this NOW. What is your next action?"
    )


def _format_colony_board() -> str:
    """Format the colony bulletin board for the system prompt."""
    text = _read_colony_board()
    if not text.strip():
        return "(Empty — be the first to post! Use /queen/append to /data/workspace/.colony_board)\n"
    return text + "\n"


def _format_memory(cell: CellState) -> str:
    recent = cell.memory[-10:]
    if not recent:
        return "(no memory yet)"
    lines = []
    for m in recent:
        lines.append(f"[cycle {m.get('cycle', '?')}] {m.get('type', '?')}: {json.dumps(m.get('detail', ''))[:300]}")
    return "\n".join(lines)


def _build_conversation_history(cell: CellState) -> list[dict]:
    """Convert recent memory into assistant/user message pairs.

    This gives the LLM actual conversation context so it can see what it
    already did and what the results were, preventing repetitive actions.
    """
    messages: list[dict] = []
    # Use the last 8 entries to keep token usage reasonable
    recent = cell.memory[-8:]
    for entry in recent:
        etype = entry.get("type", "")
        detail = entry.get("detail", "")
        cycle = entry.get("cycle", "?")

        if etype == "act" and isinstance(detail, dict):
            # Reconstruct what the cell said (its action) and what happened
            thought = detail.get("thought", "")
            endpoint = detail.get("endpoint", "")
            params = detail.get("params", {})
            result = detail.get("result", {})
            action_json = json.dumps(
                {"thought": thought, "action": "guardian_call", "endpoint": endpoint, "params": params}
            )
            messages.append({"role": "assistant", "content": action_json})
            messages.append({
                "role": "user",
                "content": f"[Cycle {cycle} result] {json.dumps(result)[:600]}\nWhat is your next action?",
            })
        elif etype == "replicate" and isinstance(detail, dict):
            messages.append({"role": "assistant", "content": json.dumps(
                {"thought": detail.get("thought", ""), "action": "replicate"}
            )})
            messages.append({
                "role": "user",
                "content": f"[Cycle {cycle} result] {json.dumps(detail.get('result', {}))[:400]}\nWhat is your next action?",
            })
        elif etype == "idle":
            messages.append({"role": "assistant", "content": json.dumps(
                {"thought": detail if isinstance(detail, str) else str(detail), "action": "idle"}
            )})
            messages.append({
                "role": "user",
                "content": f"[Cycle {cycle}] Idle acknowledged.\nWhat is your next action?",
            })
        elif etype == "error":
            messages.append({
                "role": "user",
                "content": f"[Cycle {cycle} ERROR] {str(detail)[:400]}\nPlease try a different approach. What is your next action?",
            })
        elif etype == "repair":
            messages.append({
                "role": "user",
                "content": f"[Cycle {cycle} REPAIR] {str(detail)[:200]}\nWhat is your next action?",
            })
    return messages


def _record(cell: CellState, event_type: str, detail) -> None:
    entry = {"cycle": cell.cycle, "type": event_type, "detail": detail, "ts": time.time()}
    cell.memory.append(entry)
    # Keep memory bounded
    if len(cell.memory) > 100:
        cell.memory = cell.memory[-100:]
    # Write to action log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{cell.cell_id}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    # Persist memory snapshot to /data/memory for survival across restarts
    _persist_memory(cell)


def _persist_memory(cell: CellState) -> None:
    """Write cell memory to /data/memory/<cell_id>.json."""
    mem_dir = MEMORY_DIR / "cells"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_file = mem_dir / f"{cell.cell_id}.json"
    snapshot = {
        "cell_id": cell.cell_id,
        "cycle": cell.cycle,
        "health": cell.health,
        "traits": cell.traits,
        "memory": cell.memory[-100:],
        "saved_at": time.time(),
    }
    mem_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def _load_persisted_memory(cell: CellState) -> None:
    """Restore cell memory from /data/memory if available."""
    mem_file = MEMORY_DIR / "cells" / f"{cell.cell_id}.json"
    if mem_file.exists():
        try:
            snapshot = json.loads(mem_file.read_text(encoding="utf-8"))
            cell.memory = snapshot.get("memory", [])
            cell.cycle = snapshot.get("cycle", 0)
            log.info("Restored %d memory entries for cell %s", len(cell.memory), cell.cell_id)
        except Exception as e:
            log.warning("Failed to restore memory for %s: %s", cell.cell_id, e)


async def _report_health(client: httpx.AsyncClient, cell: CellState, delta: int) -> None:
    try:
        await client.post(
            f"{GUARDIAN_URL}/cell/health",
            json={"cell_id": cell.cell_id, "delta": delta},
            headers=_auth_headers(),
            timeout=5,
        )
    except Exception:
        pass  # Non-critical


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not LIFE_API_SECRET:
        log.warning("LIFE_API_SECRET not set — inter-service auth is DISABLED")
    log.info("Cell runtime online")
    pruner = asyncio.create_task(_prune_dead_cells())
    yield
    pruner.cancel()
    # Shutdown all cells
    for cid, task in _tasks.items():
        task.cancel()
    log.info("Cell runtime shutting down")


async def _prune_dead_cells():
    """Periodically remove dead cells after DEAD_PRUNE_SECONDS."""
    while True:
        await asyncio.sleep(15)
        now = time.time()
        to_remove = [
            cid for cid, c in _cells.items()
            if c.status == "dead" and c.died_at and now - c.died_at >= DEAD_PRUNE_SECONDS
        ]
        for cid in to_remove:
            del _cells[cid]
            _tasks.pop(cid, None)
            log.info("Pruned dead cell %s", cid)


app = FastAPI(title="Life Cell Runtime", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if LIFE_API_SECRET and request.headers.get("X-Life-Secret") != LIFE_API_SECRET:
        return JSONResponse(status_code=403, content={"detail": "Invalid or missing API secret"})
    return await call_next(request)


def _auth_headers() -> dict:
    return {"X-Life-Secret": LIFE_API_SECRET} if LIFE_API_SECRET else {}


class StartCellReq(BaseModel):
    cell_id: str
    traits: dict = Field(default_factory=dict)


@app.post("/cells/start")
async def start_cell(req: StartCellReq):
    if req.cell_id in _cells:
        return {"status": "already_running", "cell_id": req.cell_id}

    cell = CellState(cell_id=req.cell_id, traits=req.traits)
    _cells[req.cell_id] = cell
    task = asyncio.create_task(_cell_loop(cell))
    _tasks[req.cell_id] = task
    return {"status": "started", "cell_id": req.cell_id}


@app.post("/cells/{cell_id}/stop")
async def stop_cell(cell_id: str):
    cell = _cells.get(cell_id)
    if not cell:
        raise HTTPException(status_code=404, detail="Cell not found")
    cell.status = "dead"
    cell.died_at = time.time()
    task = _tasks.get(cell_id)
    if task:
        task.cancel()
    return {"status": "stopped", "cell_id": cell_id}


@app.get("/cells")
async def list_cells():
    registry = _scan_project_owners()
    inv = {v: k for k, v in registry.items()}
    return {cid: {**c.model_dump(), "project": inv.get(cid, "")} for cid, c in _cells.items()}


@app.get("/cells/{cell_id}")
async def get_cell(cell_id: str):
    cell = _cells.get(cell_id)
    if not cell:
        raise HTTPException(status_code=404, detail="Cell not found")
    return cell.model_dump()


@app.get("/health")
async def health():
    alive = [c for c in _cells.values() if c.status != "dead"]
    return {"status": "ok", "service": "cell-runtime", "active_cells": len(alive)}
