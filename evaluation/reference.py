"""Reference traces of the driving scenarios, recorded twice (nondeterminism audit).

Output per case: <results>/references/<case>/run1.json, run2.json and audit.json (distinct scenarios
per run, paths that differ between the two runs, masked values, depth cuts, run1 vs run2 on every
level). Metrics compare candidate traces with run1.

A reference is valid only if every run exits with 0, records at least one invocation of the target,
and the runs agree on every level (S, A, B, C) after normalisation. An invalid reference is not kept:
the case fails with the reason (audit-failed.json keeps the audit, failed-run<i>.json the traces, for
inspection) and is recorded
again next time.

    python3 -m evaluation.reference --config config.yaml [--cases ID ...] [--force]
"""
from __future__ import annotations

import argparse
import json
import shutil
from typing import Any

from evaluation import compare as C
from evaluation import trace as T
from runner.cases import case_ids, load_case, project_checkout
from runner.config import Config, load_config, neutral_name

LEVELS = ("structure", "io", "state", "strict")


class ReferenceError(Exception):
    pass


def reference_exists(cfg: Config, case_id: str) -> bool:
    return (cfg.references_dir / case_id / "audit.json").is_file()


def problems(audit: dict[str, Any], runs: int) -> list[str]:
    """Reasons why a reference is not valid; empty when it is."""
    out = []
    if len(audit["runs"]) < runs:
        out.append(f"{len(audit['runs'])} of {runs} runs recorded")
    for i, r in enumerate(audit["runs"], start=1):
        if r["exit_code"] != 0:
            out.append(f"run {i}: scenario exit code {r['exit_code']}")
        if r["invocations"] == 0:
            out.append(f"run {i}: no invocation of the target recorded")
    self_cmp = audit.get("run1_vs_run2")
    if runs >= 2 and self_cmp:
        for level in LEVELS:
            for kind in ("recall", "precision"):
                v = self_cmp.get(f"{kind}_{level}")
                if v is not None and v < 1.0:
                    out.append(f"run 1 vs run 2: {kind} {level} = {v:.3f}")
    return out


def audit_traces(case, traces: list[dict[str, Any]], depth: int) -> dict[str, Any]:
    nondet = C.load_nondet()
    audit: dict[str, Any] = {"case_id": case.id, "project": case.project, "depth": depth, "runs": []}
    for t in traces:
        norm = C.Normaliser(nondet, case.project)
        sc = C.scenarios(t["invocations"], norm)
        audit["runs"].append({"exit_code": t["run"]["exit_code"], "seconds": t["run"]["seconds"],
                              "invocations": len(t["invocations"]), "scenarios": len(sc), "jvms": t.get("jvms"),
                              "masked": norm.masked, "cuts": norm.cuts, "errors": t["errors"][:5]})
    if len(traces) >= 2:
        a, b = traces[0]["invocations"], traces[1]["invocations"]
        diffs = C.unstable_between_runs(a, b, C.Normaliser(nondet, case.project))
        audit["unstable_paths"] = sorted(diffs.items(), key=lambda kv: -kv[1])[:50]
        audit["run1_vs_run2"] = C.compare(a, b, nondet, case.project)
    return audit


def record(cfg: Config, case_id: str, force: bool = False, reaudit: bool = False) -> dict[str, Any]:
    """Record (or re-audit) the reference of a case; raises ReferenceError when it is not valid."""
    out = cfg.references_dir / case_id
    audit_file = out / "audit.json"
    if audit_file.is_file() and not force and not reaudit:
        return json.loads(audit_file.read_text())
    case = load_case(case_id, cfg.cases_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs = cfg.reference_runs
    if reaudit:
        traces = [json.loads((out / f"run{i}.json").read_text()) for i in range(1, runs + 1)
                  if (out / f"run{i}.json").is_file()]
    else:
        traces = []
        for i in range(1, runs + 1):
            ws = T.clone_checkout(project_checkout(cfg, case.project),
                                  cfg.work_dir / "ws" / neutral_name(case_id, "reference", str(i)))
            trace_dir = cfg.work_dir / "out" / neutral_name(case_id, "reference", str(i))
            try:
                T.remove_paths(ws, case.hidden_paths)
                t = T.trace_case_command(case, ws, "scenario", trace_dir, cfg.command_env(),
                                         cfg.command_timeout, cfg.trace_depth)
            finally:
                shutil.rmtree(ws, ignore_errors=True)
                shutil.rmtree(trace_dir, ignore_errors=True)
            traces.append(t)
    audit = audit_traces(case, traces, cfg.trace_depth)
    bad = problems(audit, runs)
    if bad:
        audit_file.unlink(missing_ok=True)
        for i in range(1, runs + 1):
            (out / f"run{i}.json").unlink(missing_ok=True)
        # the traces of a rejected reference are kept for inspection under other names, so that
        # nothing takes them for a valid reference
        for i, t in enumerate(traces, start=1):
            if not reaudit:
                (out / f"failed-run{i}.json").write_text(json.dumps(t))
        (out / "audit-failed.json").write_text(json.dumps({**audit, "problems": bad}, indent=1))
        raise ReferenceError(f"{case_id}: invalid reference: " + "; ".join(bad))
    for i, t in enumerate(traces, start=1):
        if not reaudit:
            (out / f"run{i}.json").write_text(json.dumps(t))
    (out / "audit-failed.json").unlink(missing_ok=True)
    for i in range(1, runs + 1):
        (out / f"failed-run{i}.json").unlink(missing_ok=True)
    audit_file.write_text(json.dumps(audit, indent=1))
    return audit


def load_reference(cfg: Config, case_id: str) -> list[dict[str, Any]]:
    if not reference_exists(cfg, case_id):
        raise FileNotFoundError(f"no valid reference trace for {case_id}; run evaluation.reference")
    return json.loads((cfg.references_dir / case_id / "run1.json").read_text())["invocations"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reaudit", action="store_true",
                    help="recompute audit.json from saved traces (after editing nondeterminism.json)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    failed = 0
    for cid in case_ids(cfg, a.cases):
        try:
            au = record(cfg, cid, a.force, a.reaudit)
            r = au["runs"][0]
            s = au.get("run1_vs_run2", {})
            print(f"{cid:62} rc={r['exit_code']} inv={r['invocations']} sc={r['scenarios']} cuts={r['cuts']} "
                  f"self S/A/B/C={s.get('recall_structure')}/{s.get('recall_io')}/{s.get('recall_state')}/"
                  f"{s.get('recall_strict')} unstable={len(au.get('unstable_paths', []))}", flush=True)
        except Exception as e:  # keep going
            failed += 1
            print(f"{cid:62} ERROR {type(e).__name__}: {e}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
