"""Clone the subject projects at their commits and build them, so that case commands run offline.

For every project of the config's case list (cases.csv by default, config case_list): clone into <projects>/<repository name> (or reuse an existing
checkout of the same commit), then run, online, the distinct `build` and `scenario` commands of the
project's cases (Maven fills the local repository with everything the offline commands need).
A project whose commands all succeeded at this commit is not rebuilt: a marker file next to the
checkout records the commit and the commands (--rebuild ignores it).

    python3 scripts/prepare_projects.py --config config.yaml [--projects jsoup ...] [--skip-build] [--rebuild]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.cases import config_case_rows, load_case, load_projects  # noqa: E402
from runner.config import load_config  # noqa: E402
from runner.process import resolve_argv, run  # noqa: E402

ONLINE_DROP = ("-o", "--offline", "-q")


def checkout(url: str, commit: str, dest: Path) -> None:
    if (dest / ".git").exists():
        head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        if head == commit:
            return
        subprocess.run(["git", "-C", str(dest), "fetch", "--quiet", "origin", commit], check=False)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", url, str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", commit], check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--projects", nargs="*")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="build even if the marker says it is done")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    env = cfg.command_env()
    failed = 0
    rows = config_case_rows(cfg)
    used = {r["project"] for r in rows}
    for name, project in load_projects().items():
        if name not in used or (a.projects and name not in a.projects):
            continue
        dest = cfg.projects_dir / project.checkout_name
        checkout(project.url, project.commit, dest)
        print(f"{name}: {dest} at {project.commit[:12]}", flush=True)
        if a.skip_build:
            continue
        plan: list[tuple[str, str, list[str]]] = [
            ("-", "prebuild", [x for x in argv if x not in ONLINE_DROP]) for argv in project.prebuild]
        seen: set[tuple[str, ...]] = set()
        for row in rows:
            if row["project"] != name:
                continue
            case = load_case(row["id"], cfg.cases_dir)
            for kind in ("build", "scenario"):
                argv_ = [x for x in case.commands[kind] if x not in ONLINE_DROP]
                key = tuple(x for x in argv_ if not (kind == "scenario" and x.startswith("-Dtest=")))
                if key in seen:
                    continue
                seen.add(key)
                plan.append((row["id"], kind, argv_))
        marker = dest.parent / f".{dest.name}.prepared.json"
        digest = hashlib.sha256(json.dumps([project.commit, [p[2] for p in plan]]).encode()).hexdigest()
        if not a.rebuild and marker.is_file() and json.loads(marker.read_text()).get("sha256") == digest:
            print(f"  already built at this commit with these commands ({marker.name})", flush=True)
            continue
        marker.unlink(missing_ok=True)
        project_failed = 0
        for case_id, kind, argv_ in plan:
            started = time.time()
            r = run(resolve_argv(argv_, dest), dest, env, 7200, kind)
            print(f"  {case_id} {kind}: exit {r.exit_code} in {time.time() - started:.0f}s", flush=True)
            if not r.ok:
                project_failed += 1
                print("\n".join((r.stdout + r.stderr).splitlines()[-25:]), flush=True)
        failed += project_failed
        if not project_failed:
            marker.write_text(json.dumps({"commit": project.commit, "sha256": digest}) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
