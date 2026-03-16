"""Colony diagnostics — single script replacing all ad-hoc check/test scripts.

Usage:
    python diag.py              # overview: services + cells
    python diag.py services     # service status only
    python diag.py cells        # cell status only
    python diag.py logs [name]  # service logs (all, or one by name)
    python diag.py test         # HTTP smoke-test every running service
    python diag.py board        # show colony bulletin board
"""

import sys
import os
import json
import httpx

SECRET = os.getenv("LIFE_API_SECRET", "")
GUARDIAN = os.getenv("GUARDIAN_URL", "http://localhost:8002")
CELL_RT = os.getenv("CELL_RUNTIME_URL", "http://localhost:8004")
HEADERS = {"X-Life-Secret": SECRET} if SECRET else {}
TIMEOUT = 5


def _get(url):
    return httpx.get(url, headers=HEADERS, timeout=TIMEOUT)


def _post(url, payload):
    return httpx.post(url, json=payload, headers=HEADERS, timeout=TIMEOUT)


# ── Services ───────────────────────────────────────────────────────────
def cmd_services():
    r = _get(f"{GUARDIAN}/wiseoldant/services")
    svcs = r.json()
    running = [s for s in svcs if s["running"]]
    crashed = [s for s in svcs if not s["running"]]
    print(f"Services: {len(running)} running, {len(crashed)} crashed, {len(svcs)} total\n")
    for svc in sorted(svcs, key=lambda s: s["name"]):
        tag = "\033[32mRUNNING\033[0m" if svc["running"] else "\033[31mCRASHED\033[0m"
        desc = svc.get("description", "")
        print(f"  {tag}  {svc['name']:30s} port={svc['port']}")
        if desc:
            print(f"         {desc[:80]}")
        if not svc["running"]:
            try:
                r2 = _post(f"{GUARDIAN}/wiseoldant/logs", {"name": svc["name"]})
                for line in r2.json().get("lines", [])[-5:]:
                    print(f"         | {line}")
            except Exception:
                pass
    print()


# ── Cells ──────────────────────────────────────────────────────────────
def cmd_cells():
    r = _get(f"{CELL_RT}/cells")
    cells = r.json()
    print(f"Cells: {len(cells)} active\n")
    for cid, info in sorted(cells.items()):
        h = info.get("health", 0)
        cyc = info.get("cycle", 0)
        proj = info.get("project", "")
        st = info.get("status", "?")
        print(f"  {cid:30s} health={h:3d}  cycle={cyc:4d}  status={st:8s}  project={proj}")
    print()


# ── Logs ───────────────────────────────────────────────────────────────
def cmd_logs(name=None):
    if name:
        r = _post(f"{GUARDIAN}/wiseoldant/logs", {"name": name})
        data = r.json()
        print(f"=== {name} ===")
        for line in data.get("lines", [])[-30:]:
            print(f"  {line}")
        return

    r = _get(f"{GUARDIAN}/wiseoldant/services")
    for svc in r.json():
        r2 = _post(f"{GUARDIAN}/wiseoldant/logs", {"name": svc["name"]})
        lines = r2.json().get("lines", [])
        tag = "running" if svc["running"] else "CRASHED"
        print(f"=== {svc['name']} ({tag}, port={svc['port']}) ===")
        for line in lines[-10:]:
            print(f"  {line}")
        print()


# ── Smoke-test ─────────────────────────────────────────────────────────
def cmd_test():
    r = _get(f"{GUARDIAN}/wiseoldant/services")
    for svc in r.json():
        if not svc["running"]:
            continue
        port = svc["port"]
        url = f"http://localhost:{port}/"
        try:
            resp = httpx.get(url, timeout=3, follow_redirects=True)
            print(f"  \033[32mOK\033[0m   {svc['name']:30s} :{port}  -> {resp.status_code}")
        except Exception as e:
            print(f"  \033[31mFAIL\033[0m {svc['name']:30s} :{port}  -> {e}")


# ── Board ──────────────────────────────────────────────────────────────
def cmd_board():
    r = _post(f"{GUARDIAN}/queen/read", {"path": "/data/workspace/.colony_board"})
    data = r.json()
    content = data.get("content", "")
    if not content.strip():
        print("(board is empty)")
    else:
        # Show last 2000 chars
        print(content[-2000:])


# ── Main ───────────────────────────────────────────────────────────────
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "overview"

    try:
        if cmd == "overview":
            cmd_services()
            cmd_cells()
        elif cmd == "services":
            cmd_services()
        elif cmd == "cells":
            cmd_cells()
        elif cmd == "logs":
            cmd_logs(sys.argv[2] if len(sys.argv) > 2 else None)
        elif cmd == "test":
            cmd_test()
        elif cmd == "board":
            cmd_board()
        else:
            print(__doc__)
    except httpx.ConnectError as e:
        print(f"Connection failed: {e}\nAre the services running? (docker compose up)")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
