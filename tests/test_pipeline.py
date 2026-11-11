"""Verification counts, stale-result detection, cell selection, references, aggregation, statistics."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from evaluation import aggregate as AG
from evaluation import evaluate as E
from evaluation import inputs as I
from evaluation import reference as R
from evaluation import stats as ST
from evaluation import surefire as S
from runner.config import load_config


def config(tmp: Path, **extra) -> Path:
    data = {"provider": {"name": "p", "base_url": "https://example.invalid/v1", "api_key_env": "K"},
            "model": "m", "rounds": [1, 2],
            "paths": {"projects": str(tmp / "projects"), "results": str(tmp / "results"), "java_home": str(tmp)},
            **extra}
    p = tmp / "config.yaml"
    p.write_text(yaml.dump(data))
    return p


def report(tests=1, failures=0, errors=0, skipped=0) -> str:
    return (f'<?xml version="1.0"?><testsuite name="x" tests="{tests}" failures="{failures}" '
            f'errors="{errors}" skipped="{skipped}"/>')


class SurefireTest(unittest.TestCase):
    def workspace(self, reports: dict[str, str]) -> Path:
        ws = Path(tempfile.mkdtemp())
        (ws / "core").mkdir()
        (ws / "core" / "pom.xml").write_text("<project/>")
        src = ws / "core/src/test/java/a/b/FooCarvedTest.java"
        src.parent.mkdir(parents=True)
        src.write_text("package a.b;\nclass FooCarvedTest {}")
        d = ws / "core/target/surefire-reports"
        d.mkdir(parents=True)
        for name, text in reports.items():
            (d / name).write_text(text)
        return ws

    TEST = "core/src/test/java/a/b/FooCarvedTest.java"

    def test_passes_needs_a_test_that_ran(self):
        ws = self.workspace({"TEST-a.b.FooCarvedTest.xml": report(tests=3)})
        c = S.counts(ws, self.TEST)
        self.assertEqual((c["reports"], c["tests_run"]), (1, 3))
        self.assertTrue(S.passes(True, c))
        self.assertFalse(S.passes(False, c))

    def test_zero_tests_disabled_and_assumptions_do_not_pass(self):
        self.assertFalse(S.passes(True, S.counts(self.workspace({}), self.TEST)))  # no report: engine missing
        self.assertFalse(S.passes(True, S.counts(self.workspace({"TEST-a.b.FooCarvedTest.xml": report(tests=0)}),
                                                 self.TEST)))
        disabled = S.counts(self.workspace({"TEST-a.b.FooCarvedTest.xml": report(tests=2, skipped=2)}), self.TEST)
        self.assertFalse(S.passes(True, disabled))
        partly = S.counts(self.workspace({"TEST-a.b.FooCarvedTest.xml": report(tests=2, skipped=1)}), self.TEST)
        self.assertFalse(S.passes(True, partly))

    def test_nested_classes_counted_other_classes_not(self):
        ws = self.workspace({"TEST-a.b.FooCarvedTest.xml": report(tests=1),
                             "TEST-a.b.FooCarvedTest$Inner.xml": report(tests=2, failures=1),
                             "TEST-a.b.FooCarvedTestOther.xml": report(tests=5)})
        c = S.counts(ws, self.TEST)
        self.assertEqual((c["tests_run"], c["failures"]), (3, 1))
        self.assertFalse(S.passes(True, c))


class StaleAndTransientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_config(config(self.tmp))
        self.cell = self.cfg.cell_dir("jsoup-normalise-whitespace", "komar", "r1")
        self.cell.mkdir(parents=True)
        (self.cell / "summary.json").write_text(json.dumps({"case_id": "jsoup-normalise-whitespace",
                                                            "mode": "komar", "run_id": "r1", "status": "completed"}))
        (self.cell / "result.diff").write_text("+x\n")

    def test_stored_step_with_other_inputs_is_refused(self):
        with mock.patch.dict(E.STEPS, {"verify": lambda *a: {"v": 1}}):
            E.evaluate_cell(self.cfg, self.cell, ["verify"])
            E.evaluate_cell(self.cfg, self.cell, ["verify"])  # same inputs: reused
            (self.cell / "result.diff").write_text("+y\n")
            with self.assertRaises(E.StaleResult):
                E.evaluate_cell(self.cfg, self.cell, ["verify"])
            rec = E.evaluate_cell(self.cfg, self.cell, ["verify"], force=["verify"])
            self.assertEqual(rec["verify"], {"v": 1})

    def test_dependency_labels_are_an_input_of_b(self):
        from runner.cases import load_case
        case = load_case("mybatis-default-sql-session-update")
        now = I.step_inputs(self.cfg, case, self.cell, "b")
        from evaluation import compare as C
        with mock.patch.object(C, "labelled_dependencies", lambda *a: {"other"}):
            changed = I.step_inputs(self.cfg, case, self.cell, "b")
        self.assertNotEqual(now["labels"], changed["labels"])
        self.assertEqual(now["reference"], changed["reference"])

    def test_normalisation_extra_is_per_project(self):
        from evaluation import compare as C
        with mock.patch.object(C, "load_extra", lambda p: {"components": ["a.B"]} if p == "pdfbox" else {}):
            self.assertEqual(I.normalisation_hash("jsoup"), I.normalisation_hash())
            self.assertNotEqual(I.normalisation_hash("pdfbox"), I.normalisation_hash())

    def test_normalisation_change_invalidates_b(self):
        rec = {"inputs": {"b": I.step_inputs(self.cfg, __import__("runner.cases", fromlist=["x"]).load_case(
            "jsoup-normalise-whitespace"), self.cell, "b")}, "b": {}}
        with mock.patch.object(I, "normalisation_hash", lambda project=None: "changed"):
            from runner.cases import load_case
            now = I.step_inputs(self.cfg, load_case("jsoup-normalise-whitespace"), self.cell, "b")
        self.assertNotEqual(rec["inputs"]["b"]["normalisation"], now["normalisation"])

    def test_code_hash_per_step(self):
        from runner.cases import load_case
        case = load_case("jsoup-normalise-whitespace")
        I._CODE.clear()
        base = {s: I.step_inputs(self.cfg, case, self.cell, s)["code"] for s in ("verify", "b")}
        gen = I.generation_inputs(self.cfg, case, "komar")["code"]
        real = I.files_hash

        def changed(paths, base_dir=I.ROOT):  # pretend evaluation/compare.py changed
            return real(paths, base_dir) + ("*" if any(p.name == "compare.py" for p in paths) else "")
        with mock.patch.object(I, "files_hash", changed):
            I._CODE.clear()
            now = {s: I.step_inputs(self.cfg, case, self.cell, s)["code"] for s in base}
            gen_now = I.generation_inputs(self.cfg, case, "komar")["code"]
        I._CODE.clear()
        self.assertNotEqual(now["b"], base["b"])
        self.assertEqual(now["verify"], base["verify"])
        self.assertEqual(gen_now, gen)

    def test_b_from_kept_trace(self):
        from runner.cases import load_case
        case = load_case("jsoup-normalise-whitespace")
        trace = self.cell / "eval-artifacts" / "trace"
        trace.mkdir(parents=True)
        lines = [{"ev": "jvm", "main": "surefirebooter.jar"},
                 {"ev": "enter", "id": 1, "args": ["a  b"], "self": None},
                 {"ev": "return", "id": 1, "value": "a b"}]
        (trace / "trace-1-1.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
        rec = {"case_id": case.id, "mode": "komar", "run_id": "r1", "verify": {"passes": True},
               "b": {"candidate_invocations": 1}}
        (self.cell / "eval.json").write_text(json.dumps(rec))
        rec["inputs"] = {"b": {**I.step_inputs(self.cfg, case, self.cell, "b"), "code": "old",
                                 "normalisation": "old"}}
        (self.cell / "eval.json").write_text(json.dumps(rec))
        ref = [{"self": None, "args": ["a  b"], "calls": [], "outcome": "return", "value": "a b"}]
        with mock.patch.object(E, "load_reference", lambda *a: ref), \
                mock.patch.object(E, "result_workspace", side_effect=AssertionError("test run again")):
            out = E.evaluate_cell(self.cfg, self.cell, ["b"], force=["b"], reuse_traces=True)
        self.assertTrue(out["b"]["trace_reused"])
        self.assertEqual(out["b"]["candidate_invocations"], 1)
        self.assertEqual(out["b"]["b"], 1.0)
        self.assertEqual(out["inputs"]["b"], I.step_inputs(self.cfg, case, self.cell, "b"))
        # another result: the kept trace belongs to the old test, so it is not reused
        (self.cell / "result.diff").write_text("+other\n")
        self.assertIsNone(E.kept_trace(self.cfg, case, self.cell, out))

    def test_transient_errors_are_not_stored(self):
        def boom(*a):
            raise E.Transient("b: generated test timed out")
        with mock.patch.dict(E.STEPS, {"verify": lambda *a: {"passes": True}, "b": boom}):
            rec = E.evaluate_cell(self.cfg, self.cell, ["verify", "b"])
        self.assertNotIn("b", rec)
        self.assertEqual(rec["transient_errors"], ["b: generated test timed out"])
        self.assertNotIn("b", json.loads((self.cell / "eval.json").read_text()))

    def test_b_waits_for_a_transient_verify(self):
        def slow(*a):
            raise E.Transient("verify: build timed out")
        with mock.patch.dict(E.STEPS, {"verify": slow}):
            rec = E.evaluate_cell(self.cfg, self.cell, ["verify", "b"])
        self.assertNotIn("b", rec)
        self.assertEqual(rec["transient_errors"], ["verify: build timed out", "b: waits for verify"])


class CellSelectionTest(unittest.TestCase):
    def test_only_configured_run_ids(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = load_config(config(tmp))
        for name in ("r1", "r2", "r1.infra-123", "r1.incomplete-5", "r3"):
            d = cfg.cell_dir("jsoup-normalise-whitespace", "komar", name)
            d.mkdir(parents=True)
            (d / "summary.json").write_text("{}")
            (d / "eval.json").write_text(json.dumps({"case_id": "jsoup-normalise-whitespace", "mode": "komar",
                                                     "run_id": "r1" if name.startswith("r1") else name}))
        names = [c.name for c in E.cells(cfg, ["jsoup-normalise-whitespace"], None, None)]
        self.assertEqual(names, ["r1", "r2"])
        recs = AG.load(cfg)
        self.assertEqual(sorted(r["run_id"] for r in recs), ["r1", "r2"])


class ReferenceValidationTest(unittest.TestCase):
    def audit(self, rc=0, inv=1, recall=1.0):
        return {"runs": [{"exit_code": rc, "invocations": inv}, {"exit_code": 0, "invocations": inv}],
                "run1_vs_run2": {f"recall_{l}": recall for l in R.LEVELS}}

    def test_problems(self):
        self.assertEqual(R.problems(self.audit(), 2), [])
        self.assertTrue(any("exit code" in p for p in R.problems(self.audit(rc=1), 2)))
        self.assertTrue(any("no invocation" in p for p in R.problems(self.audit(inv=0), 2)))
        self.assertTrue(any("run 1 vs run 2" in p for p in R.problems(self.audit(recall=0.5), 2)))

    def test_rejected_reference_keeps_its_traces_under_other_names(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = load_config(config(tmp))
        cfg.references_root = tmp / "references"  # never the shipped references of the repository
        case = mock.Mock(id="c", project="p")
        traces = [{"run": {"exit_code": 0, "seconds": 1}, "errors": [], "invocations": [], "n": i} for i in (1, 2)]
        out = cfg.references_dir / "c"
        out.mkdir(parents=True)
        for i, t in enumerate(traces, start=1):  # traces of an earlier recording
            (out / f"run{i}.json").write_text(json.dumps(t))
        with mock.patch.object(R, "load_case", lambda *a: case):
            with self.assertRaises(R.ReferenceError):
                R.record(cfg, "c", reaudit=True)
            self.assertFalse((out / "run1.json").exists())
            self.assertTrue((out / "audit-failed.json").is_file())
        with mock.patch.object(R, "load_case", lambda *a: case), \
                mock.patch.object(R.T, "clone_checkout", lambda src, dest: dest), \
                mock.patch.object(R.T, "remove_paths", lambda *a: None), \
                mock.patch.object(R.T, "trace_case_command", side_effect=traces), \
                mock.patch.object(R, "project_checkout", lambda *a: tmp):
            with self.assertRaises(R.ReferenceError):  # no invocation recorded
                R.record(cfg, "c", force=True)
        self.assertFalse((out / "run1.json").exists())
        self.assertEqual(json.loads((out / "failed-run2.json").read_text())["n"], 2)
        self.assertFalse(R.reference_exists(cfg, "c"))


def rec(case, mode, rnd, b=1.0, passes=True):
    return {"case_id": case, "mode": mode, "run_id": f"r{rnd}", "verify": {"passes": passes, "compiles": True},
            "b": {"b": b, "scenarios": 2}}


class AggregateTest(unittest.TestCase):
    def test_incomplete_cells_are_never_scored(self):
        self.assertEqual(AG.incomplete_reason(None), "no evaluation record")
        self.assertEqual(AG.incomplete_reason({"transient_errors": ["verify: x"]}), "not verified (verify: x)")
        passing_no_b = {"case_id": "c1", "mode": "komar", "run_id": "r1", "verify": {"passes": True}}
        self.assertEqual(AG.incomplete_reason(passing_no_b), "test passes, level B missing")
        self.assertIsNone(AG.incomplete_reason(rec("c1", "komar", 1, passes=False) | {"b": None}))
        with self.assertRaises(ValueError):
            AG.cell_b(passing_no_b)

    def test_aggregate_stops_on_incomplete_cells_or_excludes_their_cases(self):
        tmp = Path(tempfile.mkdtemp())
        cases = ["jsoup-normalise-whitespace", "jsoup-element-has-class"]
        path = config(tmp, rounds=[1], modes=["baseline", "komar"], cases=cases)
        cfg = load_config(path)
        for case in cases:
            for mode in ("baseline", "komar"):
                d = cfg.cell_dir(case, mode, "r1")
                d.mkdir(parents=True)
                (d / "summary.json").write_text("{}")
                r = rec(case, mode, 1)
                if (case, mode) == (cases[1], "komar"):
                    del r["b"]  # passing test, B missing
                (d / "eval.json").write_text(json.dumps(r))
        self.assertEqual([(c["case"], c["mode"]) for c in AG.incomplete_cells(cfg, AG.load(cfg))],
                         [(cases[1], "komar")])
        self.assertEqual(AG.main(["--config", str(path)]), 2)
        out = tmp / "out.json"
        self.assertEqual(AG.main(["--config", str(path), "--exclude-incomplete", "--json", str(out)]), 0)
        rows = {r["mode"]: r for r in json.loads(out.read_text())[0]["modes"]}
        self.assertEqual((rows["baseline"]["cases"], rows["komar"]["cases"]), (1, 1))  # dropped from both

    def test_case_means_failed_cells_score_zero(self):
        recs = [rec("c1", "baseline", 1, b=0.0), rec("c1", "baseline", 2, b=1.0),
                rec("c2", "baseline", 1, b=1.0), rec("c2", "baseline", 2, b=1.0, passes=False),
                rec("c1", "komar", 1, b=1.0), rec("c2", "komar", 1, b=1.0)]
        rows = {r["mode"]: r for r in AG.mode_rows(recs)}
        self.assertAlmostEqual(rows["baseline"]["b_mean"], 0.5)  # (0.5 + 0.5) / 2
        self.assertEqual((rows["komar"]["cases"], rows["komar"]["passing"], rows["komar"]["b_mean"]), (2, 2, 1.0))
        pair = AG.paired_rows(recs)[0]
        self.assertEqual((pair["pair"], pair["cases"], pair["mean_diff"]), ("komar - baseline", 2, 0.5))


class StatsTest(unittest.TestCase):
    def test_wilcoxon_exact_and_normal(self):
        self.assertEqual(ST.wilcoxon([0, 0])["n"], 0)
        r = ST.wilcoxon([1, 2, 3, 4, 5, 6])
        self.assertEqual(r["method"], "exact")
        self.assertAlmostEqual(r["p"], 2 / 64)  # all positive: the most extreme of 2^6
        # ties: normal approximation. |d| ranks 1.5,1.5,3.5,3.5, W+ = 8.5, mean 5,
        # var = 4*5*9/24 - (6+6)/48 = 7.25, z = (3.5 - 0.5)/sqrt(7.25)
        r = ST.wilcoxon([0.1, -0.1, 0.2, 0.2])
        self.assertEqual((r["method"], r["w_plus"]), ("normal", 8.5))
        import math
        self.assertAlmostEqual(r["p"], math.erfc(3 / math.sqrt(7.25) / math.sqrt(2)))

    def test_bootstrap_is_deterministic(self):
        d = [0.1, 0.3, -0.2, 0.5, 0.0]
        self.assertEqual(ST.bootstrap_ci(d), ST.bootstrap_ci(d))
        lo, hi = ST.bootstrap_ci(d)
        self.assertLessEqual(lo, sum(d) / len(d))
        self.assertGreaterEqual(hi, sum(d) / len(d))


if __name__ == "__main__":
    unittest.main()
