"""Level B per mode and paired comparisons between modes, from the evaluation records of a run.

Per cell: B of the generated test (eval.json "b"); a cell without a passing test scores 0, as does
a cell whose agent left no test. Per case: the mean over the rounds of the config. Per mode: the mean
over cases, next to the share of cells with a passing test. Paired: for every pair of modes, the
per-case differences with a bootstrap 95% interval of the mean, wins/ties/losses and the two-sided
Wilcoxon signed-rank p-value (evaluation/stats.py).

    python3 -m evaluation.aggregate --config config.yaml [--config config-jdk11.yaml ...] [--json out.json]

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
from runner.cases import config_case_rows
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
            recs.append(r)
    return recs


def cell_b(rec: dict[str, Any]) -> float:
    if not (rec.get("verify") or {}).get("passes"):
        return 0.0
    return float((rec.get("b") or {}).get("b") or 0.0)


def case_scores(recs: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    """(mode, case) -> mean B over the rounds present."""
    by: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in recs:
        by[(r["mode"], r["case_id"])].append(cell_b(r))
    return {k: statistics.mean(v) for k, v in by.items()}


def mode_rows(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores = case_scores(recs)
    out = []
    for mode in [m for m in MODES if any(r["mode"] == m for r in recs)]:
        cells = [r for r in recs if r["mode"] == mode]
        vals = [v for (m, _), v in scores.items() if m == mode]
        out.append({"mode": mode, "cases": len(vals), "cells": len(cells),
                    "passing": sum(1 for r in cells if (r.get("verify") or {}).get("passes")),
                    "b_mean": statistics.mean(vals) if vals else None})
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


def report(name: str, recs: list[dict[str, Any]]) -> dict[str, Any]:
    rows, pairs = mode_rows(recs), paired_rows(recs)
    print(f"== {name}")
    for r in rows:
        print(f"  {r['mode']:11} cases {r['cases']:3} cells {r['cells']:3} passing {r['passing']:3}  "
              f"B {r['b_mean']:.3f}")
    fmt = lambda x, f: "n/a" if x is None else format(x, f)  # noqa: E731
    for p in pairs:
        print(f"  {p['pair']:24} n={p['cases']:3} diff {p['mean_diff']:+.3f} "
              f"[{fmt(p['ci95_low'], '+.3f')}, {fmt(p['ci95_high'], '+.3f')}] "
              f"+{p['wins']} ={p['ties']} -{p['losses']} p={fmt(p['wilcoxon_p'], '.3g')}")
    return {"name": name, "modes": rows, "paired": pairs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", action="append", required=True)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    every, out = [], []
    for path in a.config:
        cfg = load_config(path)
        recs = load(cfg)
        every += recs
        out.append(report(cfg.case_list, recs))
    if len(a.config) > 1:
        out.append(report("all", every))
    if a.json:
        with open(a.json, "w") as h:
            json.dump(out, h, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
