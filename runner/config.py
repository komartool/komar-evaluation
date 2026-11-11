"""Experiment configuration (config.yaml) and the environment of build and test commands."""
from __future__ import annotations

import atexit
import hashlib
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
# baseline: no runtime data; autonomous: the interactive debugger of pi/debugger.ts; komar: the scenario
# under Komar, read through its compact view (runner/komar.py)
AGENT_MODES = ("baseline", "autonomous", "komar")
MODES = AGENT_MODES
AGENT_COMMANDS = ("build", "scenario", "generated_test")
# verify: the generated test compiles and passes; b: level B against the reference (evaluation/compare.py)
EVAL_STEPS = ("verify", "b")
# settings that may be overridden from the environment (load_config); the command line of scripts/run.py
# overrides both
ENV_OVERRIDES = {"model": "KEVAL_MODEL", "thinking": "KEVAL_THINKING", "base_url": "KEVAL_BASE_URL",
                 "timeout": "KEVAL_TIMEOUT", "rounds": "KEVAL_ROUNDS", "parallel_cases": "KEVAL_PARALLEL_CASES",
                 "komar_commit": "KOMAR_COMMIT"}

# Environment of build and test commands: these variables of the runner's environment are passed
# through, nothing else (no API keys, no credentials). config.yaml `env` adds or overrides variables.
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "LANG", "LANGUAGE", "TMPDIR",
                 "MAVEN_OPTS", "MAVEN_ARGS", "MAVEN_HOME", "M2_HOME", "MAVEN_USER_HOME", "GRADLE_USER_HOME",
                 "GRADLE_OPTS")
ENV_ALLOWLIST_PREFIXES = ("LC_",)


class ConfigError(Exception):
    pass


def neutral_name(*parts: str) -> str:
    """Directory name that does not reveal the case, mode or run (w + 12 hex digits)."""
    return "w" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:12]


def _path(value: Any, default: str | None = None) -> Path | None:
    value = value if value not in (None, "") else default
    if value in (None, ""):
        return None
    p = Path(os.path.expandvars(str(value))).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


@dataclass
class Config:
    path: Path
    raw: dict[str, Any]
    provider_name: str
    base_url: str
    api_key_env: str
    fallback_api_key_env: str | None
    provider_models: list[Any]
    model: str
    thinking: str | None
    timeout: int
    command_timeout: int
    modes: list[str]
    rounds: list[int]
    run_id_format: str
    cases: list[str]
    parallel_cases: int
    min_free_disk_gb: float
    min_free_mem_gb: float
    projects_dir: Path
    results_dir: Path
    tools_dir: Path
    java_home: Path
    maven_repository: Path
    eval_steps: list[str]
    reference_runs: int
    trace_depth: int
    work_root: Path | None = None
    # cases/<case_list>: the case set of the run (cases.csv: JDK 21 projects; cases-jdk11.csv: JDK 11 projects)
    case_list: str = "cases.csv"
    # major version of the JDK at paths.java_home; every project of the case set must use it
    jdk: int = 21
    # autonomous and komar modes: how many times the agent may run the scenario under the debugger or
    # under Komar in a cell
    debug_runs: int = 3
    # komar mode: the installed Komar (scripts/setup_komar.sh) and the commit of komartool/komar it is built from
    komar_dir: Path = Path()
    komar_commit: str = ""
    # receiver fields labelled DEPENDENCY per case (evaluation/dependency-labels.json; level B)
    dependency_labels: Path = Path()
    references_root: Path | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    _work_dir: Path | None = field(default=None, repr=False)

    @property
    def cases_dir(self) -> Path:
        return ROOT / "cases"

    @property
    def prompts_dir(self) -> Path:
        return ROOT / "prompts"

    @property
    def references_dir(self) -> Path:
        """Reference traces of the cases: paths.references (default: references/ of the repository)."""
        return self.references_root or ROOT / "references"

    @property
    def cells_dir(self) -> Path:
        return self.results_dir / "cells"

    @property
    def work_dir(self) -> Path:
        """Scratch space for workspaces and traces, outside the results tree and the repository:
        paths.work of the config, or a fresh directory in the system temp directory (removed at exit).
        A test run there cannot reach the results through a fixed relative path."""
        with _WORK_LOCK:
            return self._make_work_dir()

    def _make_work_dir(self) -> Path:
        if self._work_dir is None:
            if self.work_root is not None:
                self.work_root.mkdir(parents=True, exist_ok=True)
                self._work_dir = Path(tempfile.mkdtemp(prefix="tg-", dir=self.work_root))
            else:
                self._work_dir = Path(tempfile.mkdtemp(prefix="tg-"))
            atexit.register(shutil.rmtree, self._work_dir, True)
        return self._work_dir

    def run_id(self, round_number: int) -> str:
        return self.run_id_format.format(round=round_number)

    def cell_dir(self, case_id: str, mode: str, run_id: str) -> Path:
        return self.cells_dir / case_id / mode / run_id

    def command_env(self) -> dict[str, str]:
        """Environment of build/test commands: the allowlisted variables of the runner's environment,
        `env` from the config, JDK first on PATH."""
        env = {k: v for k, v in os.environ.items()
               if k in ENV_ALLOWLIST or k.startswith(ENV_ALLOWLIST_PREFIXES)}
        env.update(self.extra_env)
        env["JAVA_HOME"] = str(self.java_home)
        env["PATH"] = f"{self.java_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        return env


_WORK_LOCK = threading.Lock()


DEFAULT_KOMAR_COMMIT = "665ede625b388aae6f4d3ef321a12450eb95e266"


def configure_tools(cfg: "Config") -> None:
    """Point the evaluation modules at the tools directory of the config."""
    from evaluation import trace
    trace.configure(cfg.tools_dir)


def apply_overrides(raw: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """The config with the settings of ENV_OVERRIDES taken from the environment, then from `overrides`
    (the command line; None values are ignored)."""
    raw = dict(raw)
    provider = dict(raw.get("provider") or {})
    values: dict[str, Any] = {k: os.environ[v] for k, v in ENV_OVERRIDES.items() if os.environ.get(v)}
    values.update({k: v for k, v in (overrides or {}).items() if v is not None})
    for key, value in values.items():
        if key == "base_url":
            provider["base_url"] = value
        elif key == "rounds":
            raw["rounds"] = [int(r) for r in str(value).replace(",", " ").split()] if isinstance(value, str) else list(value)
        else:
            raw[key] = value
    raw["provider"] = provider
    return raw


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"config not found: {path}")
    raw = apply_overrides(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, overrides)
    provider = raw.get("provider") or {}
    paths = raw.get("paths") or {}
    evaluation = raw.get("evaluation") or {}
    java_home = _path(paths.get("java_home")) or (Path(os.environ["JAVA_HOME"]) if os.environ.get("JAVA_HOME") else None)
    if java_home is None:
        raise ConfigError("set paths.java_home in the config or JAVA_HOME in the environment (the JDK of `jdk`, 21 by default)")
    modes = list(AGENT_MODES if raw.get("modes") is None else raw["modes"])
    unknown = sorted(set(modes) - set(MODES))
    if unknown:
        raise ConfigError(f"unknown modes: {', '.join(unknown)}")
    steps = list(evaluation.get("steps") or EVAL_STEPS)
    if set(steps) - set(EVAL_STEPS):
        raise ConfigError(f"unknown evaluation steps: {', '.join(sorted(set(steps) - set(EVAL_STEPS)))}")
    for key in ("base_url", "api_key_env"):
        if not provider.get(key):
            raise ConfigError(f"provider.{key} is required")
    if not raw.get("model"):
        raise ConfigError("model is required")
    cfg = Config(
        path=path,
        raw=raw,
        provider_name=str(provider.get("name") or "custom"),
        base_url=str(provider["base_url"]).rstrip("/"),
        api_key_env=str(provider["api_key_env"]),
        fallback_api_key_env=provider.get("fallback_api_key_env") or None,
        provider_models=list(provider.get("models") or []),
        model=str(raw["model"]),
        thinking=raw.get("thinking"),
        timeout=int(raw.get("timeout", 600)),
        command_timeout=int(raw.get("command_timeout", 900)),
        modes=modes,
        rounds=[int(r) for r in (raw.get("rounds") or [1])],
        run_id_format=str(raw.get("run_id_format") or "r{round}"),
        cases=list(raw.get("cases") or []),
        parallel_cases=max(1, int(raw.get("parallel_cases", 1))),
        min_free_disk_gb=float(raw.get("min_free_disk_gb", 15)),
        min_free_mem_gb=float(raw.get("min_free_mem_gb", 0)),
        projects_dir=_path(paths.get("projects"), "../projects"),
        results_dir=_path(paths.get("results"), "../results"),
        tools_dir=_path(paths.get("tools"), ".tools"),
        java_home=java_home,
        maven_repository=_path(paths.get("maven_repository"), "~/.m2/repository"),
        eval_steps=steps,
        reference_runs=int(evaluation.get("reference_runs", 2)),
        trace_depth=int(evaluation.get("trace_depth", 5)),
        work_root=_path(paths.get("work")),
        extra_env={str(k): os.path.expandvars(str(v)) for k, v in (raw.get("env") or {}).items()},
        case_list=str(raw.get("case_list") or "cases.csv"),
        jdk=int(raw.get("jdk", 21)),
        debug_runs=int(raw.get("debug_runs", 3)),
    )
    cfg.komar_dir = _path(paths.get("komar")) or cfg.tools_dir / "komar"
    cfg.references_root = _path(paths.get("references"))
    cfg.komar_commit = str(raw.get("komar_commit") or DEFAULT_KOMAR_COMMIT)
    cfg.dependency_labels = (_path(evaluation.get("dependency_labels"))
                             or ROOT / "evaluation" / "dependency-labels.json")
    if not re.fullmatch(r"[0-9a-f]{40}", cfg.komar_commit):
        raise ConfigError(f"komar_commit must be a full commit hash, got {cfg.komar_commit!r}")
    if cfg.debug_runs < 1:
        raise ConfigError(f"debug_runs must be at least 1, got {cfg.debug_runs}")
    if Path(cfg.case_list).name != cfg.case_list or not cfg.case_list.endswith(".csv"):
        raise ConfigError(f"case_list must be a .csv file name in cases/, got {cfg.case_list!r}")
    if cfg.work_root is not None:
        for inside in (cfg.results_dir, ROOT):
            if cfg.work_root == inside or inside in cfg.work_root.parents:
                raise ConfigError(f"paths.work must be outside {inside}: {cfg.work_root}")
    configure_tools(cfg)
    return cfg
