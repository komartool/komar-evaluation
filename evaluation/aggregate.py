"""Level B per mode and paired comparisons between modes, from the evaluation records of a run.

Per cell: B of the generated test (eval.json "b"); a cell without a passing test scores 0, as does
a cell whose agent left no test. Per case: the mean over the rounds of the config. Per mode: the mean
over cases, next to the share of cells with a passing test and the mean precision. Paired: for every
pair of modes, the per-case differences with a bootstrap 95% interval of the mean, wins/ties/losses
and the two-sided Wilcoxon signed-rank p-value (evaluation/stats.py).

Every cell of the config (cases x modes x rounds) must be complete: generated, verified and, when its
test passes, traced. An incomplete cell is never scored 0 and never dropped silently: by default the
report lists the incomplete cells and stops (rerun evaluate, or the cell under the infrastructure
rule); with --exclude-incomplete their cases are dropped from every mode and listed.

    python3 -m evaluation.aggregate --config config.yaml [--config config-jdk11.yaml ...]
        [--exclude-incomplete] [--json out.json]

Several configs (e.g. the JDK 21 and the JDK 11 case lists) are reported separately and together.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from itertools import combinations
from typing import Any

from evaluation import stats as ST
from runner.cases import case_ids, config_case_rows
from runner.config import MODES, Config, load_config


def load(cfg: Config) -> list[dict[str, Any]]:
    """The evaluation records of the configured cases, modes and rounds."""
    cases = {r["id"]: r for r in config_case_rows(cfg)}
    rounds = {cfg.run_id(r) for r in cfg.rounds}
    recs = []
    for p in sorted(cfg.cells_dir.glob("*/*/*/eval.json")):
        if p.parent.name not in rounds:  # <run-id>.infra-*: cells moved aside, not results
            continue
        r = json.loads(p.read_text())
        if r.get("case_id") in cases and r.get("mode") in cfg.modes and r.get("run_id") == p.parent.name:
            r["project"] = cases[r["case_id"]]["project"]
            summary = p.parent / "summary.json"
            if summary.is_file():  # tool usage as written by the runner
                s = json.loads(summary.read_text())
                r["debugger"], r["komar"] = s.get("debugger"), s.get("komar")
            recs.append(r)
    return recs


def incomplete_reason(rec: dict[str, Any] | None) -> str | None:
    """Why a cell cannot be scored yet, or None when it can."""
    if rec is None:
        return "no evaluation record"
    v = rec.get("verify")
    if v is None:
        return "not verified" + _transient(rec)
    if v.get("passes") and rec.get("b") is None:
        return "test passes, level B missing" + _transient(rec)
    return None


def _transient(rec: dict[str, Any]) -> str:
    t = rec.get("transient_errors")
    return f" ({'; '.join(t)})" if t else ""


def incomplete_cells(cfg: Config, recs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Cells of the config (cases x modes x rounds) that cannot be scored: never generated, not
    evaluated, or with a passing test and no level B."""
    by = {(r["case_id"], r["mode"], r["run_id"]): r for r in recs}
    out = []
    for case in case_ids(cfg):
        for mode in cfg.modes:
            for rnd in cfg.rounds:
                run_id = cfg.run_id(rnd)
                rec = by.get((case, mode, run_id))
                reason = incomplete_reason(rec)
                if rec is None and not (cfg.cell_dir(case, mode, run_id) / "summary.json").is_file():
                    reason = "not generated"
                if reason:
                    out.append({"case": case, "mode": mode, "run_id": run_id, "reason": reason})
    return out


def cell_b(rec: dict[str, Any]) -> float:
    if not (rec.get("verify") or {}).get("passes"):
        return 0.0
    b = rec.get("b")
    if b is None:
        raise ValueError(f"{rec['case_id']} {rec['mode']} {rec['run_id']}: test passes, level B missing")
    return float(b.get("b") or 0.0)


def case_scores(recs: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    """(mode, case) -> mean B over the rounds present."""
    by: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in recs:
        by[(r["mode"], r["case_id"])].append(cell_b(r))
    return {k: statistics.mean(v) for k, v in by.items()}


def _sum(cells: list[dict[str, Any]], tool: str, key: str) -> int:
    return sum(int(((r.get(tool) or {}).get(key)) or 0) for r in cells)


def mode_rows(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores = case_scores(recs)
    out = []
    for mode in [m for m in MODES if any(r["mode"] == m for r in recs)]:
        cells = [r for r in recs if r["mode"] == mode]
        passing = [r for r in cells if (r.get("verify") or {}).get("passes")]
        vals = [v for (m, _), v in scores.items() if m == mode]
        prec = [r["b"]["precision"] for r in passing if (r.get("b") or {}).get("precision") is not None]
        row = {"mode": mode, "cases": len(vals), "cells": len(cells), "passing": len(passing),
               "b_mean": statistics.mean(vals) if vals else None,
               "precision_mean": statistics.mean(prec) if prec else None,
               "cells_via_scenario_dropped": sum(
                   1 for r in passing if ((r.get("b") or {}).get("invocations_counted") or 0)
                   < ((r.get("b") or {}).get("invocations_total") or 0)),
               "cells_trace_failed": sum(1 for r in passing
                                         if str((r.get("b") or {}).get("note") or "").startswith("trace_failed"))}
        if mode == "autonomous":
            row["tools"] = {k: _sum(cells, "debugger", k) for k in ("jvm_runs", "failed_starts", "refused_starts")}
        elif mode == "komar":
            row["tools"] = {k: _sum(cells, "komar", k) for k in ("runs", "spec_errors", "refused")}
        out.append(row)
    return out


def paired_rows(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores = case_scores(recs)
    modes = [m for m in MODES if any(r["mode"] == m for r in recs)]
    out = []
    # the later mode of MODES first: komar - autonomous, komar - baseline, autonomous - baseline
    for second, first in combinations(modes, 2):
        cases = sorted(c for (m, c) in scores if m == first and (second, c) in scores)
        d = [scores[(first, c)] - scores[(second, c)] for c in cases]
        if not d:
            continue
        lo, hi = ST.bootstrap_ci(d)
        w = ST.wilcoxon(d)
        out.append({"pair": f"{first} - {second}", "cases": len(d), "mean_diff": statistics.mean(d),
                    "ci95_low": lo, "ci95_high": hi, "wins": sum(x > 0 for x in d),
                    "ties": sum(x == 0 for x in d), "losses": sum(x < 0 for x in d), "wilcoxon_p": w["p"]})
    return out


def report(name: str, recs: list[dict[str, Any]], excluded: list[dict[str, str]]) -> dict[str, Any]:
    rows, pairs = mode_rows(recs), paired_rows(recs)
    fmt = lambda x, f: "n/a" if x is None else format(x, f)  # noqa: E731
    print(f"== {name}")
    if excluded:
        cases = sorted({c["case"] for c in excluded})
        print(f"  excluded from every mode ({len(cases)} cases with incomplete cells): {', '.join(cases)}")
    for r in rows:
        tools = " ".join(f"{k} {v}" for k, v in (r.get("tools") or {}).items())
        print(f"  {r['mode']:11} cases {r['cases']:3} cells {r['cells']:3} passing {r['passing']:3}  "
              f"B {fmt(r['b_mean'], '.3f')}  precision {fmt(r['precision_mean'], '.3f')}  "
              f"via-scenario dropped {r['cells_via_scenario_dropped']}  trace failed {r['cells_trace_failed']}"
              + (f"  [{tools}]" if tools else ""))
    for p in pairs:
        print(f"  {p['pair']:24} cases {p['cases']:3} diff {p['mean_diff']:+.3f} "
              f"[{fmt(p['ci95_low'], '+.3f')}, {fmt(p['ci95_high'], '+.3f')}] "
              f"+{p['wins']} ={p['ties']} -{p['losses']} p={fmt(p['wilcoxon_p'], '.3g')}")
    return {"name": name, "excluded": excluded, "modes": rows, "paired": pairs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", action="append", required=True)
    ap.add_argument("--exclude-incomplete", action="store_true",
                    help="drop the cases with an incomplete cell from every mode instead of stopping")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    loaded = []
    for path in a.config:
        cfg = load_config(path)
        recs = load(cfg)
        loaded.append((cfg, recs, incomplete_cells(cfg, recs)))
    missing = [c for _, _, inc in loaded for c in inc]
    if missing and not a.exclude_incomplete:
        print(f"{len(missing)} incomplete cells; rerun evaluation (python3 -m evaluation.evaluate), or the cell "
              "under the infrastructure rule, or pass --exclude-incomplete:")
        for c in missing:
            print(f"  {c['case']} {c['mode']} {c['run_id']}: {c['reason']}")
        return 2
    every, every_excluded, out = [], [], []
    for cfg, recs, inc in loaded:
        drop = {c["case"] for c in inc}
        kept = [r for r in recs if r["case_id"] not in drop]
        every += kept
        every_excluded += inc
        out.append(report(cfg.case_list, kept, inc))
    if len(loaded) > 1:
        out.append(report("all", every, every_excluded))
    if a.json:
        with open(a.json, "w") as h:
            json.dump(out, h, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
