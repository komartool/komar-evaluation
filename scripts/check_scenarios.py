"""Check, without the agent, that the driving scenario of every case runs in a prepared workspace:
the steps of a generation cell before the agent (exported commit, hidden tests removed, a fresh git
repository, the project's prebuild commands, the scenario check of runner/cell.py).

    python3 scripts/check_scenarios.py --config config.yaml [--cases ID ...] [--out DIR]

Output: <out>/<case>/summary.json and the logs of the commands; <out>/summary.json for all cases
(default out: <results>/scenario-check).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.cases import case_ids, check_jdk  # noqa: E402
from runner.cell import check_case_scenario  # noqa: E402
from runner.config import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    ids = case_ids(cfg, a.cases)
    check_jdk(cfg, ids)
    out = Path(a.out) if a.out else cfg.results_dir / "scenario-check"
    results = {}
    for case_id in ids:
        s = check_case_scenario(cfg, case_id, out / case_id)
        (out / case_id).mkdir(parents=True, exist_ok=True)
        (out / case_id / "summary.json").write_text(json.dumps(s, indent=1, default=str))
        results[case_id] = {"ok": s["ok"], "seconds": s["seconds"], "error": s.get("error")}
        print(f"{case_id}: {'ok' if s['ok'] else 'FAILED'} in {s['seconds']:.0f}s"
              + (f" ({s['error']})" if s.get("error") else ""), flush=True)
    (out / "summary.json").write_text(json.dumps(results, indent=1))
    return 0 if all(r["ok"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
