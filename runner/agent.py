"""Run the Pi coding agent on a prepared workspace.

Pi starts with nothing but the two prompts and two extensions: pi/provider.ts (the model provider
from config.yaml) and pi/guard.ts (writes only to the generated test class, reads only inside the
workspace, bash only for the named case commands). The autonomous mode adds pi/debugger.ts (the five
debugger tools over the driving scenario); the komar mode adds the Komar commands to the guard and the
Komar wrapper to the PATH of the commands (runner/komar.py). No user settings, skills, prompt templates or context
files (AGENTS.md, CLAUDE.md) are loaded: Pi gets a fresh agent directory per cell.
"""
from __future__ import annotations

import json
import re
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner.config import ROOT, Config
from runner.process import run

PI_DIR = ROOT / "pi"
BUILTIN_TOOLS = ("read", "write", "edit", "bash", "grep", "find", "ls")
DEBUGGER_TOOLS = ("debug_start", "set_breakpoint", "remove_breakpoint", "resume", "inspect")
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")


class AgentError(Exception):
    pass


@dataclass
class AgentResult:
    exit_code: int | None
    timed_out: bool
    seconds: float
    events: list[dict]
    stderr: str
    denied: list[dict]
    commands: list[dict]
    output_versions: list[str]
    debug_log: list[dict] | None = None
    komar_log: list[dict] | None = None


def pi_binary(cfg: Config) -> Path:
    configured = (cfg.raw.get("paths") or {}).get("pi")
    candidates = [Path(os.path.expanduser(str(configured)))] if configured else []
    candidates.append(cfg.tools_dir / "pi" / "bin" / "pi")
    found = shutil.which("pi")
    if found:
        candidates.append(Path(found))
    for c in candidates:
        c = c if c.is_absolute() else ROOT / c
        if c.is_file() and os.access(c, os.X_OK):
            return c.resolve()
    raise AgentError("pi not found: run scripts/setup.sh or set paths.pi in the config")


def pi_version(cfg: Config) -> str | None:
    try:
        r = run([str(pi_binary(cfg)), "--version"], ROOT, agent_env(cfg, None), 60)
        return (r.stdout or r.stderr).strip().splitlines()[-1] or None
    except Exception:
        return None


def agent_env(cfg: Config, api_key_env: str | None) -> dict[str, str]:
    """Command environment plus the provider settings. The key travels as KEVAL_API_KEY to
    pi/provider.ts, which removes it from the process environment; the guard runs the agent's
    commands with the command environment only (cfg.command_env())."""
    env = cfg.command_env()
    node_bin = cfg.tools_dir / "node" / "bin"
    if node_bin.is_dir():
        env["PATH"] = f"{node_bin}{os.pathsep}{env['PATH']}"
    key = os.environ.get(api_key_env) if api_key_env else None
    for name in (cfg.api_key_env, cfg.fallback_api_key_env):
        if name:
            env.pop(name, None)
    if api_key_env is not None:
        if not key:
            raise AgentError(f"API key variable {api_key_env} is not set")
        env["KEVAL_API_KEY"] = key
    env["KEVAL_PROVIDER_NAME"] = cfg.provider_name
    env["KEVAL_BASE_URL"] = cfg.base_url
    env["KEVAL_MODELS"] = json.dumps(cfg.provider_models)
    return env


def effective_system_prompt(system: str, workspace: Path) -> str:
    """The system prompt as Pi sends it to the model. With --system-prompt and context files, skills
    and prompt templates disabled, Pi (pinned version) appends only the working directory
    (core/system-prompt.js, buildSystemPrompt)."""
    cwd = str(workspace).replace("\\", "/")
    return f"{system}\nCurrent working directory: {cwd}"


def pi_argv(cfg: Config, prompts: dict[str, str], session_dir: Path, debugger: bool = False) -> list[str]:
    if cfg.thinking is not None and cfg.thinking not in THINKING_LEVELS:
        raise AgentError(f"unsupported thinking level: {cfg.thinking}")
    extensions = ["-e", str(PI_DIR / "provider.ts"), "-e", str(PI_DIR / "guard.ts")]
    tools = BUILTIN_TOOLS
    if debugger:
        extensions += ["-e", str(PI_DIR / "debugger.ts")]
        tools = BUILTIN_TOOLS + DEBUGGER_TOOLS
    argv = [str(pi_binary(cfg)), "--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-context-files", "--no-themes", *extensions,
            "-t", ",".join(tools),
            "--provider", cfg.provider_name, "--model", cfg.model]
    if cfg.thinking is not None:
        argv += ["--thinking", str(cfg.thinking)]
    argv += ["--session-dir", str(session_dir), "--mode", "json", "--no-approve", "-p",
             "--system-prompt", prompts["system"], prompts["task"]]
    return argv


def _jsonl(path: Path) -> list[dict]:
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def parse_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    return events


def tool_calls(events: list[dict]) -> list[dict]:
    """Tool calls of the agent, once each (Pi repeats a toolCall block in several events)."""
    calls, seen = [], set()
    for event in events:
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue
            ident = block.get("id")
            if isinstance(ident, str) and ident:
                if ident in seen:
                    continue
                seen.add(ident)
            calls.append(block)
    return calls


def run_agent(cfg: Config, workspace: Path, output_test_file: str, commands: dict[str, list[str]],
              prompts: dict[str, str], agent_dir: Path, api_key_env: str,
              debugger: dict | None = None, komar: dict | None = None) -> AgentResult:
    """Run Pi with the cell's prompts; agent_dir holds Pi's own state and the guard logs.
    `debugger` (autonomous mode): {"scenario": argv, "target": case target} for pi/debugger.ts.
    `komar` (komar mode): {"scenario": argv, "compact": bool}, the command the agent runs under Komar and
    whether it reads only the compact view (always, in the komar mode)."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    session_dir = agent_dir / "session"
    session_dir.mkdir(exist_ok=True)
    guard_config = agent_dir / "guard.json"
    guard = {"workspace": str(workspace), "output_test_file": output_test_file,
             "commands": commands, "command_env": sorted(cfg.command_env())}
    if komar is not None:
        guard["komar"] = {"scenario": komar["scenario"], "compact": bool(komar.get("compact"))}
    guard_config.write_text(json.dumps(guard, indent=2))
    env = agent_env(cfg, api_key_env)
    env.update({
        "PI_CODING_AGENT_DIR": str(agent_dir / "pi-home"),
        "GUARD_CONFIG": str(guard_config),
        "GUARD_DENIED_LOG": str(agent_dir / "denied.jsonl"),
        "GUARD_COMMAND_LOG": str(agent_dir / "commands.jsonl"),
        "GUARD_OUTPUT_VERSION_LOG": str(agent_dir / "output-versions.jsonl"),
    })
    debug_log = komar_log = None
    if komar is not None:
        from runner import komar as K
        bin_dir = K.prepare(cfg, workspace, agent_dir, komar["scenario"], bool(komar.get("compact")))
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        try:
            result = run(pi_argv(cfg, prompts, session_dir), workspace, env, cfg.timeout, "pi")
        finally:
            K.kill_leftovers(agent_dir / "komar-pids.txt")
        komar_log = K.read_log(agent_dir / "komar-log.jsonl")
    elif debugger is None:
        result = run(pi_argv(cfg, prompts, session_dir), workspace, env, cfg.timeout, "pi")
    else:
        from runner import debugger as D
        with D.lease_port(cfg) as port:
            config = D.write_config(cfg, workspace, agent_dir, debugger["scenario"], port, debugger["target"])
            env.update({"DEBUGGER_CONFIG": str(config), "DEBUGGER_LOG": str(agent_dir / "debug-log.jsonl"),
                        "DEBUGGER_PIDS": str(agent_dir / "debug-pids.txt")})
            try:
                result = run(pi_argv(cfg, prompts, session_dir, debugger=True), workspace, env, cfg.timeout, "pi")
            finally:
                D.kill_leftovers(agent_dir / "debug-pids.txt")
        debug_log = D.read_journal(agent_dir / "debug-log.jsonl")
    return AgentResult(
        exit_code=result.exit_code, timed_out=result.timed_out, seconds=result.seconds,
        events=parse_events(result.stdout), stderr=result.stderr,
        denied=_jsonl(agent_dir / "denied.jsonl"), commands=_jsonl(agent_dir / "commands.jsonl"),
        output_versions=[r["content"] for r in _jsonl(agent_dir / "output-versions.jsonl")
                         if isinstance(r.get("content"), str)],
        debug_log=debug_log, komar_log=komar_log,
    )


# ---------------------------------------------------------------------------------------------
# reading a finished session: cost, gateway errors

GATEWAY_ERROR_MARKERS = ("upstream_empty_response", "overloaded", "rate limit", "ECONNRESET", "socket hang up",
                         "fetch failed", "Bad Gateway", "Service Unavailable", "Gateway Timeout",
                         "Request timed out", "Connection error", "connection reset", "ETIMEDOUT",
                         "ECONNREFUSED", "Internal Server Error")
GATEWAY_STATUS_PREFIXES = ("429", "500", "502", "503", "504")
# A 4xx answer of the gateway other than 429 (e.g. "400 invalid_request_error" for a model the gateway does
# not serve) is a provider failure only while the agent has done nothing: a 4xx after tool calls may come
# from the session itself (e.g. a context that grew too long), so it stays a failure of the cell.
CLIENT_ERROR = re.compile(r"^4\d\d\b")
AUTH_ERROR = re.compile(r"^(401|403)\b")


def _assistant_messages(events: list[dict]):
    for event in events:
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") == "assistant":
            yield message


def usage(events: list[dict]) -> dict[str, Any]:
    totals: dict[str, Any] = {"turns": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                              "cache_write_tokens": 0, "cost_usd": 0.0}
    for message in _assistant_messages(events):
        u = message.get("usage") or {}
        totals["turns"] += 1
        totals["input_tokens"] += u.get("input", 0) or 0
        totals["output_tokens"] += u.get("output", 0) or 0
        totals["cache_read_tokens"] += u.get("cacheRead", 0) or 0
        totals["cache_write_tokens"] += u.get("cacheWrite", 0) or 0
        totals["cost_usd"] += (u.get("cost") or {}).get("total") or 0
    return totals


def is_gateway_error(text: str) -> bool:
    text = text.strip()
    return text.startswith(GATEWAY_STATUS_PREFIXES) or any(m in text for m in GATEWAY_ERROR_MARKERS)


def client_errors(events: list[dict]) -> tuple[int, bool, bool]:
    """(model calls that ended with an HTTP 4xx other than 429, whether the last one did, whether any was
    an authentication or authorisation error, 401/403)."""
    count, last, auth = 0, False, False
    for message in _assistant_messages(events):
        text = str(message.get("errorMessage") or "").strip()
        last = message.get("stopReason") == "error" and bool(CLIENT_ERROR.match(text)) and not is_gateway_error(text)
        count += last
        auth = auth or (last and bool(AUTH_ERROR.match(text)))
    return count, last, auth


def gateway_errors(events: list[dict]) -> tuple[int, bool]:
    """(number of model calls that ended with a gateway error, whether the last one did)."""
    count, last = 0, False
    for message in _assistant_messages(events):
        last = message.get("stopReason") == "error" and is_gateway_error(str(message.get("errorMessage") or ""))
        count += last
    return count, last
