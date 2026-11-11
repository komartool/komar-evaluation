"""The debugger of the autonomous mode: the JDI server, the configuration of pi/debugger.ts and the
journal of a cell.

The JDI server (pi/debugger/JdiDapServer.java) is compiled once into <tools>/debugger (keyed by the
hash of its source). For a cell, pi/debugger.ts gets the scenario command of the case, a JDWP port of
its own and the command environment; it starts the scenario JVM itself (the agent never configures
the debugger) and writes every call and stop to debug-log.jsonl.

Ports: one per cell, from the range of paths.debug_ports (default 5100-5199), handed out by a lock
file in a place shared by every run of the machine, so that parallel cells and processes never share one.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from runner.config import ROOT, Config

SERVER_SRC = ROOT / "pi" / "debugger" / "JdiDapServer.java"
EXTENSION = ROOT / "pi" / "debugger.ts"
START_TIMEOUT = 300  # seconds from debug_start until the test JVM listens (build of the scenario included)
DEFAULT_PORTS = (5100, 5199)


def server_classes(cfg: Config) -> Path:
    """Directory with the compiled JDI server, rebuilt when the source changes."""
    digest = hashlib.sha256(SERVER_SRC.read_bytes()).hexdigest()[:16]
    out = cfg.tools_dir / "debugger" / digest
    if (out / "JdiDapServer.class").is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with (out.parent / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (out / "JdiDapServer.class").is_file():
            return out
        tmp = out.with_name(out.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        subprocess.run([str(cfg.java_home / "bin" / "javac"), "-Xlint:-options", "-d", str(tmp), str(SERVER_SRC)],
                       check=True, capture_output=True, text=True)
        os.replace(tmp, out)
    return out


def main(argv: list[str] | None = None) -> int:
    """Compile the JDI server for a config (scripts/setup.sh runs this):
    python3 -m runner.debugger --config config.yaml"""
    import argparse
    from runner.config import load_config
    ap = argparse.ArgumentParser(description="Compile the JDI server of the autonomous mode")
    ap.add_argument("--config", default="config.yaml")
    print(server_classes(load_config(ap.parse_args(argv).config)))
    return 0


def port_range(cfg: Config) -> tuple[int, int]:
    raw = (cfg.raw.get("paths") or {}).get("debug_ports")
    if not raw:
        return DEFAULT_PORTS
    lo, hi = (int(x) for x in str(raw).split("-"))
    return lo, hi


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@contextmanager
def lease_port(cfg: Config) -> Iterator[int]:
    """A JDWP port of the range that no other cell holds. The lock files live in one place for every run
    of the machine (KEVAL_PORT_LOCKS, default <temp dir>/keval-debug-ports), so runs with different
    work directories never share a port either."""
    lo, hi = port_range(cfg)
    import tempfile
    locks = Path(os.environ.get("KEVAL_PORT_LOCKS") or Path(tempfile.gettempdir()) / "keval-debug-ports")
    locks.mkdir(parents=True, exist_ok=True)
    for port in range(lo, hi + 1):
        handle = (locks / f"{port}.lock").open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            continue
        if not _free(port):
            handle.close()
            continue
        try:
            yield port
        finally:
            handle.close()
        return
    raise RuntimeError(f"no free JDWP port in {lo}-{hi}")


def write_config(cfg: Config, workspace: Path, agent_dir: Path, scenario: list[str], port: int,
                 target: dict[str, str]) -> Path:
    path = agent_dir / "debugger.json"
    path.write_text(json.dumps({
        "workspace": str(workspace), "java": str(cfg.java_home / "bin" / "java"),
        "server_classes": str(server_classes(cfg)), "scenario": scenario, "port": port,
        "command_env": sorted(cfg.command_env()), "start_timeout": START_TIMEOUT, "debug_runs": cfg.debug_runs,
        "scenario_log_dir": str(agent_dir), "target": {"class": target["class"], "method": target["method"]},
    }, indent=2))
    return path


def kill_leftovers(pids_file: Path) -> None:
    """Kill the scenario process groups pi/debugger.ts started (after a timeout of the agent)."""
    if not pids_file.is_file():
        return
    for line in pids_file.read_text().split():
        try:
            os.killpg(int(line), signal.SIGKILL)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            continue


# ---------------------------------------------------------------------------------------------
# the journal of a cell (artifacts/debug-log.jsonl)

def read_journal(path: Path) -> list[dict[str, Any]]:
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def journal_summary(rows: list[dict[str, Any]], run_limit: int | None = None) -> dict[str, Any]:
    """Counts of a cell: tool calls by tool, JVM runs (and the limit of runs, refused starts), stops
    (in the target method), plan revisions (breakpoints set or removed after the first stop of the
    cell), answer sizes."""
    calls: dict[str, int] = {}
    stops = stops_in_target = runs = failed_starts = refused_starts = revisions = answer_chars = 0
    first_stop_seen = False
    for r in rows:
        tool = r.get("tool")
        if not tool:
            continue
        calls[tool] = calls.get(tool, 0) + 1
        answer_chars += int(r.get("answer_chars") or 0)
        event = r.get("event") or {}
        kind = event.get("kind")
        if kind == "start":
            runs += 1
        elif kind == "start_failed":
            failed_starts += 1
        elif kind == "start_refused":
            refused_starts += 1
        elif kind == "stop":
            stops += 1
            stops_in_target += bool(event.get("in_target"))
            first_stop_seen = True
        if tool in ("set_breakpoint", "remove_breakpoint") and r.get("ok") and first_stop_seen:
            revisions += 1
    return {"calls": dict(sorted(calls.items())), "jvm_runs": runs, "run_limit": run_limit,
            "failed_starts": failed_starts, "refused_starts": refused_starts, "stops": stops, "stops_in_target": stops_in_target, "plan_revisions": revisions,
            "answer_chars": answer_chars}


def expressions(rows: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """(journal line, expression) of every inspect and breakpoint condition (for the escape check)."""
    out = []
    for i, r in enumerate(rows, start=1):
        args = r.get("args") or {}
        if r.get("tool") == "inspect" and isinstance(args.get("expr"), str):
            out.append((i, args["expr"]))
        if r.get("tool") == "set_breakpoint" and isinstance(args.get("condition"), str):
            out.append((i, args["condition"]))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
