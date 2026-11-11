"""Evaluate one generation cell: verify (compiles, passes) and level B (trace vs reference).

Input: <results>/cells/<case>/<mode>/<run-id>/ (summary.json, result.diff) written by the runner.
Output: eval.json in the same directory, plus eval-artifacts/ (candidate trace, verification logs), so
that level B can be recomputed without rerunning the tests.

Every step starts from a fresh copy of the compiled project checkout with the hidden tests removed
and the cell's result.diff applied: no execution input, no files of the agent run. Build and test
commands run as `evaluation commands` (runner.cases.eval_command): the same for every mode.

Every step records the inputs it depends on (inputs.py); a stored step whose inputs changed is not
reused (an error asks for --force). A step that failed for a transient reason (a timeout, a tool
error) is not stored and runs again next time.

Level B can be recomputed from the candidate trace kept in eval-artifacts/trace (--reuse-traces) after
a change of the comparison, the normalisation or the dependency labels: the generated test is not run
again. The kept trace is used only if the stored b step was computed from the same result, verification,
case, project and configuration; a change of the tracer itself needs fresh traces (no --reuse-traces).

    python3 -m evaluation.evaluate --config config.yaml [--cases ...] [--run-ids ...] [--steps b ...]
        [--force] [--reuse-traces]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from evaluation import compare as C
from evaluation import inputs as I
from evaluation import surefire as S
from evaluation import trace as T
from evaluation.reference import load_reference
from runner.cases import Case, case_ids, eval_command, load_case, project_checkout
from runner.config import EVAL_STEPS, MODES, Config, load_config, neutral_name
from runner.process import resolve_argv, run


class StaleResult(Exception):
    """A stored result was computed from other inputs than the current ones."""


class Transient(Exception):
    """A step failed for a reason that says nothing about the test (timeout, tool error)."""


def record_path(cell: Path) -> Path:
    return cell / "eval.json"


def load_record(cell: Path) -> dict[str, Any]:
    p = record_path(cell)
    s = json.loads((cell / "summary.json").read_text())
    if p.is_file():
        rec = json.loads(p.read_text())
        rec["escape_check"] = s.get("escape_check")  # the check may run after the first evaluation
        return rec
    return {k: s.get(k) for k in ("case_id", "project", "group", "mode", "run_id", "status", "error",
                                  "model", "thinking", "agent_seconds", "tool_calls", "usage",
                                  "debugger", "komar", "escape_check")}


def save_record(cell: Path, rec: dict[str, Any]) -> None:
    record_path(cell).write_text(json.dumps(rec, indent=1, default=str))


def _keep(src: Path, dest: Path) -> None:
    if not src.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
    shutil.copytree(src, dest) if src.is_dir() else shutil.copy2(src, dest)


def result_workspace(cfg: Config, case: Case, cell: Path, tag: str) -> Path:
    """Copy of the compiled checkout, hidden tests removed, the cell's diff applied."""
    name = neutral_name(case.id, cell.parent.name, cell.name, tag)
    ws = T.clone_checkout(project_checkout(cfg, case.project), cfg.work_dir / "ws" / name)
    T.remove_paths(ws, case.hidden_paths)
    res = subprocess.run(["git", "apply", "--whitespace=nowarn", str(cell / "result.diff")],
                         cwd=ws, capture_output=True, text=True)
    if res.returncode != 0:
        shutil.rmtree(ws, ignore_errors=True)
        raise RuntimeError(f"git apply failed: {res.stderr[-800:]}")
    return ws


def has_test(rec: dict[str, Any], cell: Path) -> bool:
    return (cell / "result.diff").is_file() and bool((cell / "result.diff").read_text().strip())


def passes(rec: dict[str, Any]) -> bool:
    return bool((rec.get("verify") or {}).get("passes"))


def eval_verify(cfg: Config, case: Case, cell: Path, rec: dict[str, Any]) -> dict[str, Any]:
    """Independent check of the generated test on a clean copy: `build` + `generated_test`.
    A test left by an agent that timed out or failed is
    evaluated the same way (the runner status is kept in the record). The test passes when the
    command succeeds and the surefire reports of the class show at least one test run and no
    failure, error or skipped test."""
    if not has_test(rec, cell):
        return {"applies": False, "compiles": None, "passes": None, "note": "no generated test"}
    out: dict[str, Any] = {"applies": False, "compiles": None, "passes": None}
    try:
        ws = result_workspace(cfg, case, cell, "verify")
    except RuntimeError as e:
        out["note"] = str(e)[:500]
        return out
    logs = cell / "eval-artifacts" / "verify"
    logs.mkdir(parents=True, exist_ok=True)
    try:
        out["applies"] = True
        for name in ("build", "generated_test"):
            r = run(resolve_argv(eval_command(case, name), ws), ws, cfg.command_env(), cfg.command_timeout, name)
            (logs / f"{name}.log").write_text(r.stdout[-20000:] + r.stderr[-5000:])
            if r.timed_out:
                raise Transient(f"verify: {name} timed out after {cfg.command_timeout}s")
            out[f"{name}_exit_code"] = r.exit_code
            if name == "build":
                out["compiles"] = r.ok
                if not r.ok:
                    break
            else:
                counts = S.counts(ws, case.output_test_file)
                out.update(counts)
                out["command_ok"] = r.ok
                out["passes"] = S.passes(r.ok, counts)
                for f in S.report_files(ws, case.output_test_file):
                    _keep(f, logs / f.name)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    return out


# inputs of a stored b step that must be unchanged to reuse its kept candidate trace
TRACE_INPUTS = ("result", "verify", "case", "project", "config")


def kept_trace(cfg: Config, case: Case, cell: Path, rec: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Candidate invocations from eval-artifacts/trace, if the stored b step traced this very result."""
    trace_dir = cell / "eval-artifacts" / "trace"
    old = rec.get("b") or {}
    stored = (rec.get("inputs") or {}).get("b") or {}
    if not trace_dir.is_dir() or old.get("candidate_invocations") is None or not stored:
        return None
    current = I.step_inputs(cfg, case, cell, "b")
    if any(stored.get(k) != current.get(k) for k in TRACE_INPUTS):
        return None
    invocations = T.read_trace(trace_dir)["invocations"]
    return invocations if len(invocations) == old["candidate_invocations"] else None


def eval_b(cfg: Config, case: Case, cell: Path, rec: dict[str, Any], reuse_trace: bool = False) -> dict[str, Any]:
    ref = load_reference(cfg, case.id)
    candidate, note, trace = None, None, None
    reused = kept_trace(cfg, case, cell, rec) if reuse_trace and passes(rec) else None
    if reused is not None:
        candidate = reused
    elif passes(rec):
        ws = result_workspace(cfg, case, cell, "b")
        out_dir = cfg.work_dir / "out" / neutral_name(case.id, cell.parent.name, cell.name, "b")
        try:
            trace = T.trace_case_command(case, ws, "generated_test", out_dir, cfg.command_env(),
                                         cfg.command_timeout, cfg.trace_depth)
            _keep(out_dir / "trace", cell / "eval-artifacts" / "trace")
            if trace["run"]["exit_code"] == "timeout":
                raise Transient(f"b: generated test timed out under the tracer after {cfg.command_timeout}s")
            if trace["run"]["exit_code"] == 0:
                candidate = trace["invocations"]
            else:
                # a test that passed verify but fails under the tracer: a result (B = 0), counted apart
                note = f"trace_failed: generated test exit code {trace['run']['exit_code']} under the tracer"
        finally:
            shutil.rmtree(ws, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)
    else:
        v = rec.get("verify") or {}
        note = f"not run: verify compiles={v.get('compiles')} passes={v.get('passes')}"
    out = C.level_b(ref, candidate, case.project, C.labelled_dependencies(cfg.dependency_labels, case.id))
    out["note"] = note
    out["candidate_invocations"] = None if candidate is None else len(candidate)
    out["trace_reused"] = reused is not None
    if candidate is not None:
        out.update(C.caller_stats(candidate, S.test_class_name_from_diff(cell / "result.diff", case.output_test_file),
                                  case.scenario_test))
    return out


STEPS = {"verify": eval_verify, "b": eval_b}


def evaluate_cell(cfg: Config, cell: Path, steps: list[str] | None = None, force: list[str] | None = None,
                  reuse_traces: bool = False) -> dict[str, Any]:
    """Run the missing evaluation steps of one cell (the forced ones again); returns the record.

    A stored step is reused only if its recorded inputs equal the current ones; otherwise
    StaleResult (rerun with --force). A step that raises Transient is not stored."""
    rec = load_record(cell)
    case = load_case(rec["case_id"], cfg.cases_dir)
    hashes = rec.setdefault("inputs", {})
    transient = []
    for step in steps or cfg.eval_steps:
        current = I.step_inputs(cfg, case, cell, step)
        if step in rec and step not in (force or []):
            stored = hashes.get(step)
            if stored != current:
                changed = sorted(k for k in set(current) | set(stored or {})
                                 if (stored or {}).get(k) != current.get(k))
                raise StaleResult(f"{cell}: stored {step} was computed from other inputs ({', '.join(changed)}); "
                                  "rerun with --force")
            continue
        if step == "b" and rec.get("verify") is None:
            # verify failed for a transient reason (or was not run): B would be computed as if the test
            # had failed; wait for the next evaluation
            transient.append("b: waits for verify")
            continue
        try:
            if step == "b" and reuse_traces:
                rec[step] = eval_b(cfg, case, cell, rec, reuse_trace=True)
            else:
                rec[step] = STEPS[step](cfg, case, cell, rec)
        except Transient as e:
            rec.pop(step, None)
            hashes.pop(step, None)
            transient.append(str(e))
            save_record(cell, rec)
            continue
        hashes[step] = current
        save_record(cell, rec)
    if transient:
        rec["transient_errors"] = transient
    return rec


def cells(cfg: Config, cases: list[str], run_ids: list[str] | None, modes: list[str] | None) -> list[Path]:
    """Cells of the configured run ids only: directories put aside by the runner
    (<run-id>.infra-*, <run-id>.incomplete-*) are not cells."""
    wanted = set(run_ids or [cfg.run_id(r) for r in cfg.rounds])
    out = []
    for cid in cases:
        for s in sorted((cfg.cells_dir / cid).glob("*/*/summary.json")):
            cell = s.parent
            if cell.name not in wanted or (modes and cell.parent.name not in modes):
                continue
            out.append(cell)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--run-ids", nargs="*")
    ap.add_argument("--modes", nargs="*", choices=list(MODES))
    ap.add_argument("--steps", nargs="*", choices=list(EVAL_STEPS))
    ap.add_argument("--force", action="store_true", help="recompute the selected steps")
    ap.add_argument("--reuse-traces", action="store_true",
                    help="b: compare the kept candidate trace instead of running the test again")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    steps = a.steps or cfg.eval_steps
    failed = 0
    for cell in cells(cfg, case_ids(cfg, a.cases), a.run_ids, a.modes):
        try:
            rec = evaluate_cell(cfg, cell, steps, steps if a.force else None, a.reuse_traces)
            extra = f" (transient, rerun later: {'; '.join(rec['transient_errors'])})" if rec.get("transient_errors") else ""
            print(f"ok   {cell}{extra}", flush=True)
            failed += bool(extra)
        except Exception as e:
            failed += 1
            print(f"FAIL {cell}: {type(e).__name__}: {e}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
