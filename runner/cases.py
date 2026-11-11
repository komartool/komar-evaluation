"""Case manifests (cases/<id>/case.yaml) and the subject projects (projects.yaml)."""
from __future__ import annotations

import csv
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from runner.config import ROOT, Config


class CaseError(Exception):
    pass


@dataclass
class Project:
    name: str
    url: str
    commit: str
    jdk: int
    license: str
    eval_maven_args: tuple[str, ...] = ()
    # commands run once in a fresh workspace before the scenario (sibling modules a case needs installed)
    prebuild: tuple[tuple[str, ...], ...] = ()

    @property
    def checkout_name(self) -> str:
        """Directory name of the checkout: the repository name from the URL (e.g. mybatis-3)."""
        return self.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


@dataclass
class Case:
    id: str
    project: str
    group: str
    target: dict[str, str]
    scenario_test: str
    scenario_source: str
    output_test_file: str
    hidden_paths: list[str]
    commands: dict[str, list[str]]
    dir: Path


def _relative(value: Any, name: str, source: Path) -> str:
    p = Path(str(value or ""))
    if not str(value or "") or p.is_absolute() or ".." in p.parts:
        raise CaseError(f"{source}: {name} must be a relative path inside the project, got {value!r}")
    return p.as_posix()


def load_case(case_id: str, cases_dir: Path = ROOT / "cases") -> Case:
    source = cases_dir / case_id / "case.yaml"
    if not source.is_file():
        raise CaseError(f"case not found: {source}")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    commands = data.get("commands") or {}
    for name in ("build", "scenario", "generated_test"):
        argv = commands.get(name)
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise CaseError(f"{source}: command {name} must be a non-empty argv list")
    return Case(
        id=data["id"],
        project=data["project"],
        group=data["group"],
        target={k: str(data["target"][k]) for k in ("class", "method", "signature")},
        scenario_test=str(data["scenario"]["test"]),
        scenario_source=_relative(data["scenario"]["source"], "scenario.source", source),
        output_test_file=_relative(data["output_test_file"], "output_test_file", source),
        hidden_paths=[_relative(p, "hidden_paths", source) for p in data.get("hidden_paths") or []],
        commands={name: list(argv) for name, argv in commands.items()},
        dir=source.parent,
    )


def case_rows(cases_dir: Path = ROOT / "cases", case_list: str = "cases.csv") -> list[dict[str, str]]:
    """Rows of a case list in cases/ (config case_list)."""
    with (cases_dir / case_list).open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def config_case_rows(cfg: Config) -> list[dict[str, str]]:
    return case_rows(cfg.cases_dir, cfg.case_list)


def case_ids(cfg: Config, only: list[str] | None = None) -> list[str]:
    ids = [row["id"] for row in config_case_rows(cfg)]
    wanted = only or cfg.cases
    if wanted:
        unknown = sorted(set(wanted) - set(ids))
        if unknown:
            raise CaseError(f"unknown cases: {', '.join(unknown)}")
        ids = [c for c in ids if c in wanted]
    return ids


def load_projects(path: Path = ROOT / "projects.yaml") -> dict[str, Project]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {name: Project(name=name, url=v["url"], commit=v["commit"], jdk=int(v["jdk"]), license=v["license"],
                          eval_maven_args=tuple(str(a) for a in v.get("eval_maven_args") or ()),
                          prebuild=tuple(tuple(str(a) for a in argv) for argv in v.get("prebuild") or ()))
            for name, v in data.items()}


def java_major(java_home: Path) -> int | None:
    """Major version of the JDK at java_home (`java -version`), None if it does not run."""
    try:
        r = subprocess.run([str(java_home / "bin" / "java"), "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', r.stderr + r.stdout)
    if not m:
        return None
    return int(m.group(2)) if m.group(1) == "1" else int(m.group(1))


def check_jdk(cfg: Config, ids: list[str]) -> None:
    """The JDK of the run: paths.java_home is the JDK of `jdk` (21 by default), every project of the cases
    uses it, and JAVA_HOME of the runner's environment is the same JDK (the tracer runs with it)."""
    major = java_major(cfg.java_home)
    if major != cfg.jdk:
        raise CaseError(f"paths.java_home {cfg.java_home} is JDK {major}, the config says jdk: {cfg.jdk}")
    env_home = os.environ.get("JAVA_HOME")
    if env_home and Path(env_home).resolve() != cfg.java_home.resolve():
        raise CaseError(f"JAVA_HOME {env_home} differs from paths.java_home {cfg.java_home}: "
                        "the tracer runs on JAVA_HOME, set it to the same JDK")
    projects = load_projects()
    other = sorted({(c.project, projects[c.project].jdk) for c in (load_case(i, cfg.cases_dir) for i in ids)
                    if projects[c.project].jdk != cfg.jdk})
    if other:
        raise CaseError(f"the config runs JDK {cfg.jdk}, but " + ", ".join(f"{p} needs JDK {j}" for p, j in other)
                        + " (set `jdk` and paths.java_home of the config)")


def eval_command(case: Case, name: str) -> list[str]:
    """A case command as the evaluation runs it (verification, tracing): the
    project's eval_maven_args added after the executable. The agent gets case.commands unchanged."""
    argv = list(case.commands[name])
    extra = list(load_projects()[case.project].eval_maven_args)
    return argv[:1] + extra + argv[1:] if extra else argv


def project_checkout(cfg: Config, project: str) -> Path:
    return cfg.projects_dir / load_projects()[project].checkout_name
