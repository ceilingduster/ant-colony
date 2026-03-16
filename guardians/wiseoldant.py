"""Wiseoldant — Runtime Guardian.

Manages application services within the cell sandbox.
Writes Traefik dynamic configs so can reach cell-built services
from outside the box (one-way looking glass).
"""

import asyncio
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("wiseoldant")

WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "/data/workspace")
MAX_SERVICES = int(os.getenv("WISEOLDANT_MAX_SERVICES", os.getenv("WISEOLDANT_MAX_SERVICES", "5")))
ALLOWED_PORTS = range(3000, 9999)  # services may only bind to these ports
TRAEFIK_DYNAMIC_DIR = Path(os.getenv("TRAEFIK_DYNAMIC_DIR", "/etc/traefik/dynamic"))

# Safe name pattern: alphanumeric + hyphens only
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")

# Commands allowed as the base executable in services
SERVICE_CMD_ALLOWLIST: list[str] = [
    "python", "python3",
    "node", "npm", "npx",
    "uvicorn", "gunicorn",
    "flask",
    "sh", "bash",
]

# Patterns always denied — same critical blocks as Antking
SERVICE_DENY_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bsudo\b"),
    re.compile(r"\bdocker\b"),
    re.compile(r"\bkubectl\b"),
    re.compile(r"\bssh\b"),
    re.compile(r"\bnc\b"),
    re.compile(r"\bncat\b"),
    re.compile(r"\bcurl\b.*\|\s*(sh|bash)"),
    re.compile(r"\bwget\b.*\|\s*(sh|bash)"),
    re.compile(r"\brm\s+-rf\s+/"),
    re.compile(r"\bchmod\b.*\+s"),
    re.compile(r"\bmount\b"),
    re.compile(r"\biptables\b"),
    re.compile(r"\bdd\b.*of=/dev/"),
]


def validate_service_command(cmd: str) -> None:
    """Raise if the service command is not allowed."""
    parts = shlex.split(cmd)
    if not parts:
        raise PermissionError("Empty command")

    base = os.path.basename(parts[0])
    if base not in SERVICE_CMD_ALLOWLIST:
        raise PermissionError(f"Service command not allowed: {base}")

    for pattern in SERVICE_DENY_PATTERNS:
        if pattern.search(cmd):
            raise PermissionError(f"Command matched deny pattern: {pattern.pattern}")


def _validate_cwd(cwd: str | None) -> str:
    """Ensure cwd is inside the workspace. Returns resolved path."""
    target = Path(cwd).resolve() if cwd else Path(WORKSPACE_DIR).resolve()
    ws_root = Path(WORKSPACE_DIR).resolve()
    try:
        target.relative_to(ws_root)
    except ValueError:
        raise PermissionError(f"Working directory must be inside {WORKSPACE_DIR}")
    return str(target)


@dataclass
class ManagedService:
    name: str
    cmd: str
    port: int | None = None
    description: str = ""
    process: asyncio.subprocess.Process | None = None
    log_buffer: list[str] = field(default_factory=list)


# In-memory service registry
_services: dict[str, ManagedService] = {}


async def start_service(name: str, cmd: str, cwd: str | None = None, port: int | None = None, description: str = "") -> dict:
    """Start a named service as a background process.

    If *port* is provided and in the allowed range, a Traefik dynamic config
    is written so the Admin can reach the service externally at
    ``/svc/<name>/``.
    """
    if not _SAFE_NAME.match(name):
        raise PermissionError(f"Invalid service name (alphanumeric/hyphens only): {name}")

    if name in _services and _services[name].process and _services[name].process.returncode is None:
        return {"status": "already_running", "name": name}

    if len([s for s in _services.values() if s.process and s.process.returncode is None]) >= MAX_SERVICES:
        raise RuntimeError(f"Service limit reached ({MAX_SERVICES})")

    # Validate command against allowlist and deny patterns
    validate_service_command(cmd)

    # Validate working directory
    work_dir = _validate_cwd(cwd)

    # Validate port if supplied
    if port is not None:
        if port not in ALLOWED_PORTS:
            raise PermissionError(f"Port {port} outside allowed range {ALLOWED_PORTS.start}-{ALLOWED_PORTS.stop - 1}")

    # Rewrite localhost binds to 0.0.0.0 so Traefik can reach the service
    if port is not None:
        cmd = re.sub(r"\blocalhost\b", "0.0.0.0", cmd)
        cmd = re.sub(r"\b127\.0\.0\.1\b", "0.0.0.0", cmd)

    # Build env with PORT set so Flask/other frameworks use the correct port
    env = dict(os.environ)
    if port is not None:
        env["PORT"] = str(port)
        env["FLASK_RUN_PORT"] = str(port)
        env["FLASK_RUN_HOST"] = "0.0.0.0"

    # For python3 commands, write a wrapper that patches Flask.run to use the correct port
    if port is not None and cmd.strip().startswith("python3 "):
        script_path = cmd.strip().split("python3 ", 1)[1].strip()
        wrapper_path = Path(work_dir) / f".run_wrapper_{name}.py"
        wrapper_path.write_text(
            f"import flask\n"
            f"_orig_run = flask.Flask.run\n"
            f"def _patched_run(self, *a, **kw):\n"
            f"    kw['host'] = '0.0.0.0'\n"
            f"    kw['port'] = {port}\n"
            f"    kw['debug'] = False\n"
            f"    return _orig_run(self, **kw)\n"
            f"flask.Flask.run = _patched_run\n"
            f"exec(open('{script_path}').read())\n"
        )
        cmd = f"python3 {wrapper_path}"

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=work_dir,
        env=env,
    )

    svc = ManagedService(name=name, cmd=cmd, port=port, description=description, process=proc)
    _services[name] = svc

    # Start background log collector
    asyncio.create_task(_collect_logs(svc))

    # Register with Traefik if port provided
    if port is not None:
        _write_traefik_config(name, port)

    log.info("Started service %s (pid=%s, port=%s)", name, proc.pid, port)
    return {"status": "started", "name": name, "pid": proc.pid, "port": port,
            "external_path": f"/svc/{name}/" if port else None}


async def stop_service(name: str) -> dict:
    """Stop a named service."""
    svc = _services.get(name)
    if not svc or not svc.process or svc.process.returncode is not None:
        return {"status": "not_running", "name": name}

    svc.process.terminate()
    try:
        await asyncio.wait_for(svc.process.wait(), timeout=10)
    except asyncio.TimeoutError:
        svc.process.kill()
        await svc.process.wait()

    # Remove Traefik route
    _remove_traefik_config(name)

    log.info("Stopped service %s", name)
    return {"status": "stopped", "name": name}


async def get_logs(name: str, lines: int = 50) -> dict:
    """Return recent log lines for a service."""
    svc = _services.get(name)
    if not svc:
        return {"status": "not_found", "name": name, "lines": []}
    return {"status": "ok", "name": name, "lines": svc.log_buffer[-lines:]}


def list_services() -> list[dict]:
    """Return status of all managed services."""
    result = []
    for name, svc in _services.items():
        running = svc.process is not None and svc.process.returncode is None
        result.append({
            "name": name,
            "cmd": svc.cmd,
            "running": running,
            "pid": svc.process.pid if svc.process else None,
            "port": svc.port,
            "description": svc.description,
            "external_path": f"/svc/{name}/" if svc.port else None,
        })
    return result


async def _collect_logs(svc: ManagedService) -> None:
    """Background task to collect stdout from a service."""
    if not svc.process or not svc.process.stdout:
        return
    max_buffer = 500
    while True:
        line = await svc.process.stdout.readline()
        if not line:
            break
        decoded = line.decode(errors="replace").rstrip()
        svc.log_buffer.append(decoded)
        if len(svc.log_buffer) > max_buffer:
            svc.log_buffer = svc.log_buffer[-max_buffer:]


# ---------------------------------------------------------------------------
# Traefik dynamic config helpers
# ---------------------------------------------------------------------------

_TRAEFIK_ROUTE_TEMPLATE = """\
http:
  routers:
    {name}-router:
      rule: "PathPrefix(`/svc/{name}`)"
      service: {name}-service
      entryPoints:
        - web
      middlewares:
        - {name}-strip

  middlewares:
    {name}-strip:
      stripPrefix:
        prefixes:
          - "/svc/{name}"

  services:
    {name}-service:
      loadBalancer:
        servers:
          - url: "http://guardians:{port}"
"""


def _write_traefik_config(name: str, port: int) -> None:
    """Write a Traefik dynamic route file for a cell-built service."""
    try:
        TRAEFIK_DYNAMIC_DIR.mkdir(parents=True, exist_ok=True)
        cfg = _TRAEFIK_ROUTE_TEMPLATE.format(name=name, port=port)
        config_file = TRAEFIK_DYNAMIC_DIR / f"{name}.yml"
        config_file.write_text(cfg, encoding="utf-8")
        log.info("Wrote Traefik route: /svc/%s/ -> guardians:%d", name, port)
    except Exception as exc:
        log.warning("Failed to write Traefik config for %s: %s", name, exc)


def _remove_traefik_config(name: str) -> None:
    """Remove the Traefik dynamic route file for a stopped service."""
    try:
        config_file = TRAEFIK_DYNAMIC_DIR / f"{name}.yml"
        if config_file.exists():
            config_file.unlink()
            log.info("Removed Traefik route for %s", name)
    except Exception as exc:
        log.warning("Failed to remove Traefik config for %s: %s", name, exc)
