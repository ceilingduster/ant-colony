"""Antking — Command Guardian.

Runs sandboxed commands with allowlists, timeouts, and resource limits.
"""

import asyncio
import os
import re
import resource
import shlex

TIMEOUT = int(os.getenv("ANTKING_TIMEOUT", os.getenv("ANTKING_TIMEOUT", "30")))
MAX_OUTPUT = int(os.getenv("ANTKING_MAX_OUTPUT", os.getenv("ANTKING_MAX_OUTPUT", "65536")))  # 64 KB
WORKING_DIR = os.getenv("WORKSPACE_DIR", "/data/workspace")
MAX_CPU_SECONDS = int(os.getenv("ANTKING_MAX_CPU_SECONDS", os.getenv("ANTKING_MAX_CPU_SECONDS", "30")))
MAX_MEM_BYTES = int(os.getenv("ANTKING_MAX_MEM_BYTES", os.getenv("ANTKING_MAX_MEM_BYTES", str(256 * 1024 * 1024))))  # 256 MB

# Commands that are allowed to run (exact basename match)
COMMAND_ALLOWLIST: list[str] = [
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "node",
    "npm",
    "npx",
    "cat",
    "echo",
    "ls",
    "find",
    "grep",
    "head",
    "tail",
    "wc",
    "sort",
    "mkdir",
    "touch",
    "curl",  # only internal — network policy blocks external
    "sh",
    "bash",
]

# Patterns that are always denied
DENY_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+-rf\s+/"),  # destructive root rm
    re.compile(r"\bsudo\b"),
    re.compile(r"\bchmod\b.*\+s"),
    re.compile(r"\bchown\b"),
    re.compile(r"\bdd\b.*of=/dev/"),
    re.compile(r"\bmount\b"),
    re.compile(r"\bumount\b"),
    re.compile(r"\biptables\b"),
    re.compile(r"\bdocker\b"),
    re.compile(r"\bkubectl\b"),
    re.compile(r"\bssh\b"),
    re.compile(r"\bnc\b"),
    re.compile(r"\bncat\b"),
]


def validate_command(cmd: str) -> None:
    """Raise if the command is not allowed."""
    parts = shlex.split(cmd)
    if not parts:
        raise ValueError("Empty command")

    base = os.path.basename(parts[0])
    if base not in COMMAND_ALLOWLIST:
        raise PermissionError(f"Command not allowed: {base}")

    for pattern in DENY_PATTERNS:
        if pattern.search(cmd):
            raise PermissionError(f"Command matched deny pattern: {pattern.pattern}")


async def execute(cmd: str, cwd: str | None = None, timeout: int | None = None) -> dict:
    """Execute a sandboxed command and return stdout/stderr."""
    validate_command(cmd)
    work_dir = cwd or WORKING_DIR
    effective_timeout = timeout or TIMEOUT

    def _set_limits():
        """Set CPU and memory limits on the child process (Linux only)."""
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
            resource.setrlimit(resource.RLIMIT_AS, (MAX_MEM_BYTES, MAX_MEM_BYTES))
        except Exception:
            pass  # Non-fatal: limits are best-effort

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
        preexec_fn=_set_limits,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {effective_timeout}s",
        }

    return {
        "status": "ok" if proc.returncode == 0 else "error",
        "exit_code": proc.returncode,
        "stdout": stdout.decode(errors="replace")[:MAX_OUTPUT],
        "stderr": stderr.decode(errors="replace")[:MAX_OUTPUT],
    }
