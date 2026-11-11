"""The autonomous mode: pi/debugger.ts and its JDI server, the journal, the wiring of the mode.

The node checks need node and an installed Pi package (as tests/test_runner.py); the integration
check also needs JDK 21, Maven and the Maven repository of the subject projects (KEVAL_M2, default
~/.m2/repository) to build tests/debugger/sample offline. Missing tools skip the check."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from runner import agent as A
from runner import debugger as D
from runner import escape_check as X
from runner.cases import load_case
from runner.config import AGENT_MODES, ROOT, load_config
from runner.prompts import render
from tests.test_runner import PiExtensionsTest, write_config


def free_test_port() -> int:
    """A free port of KEVAL_TEST_PORTS (default 5450-5499, outside the ranges of experiment runs)."""
    import socket
    lo, hi = (int(x) for x in os.environ.get("KEVAL_TEST_PORTS", "5450-5499").split("-"))
    for port in range(lo, hi + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise unittest.SkipTest(f"no free port in {lo}-{hi}")


def node_run(*args: str, timeout: int = 120) -> dict:
    pi = PiExtensionsTest.pi_package()
    if pi is None or shutil.which("node") is None:
        raise unittest.SkipTest("node and an installed Pi package are needed")
    r = subprocess.run(["node", str(ROOT / "tests" / "node" / "debugger.mjs"), str(pi), str(ROOT), *args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise AssertionError(r.stderr[-3000:])
    return json.loads(r.stdout)


class ModeWiringTest(unittest.TestCase):
    def test_autonomous_mode_prompt_and_argv(self):
        self.assertIn("autonomous", AGENT_MODES)
        case = load_case("jsoup-token-queue-chomp-balanced")
        commands = {n: case.commands[n] for n in ("build", "scenario", "generated_test")}
        p = render(ROOT / "prompts", case, "autonomous", commands)
        self.assertIn("Condition: debugger.", p["task"])
        self.assertIn("run the scenario again, at most 3 runs in total.", p["task"])
        p5 = render(ROOT / "prompts", case, "autonomous", commands, debug_runs=5)
        self.assertIn("at most 5 runs in total.", p5["task"])
        self.assertLess(len((ROOT / "prompts" / "task-autonomous.txt").read_text()), 700)
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(write_config(Path(d), paths={"java_home": d, "pi": "/bin/echo", "results": d + "/r"}))
            plain = A.pi_argv(cfg, {"system": "S", "task": "T"}, Path(d))
            debug = A.pi_argv(cfg, {"system": "S", "task": "T"}, Path(d), debugger=True)
        self.assertFalse(any(a.endswith("debugger.ts") for a in plain))
        self.assertTrue(any(a.endswith("pi/debugger.ts") for a in debug))
        self.assertEqual(debug[debug.index("-t") + 1].split(","), list(A.BUILTIN_TOOLS + A.DEBUGGER_TOOLS))
        self.assertEqual(plain[plain.index("-t") + 1].split(","), list(A.BUILTIN_TOOLS))

    def test_debugged_scenario_is_the_checked_scenario_command(self):
        from runner.cases import eval_command
        from runner.cell import debugger_input
        klaw = load_case("klaw-util-methods-get-user-name")
        d = debugger_input(klaw)
        self.assertEqual(d["scenario"], eval_command(klaw, "scenario"))
        self.assertIn("-Dspotless.check.skip=true", d["scenario"])
        self.assertEqual(d["target"], klaw.target)

    def test_resume_waits_longer_for_the_first_stop_of_a_run(self):
        src = (ROOT / "pi" / "debugger.ts").read_text()
        self.assertIn("export const FIRST_RESUME_TIMEOUT = 300;", src)
        self.assertIn("export const RESUME_TIMEOUT = 60;", src)
        self.assertIn("resumedInRun ? RESUME_TIMEOUT : FIRST_RESUME_TIMEOUT", src)

    def test_journal_summary_and_expressions(self):
        rows = [
            {"tool": "set_breakpoint", "ok": True, "args": {"method": "a.B#c", "on": "exit", "condition": "x > 1"}},
            {"tool": "debug_start", "ok": True, "event": {"kind": "start"}},
            {"tool": "resume", "ok": True, "event": {"kind": "stop", "in_target": True}},
            {"tool": "inspect", "ok": True, "args": {"expr": "this.items.size()"}, "answer_chars": 10},
            {"tool": "set_breakpoint", "ok": True, "args": {"file": "F.java", "code": "x"}},
            {"tool": "remove_breakpoint", "ok": False, "args": {"id": 9}},
            {"tool": "resume", "ok": True, "event": {"kind": "stop", "in_target": False}},
            {"tool": "resume", "ok": True, "event": {"kind": "terminated"}},
            {"tool": "debug_start", "ok": True, "event": {"kind": "start"}},
            {"event": "run_end", "reason": "bash"},
        ]
        s = D.journal_summary(rows)
        self.assertEqual((s["jvm_runs"], s["stops"], s["stops_in_target"], s["plan_revisions"]), (2, 2, 1, 1))
        self.assertEqual(s["calls"], {"debug_start": 2, "inspect": 1, "remove_breakpoint": 1, "resume": 3,
                                      "set_breakpoint": 2})
        self.assertEqual([e for _, e in D.expressions(rows)], ["x > 1", "this.items.size()"])

    def test_escape_check_reads_debugger_expressions(self):
        rows = [{"tool": "inspect", "args": {"expr": "java.nio.file.Files.readString(p)"}},
                {"tool": "inspect", "args": {"expr": "this.seen.get(0)"}},
                {"tool": "set_breakpoint", "args": {"condition": "Runtime.getRuntime() != null"}},
                {"tool": "inspect", "args": {"expr": "this.getClass().classLoader.ucp.path"}},
                {"tool": "inspect", "args": {"expr": "this.files.size() + this.runtime.ok"}},
                {"tool": "inspect", "args": {"expr": "this.loader.parent.name"}}]
        found = X.check_debugger(rows)
        self.assertEqual([f["where"] for f in found],
                         ["debug-log 1 (inspect)", "debug-log 3 (set_breakpoint)", "debug-log 4 (inspect)"])
        self.assertEqual(X.finding_class(found[0]["kind"]), "escape")

    def test_ports_are_leased_once(self):
        """Two configs with different work directories share the lock files: no port is handed out twice."""
        lo = free_test_port()
        rng = f"{lo}-{lo + 1}"
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KEVAL_PORT_LOCKS": d + "/locks"}):
            (Path(d) / "a").mkdir()
            (Path(d) / "b").mkdir()
            one = load_config(write_config(Path(d) / "a", paths={"java_home": d, "results": d + "/r1", "work": d + "/w1",
                                                                  "debug_ports": rng}))
            two = load_config(write_config(Path(d) / "b", paths={"java_home": d, "results": d + "/r2", "work": d + "/w2",
                                                                  "debug_ports": rng}))
            with D.lease_port(one) as a, D.lease_port(two) as b:
                self.assertNotEqual(a, b)
                self.assertTrue({a, b} <= {lo, lo + 1})

    def test_server_source_is_attach_only(self):
        src = D.SERVER_SRC.read_text()
        self.assertIn("be84732", src)
        for gone in ("redefineClasses", "\"launch\"", "ProcessBuilder", "StepRequest", "invokeStaticMethod"):
            self.assertNotIn(gone, src)
        self.assertIn("allowedCall", src)


class RunLimitConfigTest(unittest.TestCase):
    def test_debug_runs_enters_the_hash_of_the_autonomous_mode_only(self):
        from evaluation import inputs as I
        case = load_case("klaw-util-methods-get-user-name")
        with tempfile.TemporaryDirectory() as d:
            three = load_config(write_config(Path(d)))
            self.assertEqual(three.debug_runs, 3)
        with tempfile.TemporaryDirectory() as d:
            five = load_config(write_config(Path(d), debug_runs=5))
        self.assertEqual(I.generation_inputs(three, case, "baseline")["config"],
                         I.generation_inputs(five, case, "baseline")["config"])
        self.assertNotEqual(I.generation_inputs(three, case, "autonomous")["config"],
                            I.generation_inputs(five, case, "autonomous")["config"])

class DebuggerHelpersTest(unittest.TestCase):
    def test_line_matching_frames_values(self):
        out = node_run()
        self.assertEqual(out["roots"], ["core/src/main/java", "core/src/test/java", "jgit.test/tst"])
        self.assertEqual(out["unique"], {"line": 7, "text": "int g() {"})
        self.assertEqual(out["trimmed"]["line"], 4)
        self.assertIn("lines 5, 8", out["duplicate"]["error"])
        self.assertIn("with line", out["duplicate"]["error"])
        self.assertEqual(out["duplicateWithLine"]["line"], 8)
        self.assertIn("the code is at lines 5, 8", out["wrongLine"]["error"])
        self.assertIn("no line", out["missing"]["error"])
        self.assertEqual(out["partial"]["line"], 10)
        self.assertIn("200", out["tooLong"]["error"])
        self.assertIn("single source line", out["multiLine"]["error"])
        self.assertEqual(out["className"], "a.b.X")
        self.assertEqual(out["method"]["argTypes"], ["int", "java.lang.String"])
        self.assertIsNone(out["methodAny"]["argTypes"])
        self.assertEqual(out["methodGeneric"]["argTypes"], ["java.util.Map", "int", "String[]"])
        self.assertEqual(out["ctor"]["method"], "<init>")
        self.assertIn("error", out["badMethod"])
        self.assertEqual(out["frames"], {"shown": ["T.m:3", "U.u0:0", "U.u1:1", "U.u2:2", "U.u3:3"], "more": 3})
        v = out["values"]
        self.assertEqual((v["prim"], v["char"], v["null"]), ("5", "\x27(\x27", "null"))
        self.assertEqual(v["str"], "\"a\\\"b\\n\"")
        self.assertIn("3 of 5000 characters", v["cutStr"])
        self.assertEqual(v["list"], "ArrayList@9(size 25) [\"x\", …+24 more]")
        self.assertEqual(v["obj"], "T@3 {n=1, o=U@4, …+2 more fields}")
        self.assertLessEqual(out["fit"]["length"], 8000)
        self.assertIn("this = T@3 {n=1}", out["stopLocals"])
        self.assertIn("u = U@4 {\n    s=\"zzz", out["stopLocals"])
        self.assertIn("this = T@3 {n=1}", out["stopLocalsCapped"])
        self.assertIn("u = U@4\n", out["stopLocalsCapped"])
        self.assertEqual(out["outside"], [True, True, True, False, False, True])


class DebuggerIntegrationTest(unittest.TestCase):
    """The five tools on tests/debugger/sample: line, entry and exit breakpoints, the exit by an
    exception, inspect with the method whitelist, a second run, the JVM stopped before bash."""

    def test_tools_on_the_sample_project(self):
        java_home = Path(os.environ.get("JAVA_HOME", ""))
        m2 = Path(os.environ.get("KEVAL_M2", Path.home() / ".m2" / "repository"))
        if not (java_home / "bin" / "javac").is_file() or shutil.which("mvn") is None or not m2.is_dir():
            self.skipTest("JDK 21, Maven and a Maven repository with JUnit 5.10.3 are needed")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "ws"
            shutil.copytree(ROOT / "tests" / "debugger" / "sample", ws)
            classes = tmp / "server"
            classes.mkdir()
            subprocess.run([str(java_home / "bin" / "javac"), "-Xlint:-options", "-d", str(classes), str(D.SERVER_SRC)],
                           check=True)
            agent = tmp / "agent"
            agent.mkdir()
            config = agent / "debugger.json"
            config.write_text(json.dumps({
                "workspace": str(ws), "java": str(java_home / "bin" / "java"), "server_classes": str(classes),
                "scenario": ["mvn", "-q", "-o", f"-Dmaven.repo.local={m2}", "-Dtest=CounterScenarioTest", "test"],
                "port": free_test_port(), "command_env": ["PATH", "JAVA_HOME", "HOME"], "start_timeout": 180, "debug_runs": 3,
                "scenario_log_dir": str(agent), "target": {"class": "demo.Counter", "method": "add"}}))
            os.environ["DEBUGGER_LOG"] = str(agent / "log.jsonl")
            os.environ["DEBUGGER_PIDS"] = str(agent / "pids")
            try:
                out = node_run(str(config), timeout=600)
            finally:
                os.environ.pop("DEBUGGER_LOG")
                os.environ.pop("DEBUGGER_PIDS")
            journal = D.read_journal(agent / "log.jsonl")
        self.assertEqual(out["tools"], list(A.DEBUGGER_TOOLS))
        self.assertIn("call debug_start", out["noSession"])
        self.assertIn("Counter.java:15", out["bpLine"])
        self.assertIn("call set_breakpoint again with line", out["bpDuplicate"])
        self.assertIn("run #1", out["start"])
        self.assertIn("stopped at breakpoint #1 (hit 1)", out["stop1"])
        self.assertIn("word = \"hello\"", out["stop1"])
        self.assertIn("this = Counter@", out["stop1"])
        self.assertIn("total=0", out["stop1"])
        self.assertIn("total=0", out["inspectThis"])
        self.assertEqual(out["inspectSize"], "1")
        self.assertIn("could run code of the program", out["inspectProjectCall"])
        self.assertIn("c = Counter@", out["inspectFrame1"])
        self.assertIn("stopped at exit #2 (hit 1)", out["stop2"])
        self.assertIn("returned = 5", out["stop2"])
        self.assertIn("returned = 10", out["stop4"])
        self.assertIn("stopped at entry #3", out["stop5"])
        self.assertIn("word = null", out["stop5"])
        self.assertIn("exception java.lang.IllegalArgumentException: \"word is null\" thrown out of demo.Counter#add",
                      out["stop6"])
        self.assertIn("terminated: run #1 ended, 6 stops", out["stop7"])
        self.assertIn("run #2", out["restart"])
        self.assertIn("returned = 5", out["stopAfterRestart"])
        self.assertIn("stopped at entry #4 (hit 1) of demo.Counter#twice", out["stopStatic"])
        self.assertIn("x = 2", out["stopStatic"])
        self.assertNotIn("this =", out["stopStatic"])
        self.assertEqual(out["inspectStaticThis"], "static method: no this")
        self.assertNotIn("overloads", out["bpBridge"])
        self.assertIn("stopped at exit #5 (hit 1) of demo.Square#copy", out["stopBridge"])
        self.assertIn("returned = Square@", out["stopBridge"])
        self.assertIn("call debug_start", out["afterBash"])
        self.assertIn("run #3", out["third"])
        self.assertEqual(out["refused"], "limit of 3 scenario runs reached; write the test from the data you have")
        self.assertIn("At most 3 runs", out["description"])
        self.assertIn("bash command ends the debug JVM", out["description"])
        s = D.journal_summary(journal, 3)
        self.assertEqual((s["jvm_runs"], s["stops"], s["stops_in_target"]), (3, 9, 6))
        self.assertEqual((s["run_limit"], s["refused_starts"]), (3, 1))
        self.assertTrue(any(isinstance(r.get("event"), dict) and r["event"].get("kind") == "start_refused"
                            for r in journal))
        self.assertTrue(any(r.get("event") == "run_end" and r.get("reason") == "bash" for r in journal))
        for answer in out.values():
            if isinstance(answer, str):
                self.assertLess(len(answer), 8500)

    def test_no_code_of_the_program_runs(self):
        """Stops and inspect on containers of project keys (Tag prints a marker from hashCode, equals,
        toString and compareTo once armed): the scenario log must show no marker."""
        out, logs = self.run_sample("ShelfScenarioTest", {"DEBUGGER_SHELF": "1"},
                                    {"class": "demo.Shelf", "method": "fixedSize"})
        self.assertIn("SCENARIO_DONE", logs)
        self.assertNotIn("PROJECT_", logs)
        self.assertIn("stopped at breakpoint #1", out["stop"])
        self.assertIn("counts=HashMap@", out["inspectThis"])
        self.assertIn("name=\"x\"", out["inspectThis"])
        self.assertEqual(out["countsSize"], "2")
        self.assertIn("Tag@", out["byNameGet"])
        self.assertEqual(out["plainGet"], "1")
        self.assertEqual(out["plainHas"], "false")
        self.assertIn("Tag@", out["orderGet"])
        self.assertEqual(out["fixedGet"], "\"c\"")
        self.assertEqual((out["tagsSize"], out["sortedSize"]), ("2", "2"))
        for refused in ("tagsContains", "countsGet", "tagToString"):
            self.assertIn("could run code of the program", out[refused])
        self.assertEqual(out["tagEquals"], "false")
        self.assertRegex(out["concat"], r'^"t=demo\.Tag@\d+"$')
        self.assertRegex(out["last"], r"AtomicReference@\d+\(Tag@\d+")
        self.assertRegex(out["first"], r"Optional@\d+\(Tag@\d+ \{name=\"x\"\}\)")
        for refused in ("loaderField", "classLoader", "threadName"):
            self.assertIn("are not shown", out[refused])
        self.assertRegex(out["ownerShown"], r"^Thread@\d+$")
        self.assertEqual(out["outsideExisting"], out["outsideMissing"])
        self.assertEqual(out["outsideExisting"], out["outsideRelative"])
        self.assertIn("restricted to files in the workspace", out["outsideExisting"])
        self.assertIn("terminated", out["end"])

    def run_sample(self, test_class: str, env: dict[str, str], target: dict[str, str]) -> tuple[dict, str]:
        java_home = Path(os.environ.get("JAVA_HOME", ""))
        m2 = Path(os.environ.get("KEVAL_M2", Path.home() / ".m2" / "repository"))
        if not (java_home / "bin" / "javac").is_file() or shutil.which("mvn") is None or not m2.is_dir():
            self.skipTest("JDK 21, Maven and a Maven repository with JUnit 5.10.3 are needed")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "ws"
            shutil.copytree(ROOT / "tests" / "debugger" / "sample", ws)
            classes = tmp / "server"
            classes.mkdir()
            subprocess.run([str(java_home / "bin" / "javac"), "-Xlint:-options", "-d", str(classes), str(D.SERVER_SRC)],
                           check=True)
            agent = tmp / "agent"
            agent.mkdir()
            config = agent / "debugger.json"
            config.write_text(json.dumps({
                "workspace": str(ws), "java": str(java_home / "bin" / "java"), "server_classes": str(classes),
                "scenario": ["mvn", "-q", "-o", f"-Dmaven.repo.local={m2}", f"-Dtest={test_class}", "test"],
                "port": free_test_port(), "command_env": ["PATH", "JAVA_HOME", "HOME"], "start_timeout": 180,
                "debug_runs": 3, "scenario_log_dir": str(agent), "target": target}))
            saved = {k: os.environ.get(k) for k in [*env, "DEBUGGER_LOG", "DEBUGGER_PIDS"]}
            os.environ.update(env, DEBUGGER_LOG=str(agent / "log.jsonl"), DEBUGGER_PIDS=str(agent / "pids"))
            try:
                out = node_run(str(config), timeout=600)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            logs = "".join(f.read_text(errors="replace") for f in sorted(agent.glob("scenario-debug-start*.log")))
        return out, logs


if __name__ == "__main__":
    unittest.main()
