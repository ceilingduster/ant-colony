"""Queen — Filesystem Guardian.

Provides sandboxed file operations limited to approved writable roots.
"""

import os
import shutil
from pathlib import Path

WRITABLE_ROOTS = [
    Path(os.getenv("WORKSPACE_DIR", "/data/workspace")),
    Path(os.getenv("MEMORY_DIR", "/data/memory")),
]


def _resolve_safe(raw_path: str) -> Path:
    """Resolve a path and ensure it falls within an allowed root."""
    p = Path(raw_path).resolve()
    for root in WRITABLE_ROOTS:
        root = root.resolve()
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise PermissionError(f"Path outside writable roots: {raw_path}")


def _resolve_readable(raw_path: str) -> Path:
    """Resolve a path for read — allowed anywhere under /data."""
    p = Path(raw_path).resolve()
    data_root = Path("/data").resolve()
    try:
        p.relative_to(data_root)
    except ValueError:
        raise PermissionError(f"Read path outside /data: {raw_path}")
    return p


# ---- Public API ----------------------------------------------------------


def read_file(path: str) -> str:
    p = _resolve_readable(path)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    return p.read_text(encoding="utf-8")


def write_file(path: str, content: str) -> dict:
    p = _resolve_safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"status": "ok", "path": str(p), "bytes": len(content)}


def append_file(path: str, content: str) -> dict:
    p = _resolve_safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(content)
    return {"status": "ok", "path": str(p)}


def delete_file(path: str) -> dict:
    p = _resolve_safe(path)
    if p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)
    else:
        raise FileNotFoundError(f"Not found: {path}")
    return {"status": "deleted", "path": str(p)}


def create_directory(path: str) -> dict:
    p = _resolve_safe(path)
    p.mkdir(parents=True, exist_ok=True)
    return {"status": "ok", "path": str(p)}


def list_directory(path: str) -> list[dict]:
    p = _resolve_readable(path)
    if not p.is_dir():
        raise FileNotFoundError(f"Directory not found: {path}")
    entries = []
    for child in sorted(p.iterdir()):
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "size": child.stat().st_size if child.is_file() else None,
        })
    return entries
