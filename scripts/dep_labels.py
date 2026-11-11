"""Label the object-valued receiver fields of every case as DEPENDENCY or STATE with a language model
(evaluation/dep_labels.py; the labels feed level B, evaluation/compare.py level_b).

    python3 scripts/dep_labels.py --config config.yaml units           # fields to label, from the references
    python3 scripts/dep_labels.py --config config.yaml label --run p1  # first phrasing of the prompt
    python3 scripts/dep_labels.py --config config.yaml label --run p2  # second phrasing, same definition
    python3 scripts/dep_labels.py --config config.yaml merge           # agreement; disagreements -> p3
    python3 scripts/dep_labels.py --config config.yaml label --run p3  # third run on the disagreements only
    python3 scripts/dep_labels.py --config config.yaml merge           # final labels (majority of three)

The model sees the target method, the source of its class and the developers' tests of the class at the
pinned commit (the hidden test paths of the case), never a generated test or a result. Settings (section
`labelling` of the config): model, base_url (default: provider.base_url), api_key_env (default:
provider.api_key_env), out (default: <results>/labelling). merge writes <out>/labels.json; copy it to
evaluation/dependency-labels.json (or point evaluation.dependency_labels at it) to use it for level B.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from evaluation import dep_labels as D  # noqa: E402
from runner.cases import case_ids, load_case, load_projects, project_checkout  # noqa: E402
from runner.config import Config, load_config  # noqa: E402

PROMPTS = ROOT / "evaluation" / "labelling-prompts"
MAX_CLASS = 40_000
MAX_TESTS = 30_000
# temperature and seed of each run: p1 and p2 are the two phrasings, p3 decides their disagreements
RUNS = {"p1": (0.0, 1), "p2": (0.0, 2), "p3": (0.7, 3)}


class Settings:
    def __init__(self, cfg: Config):
        raw = (yaml.safe_load(cfg.path.read_text()) or {}).get("labelling") or {}
        self.model = raw.get("model") or os.environ.get("KEVAL_LABEL_MODEL")
        self.base_url = (raw.get("base_url") or cfg.base_url).rstrip("/")
        self.api_key_env = raw.get("api_key_env") or cfg.api_key_env
        self.out = Path(os.path.expanduser(raw.get("out") or cfg.results_dir / "labelling"))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def class_path(files: list[str], fqcn: str) -> str | None:
    rel = fqcn.split("$")[0].replace(".", "/") + ".java"
    main = [f for f in files if f.endswith("/" + rel) and "/test/" not in f and "/tst/" not in f]
    return main[0] if main else None


def developer_tests(hidden: list[str], target_class: str, files: list[str]) -> list[str]:
    tests = [p for p in hidden if p.endswith(".java")]
    if tests:
        return tests
    simple = target_class.split(".")[-1].split("$")[0]
    pat = re.compile(rf"/{re.escape(simple)}(Test|Tests|IT|TestCase)\w*\.java$")
    return [f for f in files if pat.search(f) and ("/test/" in f or "/tst/" in f)][:3]


def declared_type(source: str, field: str) -> str | None:
    m = re.search(rf"^\s*(?:(?:private|protected|public|static|final|transient|volatile)\s+)*"
                  rf"([\w.$<>\[\], ?]+?)\s+{re.escape(field)}\s*(?:=|;)", source, re.M)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n// ... (trimmed)\n"


def cmd_units(cfg: Config, st: Settings) -> None:
    projects = load_projects()
    units, skipped = [], []
    for cid in case_ids(cfg):
        case = load_case(cid, cfg.cases_dir)
        ref = json.loads((cfg.references_dir / cid / "run1.json").read_text())["invocations"]
        repo, commit = project_checkout(cfg, case.project), projects[case.project].commit
        files = git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()
        target_class = case.target["class"]
        cp = class_path(files, target_class)
        src = git(repo, "show", f"{commit}:{cp}") if cp else ""
        fields = []
        for name, f in sorted(D.receiver_fields(ref).items()):
            entry = {"name": name, "runtime_classes": f["classes"], "kinds": f["kinds"],
                     "declared_type": declared_type(src, name.split(".")[-1])}
            if f["object"]:
                fields.append(entry)
            else:
                skipped.append({"case": cid, **entry})
        units.append({"case": cid, "project": case.project, "commit": commit, "target": case.target,
                      "class_file": cp,
                      "tests": developer_tests(case.hidden_paths, target_class, files), "fields": fields})
    st.out.mkdir(parents=True, exist_ok=True)
    (st.out / "units.json").write_text(json.dumps({"units": units, "not_labelled": skipped}, indent=1))
    print(f"cases {len(units)}, fields to label {sum(len(u['fields']) for u in units)}, "
          f"not labelled (null/scalar/collection/JDK) {len(skipped)}")


def build_prompt(cfg: Config, unit: dict, run: str) -> tuple[str, str]:
    repo = project_checkout(cfg, unit["project"])
    show = lambda path: git(repo, "show", f"{unit['commit']}:{path}")  # noqa: E731
    src = show(unit["class_file"]) if unit["class_file"] else "(source not found)"
    tests, budget = [], MAX_TESTS
    for t in unit["tests"]:
        try:
            body = trim(show(t), budget)
        except subprocess.CalledProcessError:
            continue
        budget -= len(body)
        tests.append(f"// {t}\n{body}")
        if budget <= 0:
            break
    fields = "\n".join(
        f"- {f['name']}: declared type {f['declared_type'] or 'unknown (inherited or not found)'}; "
        f"runtime class in the recorded execution: {', '.join(f['runtime_classes']) or 'unknown'}"
        for f in unit["fields"])
    phrasing = "p2" if run == "p2" else "p1"
    t = unit["target"]
    user = (PROMPTS / f"user-{phrasing}.txt").read_text().format(
        target_class=t["class"], target_method=t["method"], signature=t.get("signature", ""),
        class_source=trim(src, MAX_CLASS), fields=fields,
        tests="\n\n".join(tests) if tests else "(no developer tests of this class found)")
    return (PROMPTS / f"system-{phrasing}.txt").read_text(), user


def chat(st: Settings, system: str, user: str, temperature: float, seed: int) -> dict:
    key = os.environ.get(st.api_key_env)
    if not key:
        raise SystemExit(f"set {st.api_key_env} (labelling.api_key_env)")
    body = {"model": st.model, "temperature": temperature, "seed": seed,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}}
    last = None
    for attempt in range(6):
        req = urllib.request.Request(f"{st.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:300]!r}"
            if e.code == 400 and "response_format" in body:
                body.pop("response_format")
                continue
            if e.code < 500 and e.code != 429:
                raise RuntimeError(last) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = str(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"gateway failed: {last}")


def cmd_label(cfg: Config, st: Settings, run: str, only: list[str] | None) -> None:
    if not st.model:
        raise SystemExit("set labelling.model in the config (or KEVAL_LABEL_MODEL)")
    temperature, seed = RUNS[run]
    units = json.loads((st.out / "units.json").read_text())["units"]
    if run == "p3":
        dis = json.loads((st.out / "disagreements.json").read_text())
        units = [u for u in units if u["case"] in dis]
    if only:
        units = [u for u in units if u["case"] in only]
    rawdir = st.out / "raw" / run
    rawdir.mkdir(parents=True, exist_ok=True)
    for u in units:
        if not u["fields"] or (rawdir / f"{u['case']}.json").exists():
            continue
        system, user = build_prompt(cfg, u, run)
        err = None
        for attempt in range(3):
            resp = chat(st, system, user, temperature, seed + attempt)
            text = resp["choices"][0]["message"]["content"]
            try:
                parsed = D.parse_response(text)
                missing = [f["name"] for f in u["fields"] if f["name"] not in parsed]
                if not missing:
                    break
                err = f"missing fields {missing}"
            except (ValueError, json.JSONDecodeError) as e:
                err = str(e)
            print(f"{u['case']}: retry ({err})", flush=True)
        else:
            parsed = {}
        (rawdir / f"{u['case']}.json").write_text(json.dumps(
            {"case": u["case"], "run": run, "model": st.model, "temperature": temperature,
             "prompt_chars": len(system) + len(user), "response": text, "usage": resp.get("usage"),
             "labels": parsed, "error": None if parsed else err}, indent=1))
        print(f"{u['case']}: {len(parsed)} labels", flush=True)


def load_run(out: Path, run: str) -> dict[str, dict]:
    d = out / "raw" / run
    return {p.stem: json.loads(p.read_text()) for p in d.glob("*.json")} if d.exists() else {}


def cmd_merge(st: Settings) -> None:
    out = st.out
    units = json.loads((out / "units.json").read_text())["units"]
    runs = {r: load_run(out, r) for r in RUNS}
    a, b, dis, final = [], [], {}, []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    for r in runs.values():
        for x in r.values():
            u = x.get("usage") or {}
            usage["prompt_tokens"] += u.get("prompt_tokens", 0)
            usage["completion_tokens"] += u.get("completion_tokens", 0)
            usage["cost"] += float(u.get("price") or u.get("cost") or 0)
    for u in units:
        for f in u["fields"]:
            got = [runs[r].get(u["case"], {}).get("labels", {}).get(f["name"]) for r in RUNS]
            v1, v2, v3 = (x["label"] if x else None for x in got)
            if v1 and v2:
                a.append(v1)
                b.append(v2)
            if v1 != v2:
                dis.setdefault(u["case"], []).append(f["name"])
            label = v1 if v1 == v2 else D.majority([v1, v2, v3])
            reason = (got[0] or got[1] or {}).get("reason", "") if v1 == v2 else (got[2] or {}).get("reason", "")
            final.append({"case": u["case"], "project": u["project"], "field": f["name"],
                          "declared_type": f["declared_type"], "runtime_classes": f["runtime_classes"],
                          "label": label, "reason": reason, "p1": v1, "p2": v2, "p3": v3})
    meta = {"pairs": len(a), "agreement": sum(x == y for x, y in zip(a, b)) / len(a) if a else None,
            "kappa": D.cohen_kappa(a, b), "disagreements": sum(len(v) for v in dis.values()),
            "unresolved": sum(1 for x in final if not x["label"]), "usage": usage,
            "model": next((x["model"] for r in runs.values() for x in r.values()), None)}
    (out / "disagreements.json").write_text(json.dumps(dis, indent=1))
    (out / "labels.json").write_text(json.dumps({"meta": meta, "labels": final}, indent=1))
    write_review(out, final)
    print(json.dumps(meta, indent=1))


def write_review(out: Path, final: list[dict]) -> None:
    """review.csv: every label, with a stratified sample of 50 marked for a manual check."""
    rnd = random.Random(7)
    by_proj: dict[str, list[int]] = {}
    for i, x in enumerate(final):
        by_proj.setdefault(x["project"], []).append(i)
    sample: set[int] = set()
    while len(sample) < min(50, len(final)):
        for p in sorted(by_proj):
            pool = [i for i in by_proj[p] if i not in sample]
            if pool and len(sample) < 50:
                sample.add(rnd.choice(pool))
    with (out / "review.csv").open("w", newline="") as h:
        w = csv.writer(h)
        w.writerow(["spot_check", "case", "field", "declared_type", "runtime_class", "label", "reason",
                    "run1", "run2", "run3", "your_label"])
        for i, x in enumerate(final):
            w.writerow(["x" if i in sample else "", x["case"], x["field"], x["declared_type"] or "",
                        " ".join(x["runtime_classes"]), x["label"] or "", x["reason"], x["p1"], x["p2"],
                        x["p3"] or "", ""])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("units")
    lab = sub.add_parser("label")
    lab.add_argument("--run", choices=list(RUNS), required=True)
    lab.add_argument("--cases", nargs="*")
    sub.add_parser("merge")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    st = Settings(cfg)
    if a.cmd == "units":
        cmd_units(cfg, st)
    elif a.cmd == "label":
        cmd_label(cfg, st, a.run, a.cases)
    else:
        cmd_merge(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
