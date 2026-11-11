"""Subprocesses in their own process group, killed as a whole on timeout."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandResult:
    name: str
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_dict(self) -> dict:
        return {"name": self.name, "argv": self.argv, "exit_code": self.exit_code,
                "timed_out": self.timed_out, "seconds": round(self.seconds, 1)}


def _kill_group(process: subprocess.Popen, grace: float = 3.0) -> None:
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        pgid = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                process.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        deadline = time.time() + grace
        while time.time() < deadline and process.poll() is None:
            time.sleep(0.05)


def run(argv: list[str], cwd: Path, env: dict[str, str], timeout: float, name: str = "") -> CommandResult:
    started = time.time()
    process = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = "", ""
    else:
        _kill_group(process, grace=0.5)  # stray children of the command (forked JVMs)
    return CommandResult(name=name, argv=list(argv), exit_code=None if timed_out else process.returncode,
                         stdout=stdout or "", stderr=stderr or "", timed_out=timed_out,
                         seconds=time.time() - started)


def resolve_argv(argv: list[str], cwd: Path) -> list[str]:
    """Run a project-local wrapper (./mvnw) by absolute path."""
    argv = list(argv)
    if argv and argv[0].startswith("./") and (cwd / argv[0]).is_file():
        argv[0] = str(cwd / argv[0])
    return argv
