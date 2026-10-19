#!/usr/bin/env python3
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "state" / "agent_next.db"
REGISTRY = BASE / "state" / "capability_registry.json"

WIDTH = 62


def db_rows(sql, params=()):
    try:
        con = sqlite3.connect(DB, timeout=1)
        con.row_factory = sqlite3.Row
        row = con.execute(sql, params).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None


def db_count(sql, params=()):
    try:
        con = sqlite3.connect(DB, timeout=1)
        value = con.execute(sql, params).fetchone()[0]
        con.close()
        return value
    except Exception:
        return 0


def port_ok(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def pid_ok(name):
    try:
        p = subprocess.run(
            ["pgrep", "-f", name],
            capture_output=True,
            text=True,
            timeout=1,
        )
        return p.returncode == 0 and bool(p.stdout.strip())
    except Exception:
        return False


def latest_task():
    return db_rows(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1"
    )


def latest_step(task_id):
    if not task_id:
        return None
    return db_rows(
        """SELECT * FROM task_steps
           WHERE task_id = ?
           ORDER BY step_no DESC, id DESC LIMIT 1""",
        (task_id,),
    )


def latest_worker():
    return db_rows(
        """SELECT * FROM workers
           ORDER BY COALESCE(heartbeat_at, started_at) DESC LIMIT 1"""
    )


def latest_mcp():
    return db_rows(
        "SELECT * FROM mcp_calls ORDER BY id DESC LIMIT 1"
    )


def pending_confirmation():
    return db_rows(
        """SELECT * FROM confirmations
           WHERE consumed_at IS NULL
           ORDER BY expires_at DESC LIMIT 1"""
    )


def latest_error():
    return db_rows(
        "SELECT * FROM errors ORDER BY id DESC LIMIT 1"
    )


def capability_count():
    try:
        import json
        data = json.loads(REGISTRY.read_text())
        tools = data.get("result", {}).get("tools", [])
        if isinstance(tools, list):
            return len(tools)
        return 0
    except Exception:
        return 0

def memory():
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        used = total - avail
        pct = int(used * 100 / total) if total else 0
        return pct, used * 1024, total * 1024
    except Exception:
        return 0, 0, 0


def disk():
    total, used, free = shutil.disk_usage(Path.home())
    pct = int(used * 100 / total) if total else 0
    return pct, used, total


def fmt_bytes(value):
    return f"{value / (1024 ** 3):.1f}G"


def bar(pct, width=18):
    filled = max(0, min(width, round(width * pct / 100)))
    return "#" * filled + "." * (width - filled)


def clean(value, limit=48):
    value = str(value if value is not None else "--")
    return value.replace("\n", " ")[:limit]


def lines():
    task = latest_task()
    step = latest_step(task["id"]) if task else None
    worker = latest_worker()
    mcp = latest_mcp()
    confirmation = pending_confirmation()
    error = latest_error()

    disk_pct, disk_used, disk_total = disk()
    ram_pct, ram_used, ram_total = memory()

    core = port_ok(8081)
    bridge = port_ok(18081)
    tunnel = pid_ok("cloudflared tunnel")

    status = task["status"] if task else "IDLE"
    stage = task["stage"] if task else "waiting"
    task_id = task["id"] if task else "IDLE"

    worker_status = worker["status"] if worker else "--"
    worker_id = worker["id"] if worker else "--"

    if confirmation:
        security = "CONFIRM REQUIRED"
    elif status in ("FAILED", "ERROR"):
        security = "ERROR"
    else:
        security = "READY"

    last_mcp = (
        f'{mcp["tool_name"]} / {mcp["status"] or "--"}'
        if mcp else "--"
    )

    error_text = clean(error["error"], 40) if error else "none"

    content = [
        f" STATUS: {security}",
        f" TASK:   {clean(task_id)}",
        f" STAGE:  {clean(stage)}",
        f" STEP:   {clean(step['name'] if step else '--')}",
        f" STEP STATUS: {clean(step['status'] if step else '--')}",
        f" WORKER: {clean(worker_id)}  {clean(worker_status)}",
        f" CORE     {'🟢' if core else '🔴'}  127.0.0.1:8081",
        f" BRIDGE   {'🟢' if bridge else '🔴'}  127.0.0.1:18081",
        f" TNNL     {'🟢' if tunnel else '🔴'}  cloudflared",
        f" MCP TOOLS: {capability_count()}",
        f" POLICY: {security}",
        f" CONFIRM: {'WAITING' if confirmation else '--'}",
        f" LAST MCP: {clean(last_mcp)}",
        f" ERROR: {error_text}",
        f" DISK [{bar(disk_pct)}] {disk_pct}%  {fmt_bytes(disk_used)}/{fmt_bytes(disk_total)}",
        f" RAM  [{bar(ram_pct)}] {ram_pct}%  {fmt_bytes(ram_used)}/{fmt_bytes(ram_total)}",
    ]

    def box_line(text):
        text = clean(text, WIDTH)
        return "|" + text.ljust(WIDTH) + "|"

    return [
        "+" + "=" * WIDTH + "+",
        box_line(" REELFORGE AGENT NEXT · LIVE"),
        "+" + "-" * WIDTH + "+",
        box_line(content[0]),
        box_line(content[1]),
        box_line(content[2]),
        box_line(content[3]),
        box_line(content[4]),
        box_line(content[5]),
        "+" + "-" * WIDTH + "+",
        box_line(content[6]),
        box_line(content[7]),
        box_line(content[8]),
        box_line(content[9]),
        "+" + "-" * WIDTH + "+",
        box_line(content[10]),
        box_line(content[11]),
        box_line(content[12]),
        box_line(content[13]),
        "+" + "-" * WIDTH + "+",
        box_line(content[14]),
        box_line(content[15]),
        "+" + "=" * WIDTH + "+",
    ]

def render():
    print("\033[2J\033[H\033[?25l", end="", flush=True)
    old = None

    try:
        while True:
            current = lines()

            if current != old:
                print("\033[H", end="")
                for line in current:
                    print(line + "\033[K")
                print(
                    f"\033[K UPDATED: "
                    f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                print("\033[H", end="", flush=True)
                old = current

            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\033[?25h\033[H", end="", flush=True)


def self_test():
    assert DB.exists(), f"missing database: {DB}"
    assert port_ok(8081), "MCP Core is not reachable"
    assert port_ok(18081), "Bridge is not reachable"
    assert capability_count() >= 1, "capability registry is empty"
    print("HUD_SELF_TEST_OK")
    print(f"DB={DB}")
    print(f"CORE={'OK' if port_ok(8081) else 'FAIL'}")
    print(f"BRIDGE={'OK' if port_ok(18081) else 'FAIL'}")
    print(f"TOOLS={capability_count()}")


if __name__ == "__main__":
    if "--self-test" in os.sys.argv:
        self_test()
    else:
        render()
