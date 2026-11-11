"""The komar mode: the command forms of the guard (Python and TypeScript agree), the wrapper (run limit,
spec errors, journal), the prompt and the wiring of the mode, the hashes of the other modes.

The guard check needs node and an installed Pi package (as tests/test_runner.py); the integration check
needs Komar installed (scripts/setup_komar.sh, KEVAL_KOMAR or <KEVAL_TOOLS>/komar) and JDK 21 with
javac. Missing tools skip the check."""
import json
import re
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from evaluation import inputs as I
from runner import agent as A
from runner import cell as G
from runner import escape_check as X
from runner import komar as K
from runner.cases import eval_command, load_case
from runner.config import AGENT_MODES, ROOT, load_config
from runner.prompts import render
from tests.test_runner import PiExtensionsTest, write_config

SCENARIO = ["mvn", "-q", "-o", "-Dtest=CacheTest#testplan1", "test"]
SCENARIO_TEXT = "mvn -q -o -Dtest=CacheTest#testplan1 test"

# (command, expected kind): the same table is checked against pi/guard.ts in test_guard_agrees
COMMANDS = [
    (f"komar run komar/a.yaml -o komar/a.jsonl -- {SCENARIO_TEXT}", "komar"),
    (f"komar  run komar/run-2.yaml -o komar/out.v2.jsonl --\t{SCENARIO_TEXT}", "komar"),
    (f"komar run 'komar/a.yaml' -o komar/a.jsonl -- mvn -q -o '-Dtest=CacheTest#testplan1' test", "komar"),
    (f"komar run komar/a.yaml -o komar/a.jsonl -- {SCENARIO_TEXT} -X", None),
    ("komar run komar/a.yaml -o komar/a.jsonl -- mvn -q -o test", None),
    (f"komar run ../a.yaml -o komar/a.jsonl -- {SCENARIO_TEXT}", None),
    (f"komar run komar/../x.yaml -o komar/a.jsonl -- {SCENARIO_TEXT}", None),
    (f"komar run komar/a.yaml -o /tmp/a.jsonl -- {SCENARIO_TEXT}", None),
    (f"komar run komar/a.yaml -o komar/a.jsonl -p 1234", None),
    (f"komar run komar/a.yaml -o komar/a.jsonl -- {SCENARIO_TEXT} > x", None),
    (f"komar run komar/a.yaml -o komar/a.jsonl -- {SCENARIO_TEXT}; cat x", None),
    (f"komar run komar/a.yaml -o komar/a.jsonl -- {SCENARIO_TEXT}\nls", None),
    ("jq -c 'select(.kind==\"hit\" and .probe==\"p\") | .what' komar/a.jsonl", "jq"),
    ("jq -r '.what | keys[]' komar/a.jsonl komar/b.jsonl", "jq"),
    ("jq -s 'map(select(.kind==\"hit\")) | group_by(.call) | length' komar/a.jsonl", "jq"),
    ("jq -c --arg p hc 'select(.probe==$p)' komar/a.jsonl", "jq"),
    ("jq -n '1+1'", "jq"),
    ("jq '.' komar/a.jsonl | head", None),
    ("jq '.' /etc/passwd", None),
    ("jq '.' komar/../../x.jsonl", None),
    ("jq '.' komar/a.yaml", None),
    ("jq -L /tmp 'import \"x\" as x; .' komar/a.jsonl", None),
    ("jq 'include \"x\"; .' komar/a.jsonl", None),
    ("jq --rawfile x /etc/passwd '$x' -n", None),
    ("jq -f prog.jq komar/a.jsonl", None),
    ("jq \"$(cat x)\" komar/a.jsonl", None),
    ("jq '.' komar/*.jsonl", None),
    ("jq '.'", None),
    ("cat komar/a.jsonl", None),
    (SCENARIO_TEXT, None),
]

# commands reading a view: "view" with compact (the komar mode), None without
VIEW_COMMANDS = [
    ("sed -n '1,40p' komar/a.view", "view"),
    ("sed -n 7p komar/run-2.v1.view", "view"),
    ("sed -n \"3,3p\" komar/a.view", "view"),
    ("cut -c 1-20000 komar/a.view", "view"),
    ("cut -c 20001- komar/a.view", "view"),
    ("cut -c -500 komar/a.view", "view"),
    ("sed -n '1,40p' komar/a.jsonl", None),
    ("cut -c 1-100 komar/a.jsonl", None),
    ("sed -n '1,40p' komar/a.view komar/b.view", None),
    ("sed -n '1,40p' komar/a.view | head", None),
    ("sed -n '1,40p' komar/a.view > x", None),
    ("sed '1,40p' komar/a.view", None),
    ("sed -n '1,40p;w x' komar/a.view", None),
    ("sed -n 'e cat /etc/passwd' komar/a.view", None),
    ("sed -n '1,40p' komar/../src/A.view", None),
    ("sed -n '1,40p' /tmp/a.view", None),
    ("sed -n '1,40p' src/A.java", None),
    ("cut -f 1 komar/a.view", None),
    ("cut -c 1-20 komar/*.view", None),
]


# the compact view: (tool, workspace-relative path, grep glob, refused): no tool shows a dump komar/*.jsonl
COMPACT_READS = [
    ("read", "komar/a.jsonl", None, True),
    ("read", "komar/a.JSONL", None, True),
    ("grep", "komar/a.jsonl", None, True),
    ("read", "komar/a.view", None, False),
    ("read", "komar/a.yaml", None, False),
    ("grep", "komar", None, False),
    ("grep", "", None, False),
    ("grep", "", "*.java", False),
    ("grep", "komar", "*.view", False),
    ("grep", "", "!*.xml", False),
    ("grep", "", "*.jsonl", True),
    ("grep", "", "*", True),
    ("grep", "", "**", True),
    ("grep", "komar", "*.json?", True),
    ("grep", "", "komar/**", True),
    ("grep", "", "*.{jsonl,java}", True),
    ("grep", "src", "*", False),
    ("find", "komar", None, False),
    ("ls", "komar", None, False),
    ("read", "src/A.jsonl", None, False),
]


class CommandFormsTest(unittest.TestCase):
    def test_python_forms(self):
        for command, kind in COMMANDS:
            self.assertEqual(K.command_kind(command, SCENARIO), kind, command)
            self.assertEqual(K.command_kind(command, SCENARIO, compact=True), None if kind == "jq" else kind, command)
        for command, kind in VIEW_COMMANDS:
            self.assertEqual(K.command_kind(command, SCENARIO), None, command)
            self.assertEqual(K.command_kind(command, SCENARIO, compact=True), kind, command)

    def test_guard_agrees(self):
        pi = PiExtensionsTest.pi_package()
        if pi is None or shutil.which("node") is None:
            self.skipTest("node and an installed Pi package are needed")
        r = subprocess.run(["node", str(ROOT / "tests" / "node" / "komar.mjs"), str(pi), str(ROOT)],
                           input=json.dumps({"scenario": SCENARIO, "commands": [c for c, _ in COMMANDS],
                                             "views": [c for c, _ in VIEW_COMMANDS],
                                             "reads": [[t, p, g] for t, p, g, _ in COMPACT_READS]}),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout)
        self.assertEqual(out["kinds"], [k for _, k in COMMANDS])
        self.assertEqual(out["compactKinds"], [None if k == "jq" else k for _, k in COMMANDS])
        self.assertEqual(out["compactReads"], [refused for *_, refused in COMPACT_READS])
        self.assertEqual(out["viewKinds"], [None for _ in VIEW_COMMANDS])
        self.assertEqual(out["compactViewKinds"], [k for _, k in VIEW_COMMANDS])
        self.assertEqual(out["spec"], [True, True, False, False, False])
        self.assertEqual(out["timeouts"], [None, 120])


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.ws = d / "ws"
        (self.ws / "komar").mkdir(parents=True)
        self.agent = d / "agent"
        self.agent.mkdir()
        # a fake Komar: exit 2 for a spec containing "bad", else write an output like Komar's and exit 0
        fake = d / "komar-bin"
        fake.write_text("#!/bin/sh\n"
                        "grep -q bad \"$2\" && { echo 'komar: 1 errors, nothing started' >&2; exit 2; }\n"
                        "printf '%s\\n' '{\"kind\":\"run\"}' '{\"kind\":\"diag\",\"level\":\"info\"}' "
                        "'{\"kind\":\"hit\",\"probe\":\"p\"}' '{\"kind\":\"end\",\"reason\":\"exit\",\"code\":0,"
                        "\"hits\":{\"p\":1},\"dropped\":0,\"when_errors\":{}}' > \"$4\"\n")
        fake.chmod(0o755)
        self.config = self.agent / "komar.json"
        self.config.write_text(json.dumps({
            "workspace": str(self.ws), "binary": str(fake), "jar": str(d / "agent.jar"), "scenario": SCENARIO,
            "runs": 2, "log": str(self.agent / "komar-log.jsonl"), "state": str(self.agent / "komar-runs"),
            "pids": str(self.agent / "komar-pids.txt")}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_wrapper(self, name: str) -> int:
        argv = ["run", f"komar/{name}.yaml", "-o", f"komar/{name}.jsonl", "--", *SCENARIO]
        return K.wrapper(str(self.config), argv)

    def test_limit_spec_errors_and_journal(self):
        (self.ws / "komar" / "ok.yaml").write_text("probes: []\n")
        (self.ws / "komar" / "bad.yaml").write_text("probes: bad\n")
        (self.ws / "komar" / "calls.yaml").write_text("calls: true\nprobes: []\n")
        self.assertEqual(self.run_wrapper("bad"), 2)
        self.assertEqual(self.run_wrapper("calls"), 2)
        self.assertEqual(self.run_wrapper("ok"), 0)
        self.assertEqual(self.run_wrapper("ok"), 0)
        self.assertEqual(self.run_wrapper("ok"), 1)  # third run: refused
        self.assertEqual(K.wrapper(str(self.config), ["run", "komar/ok.yaml", "-o", "/tmp/x.jsonl", "--", *SCENARIO]), 2)
        rows = K.read_log(self.agent / "komar-log.jsonl")
        self.assertEqual([r["event"] for r in rows], ["spec_error", "spec_error", "run", "run", "refused", "rejected"])
        self.assertEqual(rows[1]["reason"], "calls")
        self.assertEqual(rows[2]["hits"], {"p": 1})
        self.assertEqual(rows[2]["diag"], {"info": 1})
        self.assertEqual(rows[2]["spec_text"], "probes: []\n")
        commands = [{"event": "end", "name": "jq", "succeeded": True}, {"event": "end", "name": "jq", "succeeded": False}]
        s = K.log_summary(rows, commands, 2)
        self.assertEqual((s["runs"], s["spec_errors"], s["refused"], s["hits"], s["jq_queries"], s["jq_failed"]),
                         (2, 2, 1, 2, 2, 1))


@unittest.skipUnless(Path("/proc").is_dir(), "needs /proc")
class WrapperProcessTreeTest(unittest.TestCase):
    """A fake Komar that, like Komar, starts the scenario in a process group of its own; the scenario
    sleeps. No process of the run may survive a timeout or a kill of the wrapper."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.ws = d / "ws"
        (self.ws / "komar").mkdir(parents=True)
        (self.ws / "komar" / "a.yaml").write_text("probes: []\n")
        self.agent = d / "agent"
        self.agent.mkdir()
        self.marker = f"komar-tree-test-{os.getpid()}-{id(self)}"
        fake = d / "komar-bin"
        scenario = d / self.marker
        scenario.write_text("while :; do sleep 1; done\n")
        fake.write_text(f"#!/bin/sh\nsetsid sh {scenario} &\nwait\n")
        fake.chmod(0o755)
        self.config = self.agent / "komar.json"
        self.config.write_text(json.dumps({
            "workspace": str(self.ws), "binary": str(fake), "jar": str(d / "agent.jar"), "scenario": SCENARIO,
            "runs": 3, "log": str(self.agent / "komar-log.jsonl"), "state": str(self.agent / "komar-runs"),
            "pids": str(self.agent / "komar-pids.txt"), "run_timeout": 3}))
        self.argv = ["run", "komar/a.yaml", "-o", "komar/a.jsonl", "--", *SCENARIO]

    def tearDown(self):
        subprocess.run(["pkill", "-KILL", "-f", self.marker])
        self.tmp.cleanup()

    def survivors(self) -> list[str]:
        r = subprocess.run(["pgrep", "-f", self.marker], capture_output=True, text=True)
        return r.stdout.split()

    def wait_for_scenario(self) -> None:
        for _ in range(100):
            if self.survivors():
                return
            time.sleep(0.1)
        self.fail("the fake scenario did not start")

    def wrapper_process(self) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-m", "runner.komar", str(self.config), *self.argv], cwd=ROOT,
                                env=dict(os.environ, PYTHONPATH=str(ROOT)), stderr=subprocess.PIPE, text=True)

    def test_timeout_kills_the_run_and_counts_it(self):
        p = self.wrapper_process()
        _, err = p.communicate(timeout=60)
        self.assertIn("Komar run stopped after 3 s; the scenario did not finish", err)
        time.sleep(0.5)
        self.assertEqual(self.survivors(), [])
        rows = K.read_log(self.agent / "komar-log.jsonl")
        self.assertEqual([(r["event"], r.get("timed_out")) for r in rows], [("run", True)])

    def test_killed_wrapper_leaves_no_process(self):
        for sig in (signal.SIGKILL, signal.SIGTERM):
            with self.subTest(sig=sig):
                p = self.wrapper_process()
                self.wait_for_scenario()
                p.send_signal(sig)
                p.wait(timeout=30)
                for _ in range(100):
                    if not self.survivors():
                        break
                    time.sleep(0.1)
                self.assertEqual(self.survivors(), [])

    def test_kill_leftovers_ends_a_running_run(self):
        p = self.wrapper_process()
        self.wait_for_scenario()
        time.sleep(1.5)  # the supervisor notes the groups of the run every 0.5 s
        K.kill_leftovers(self.agent / "komar-pids.txt")
        p.wait(timeout=30)
        for _ in range(50):
            if not self.survivors():
                break
            time.sleep(0.1)
        self.assertEqual(self.survivors(), [])


class ModeWiringTest(unittest.TestCase):
    def test_prompt(self):
        """task-komar.txt shares the system prompt and the common task with the other modes and describes the
        compact view, not jq."""
        case = load_case("mybatis-caching-executor-query")
        commands = {n: case.commands[n] for n in ("build", "scenario", "generated_test")}
        komar = render(ROOT / "prompts", case, "komar", commands)
        auto = render(ROOT / "prompts", case, "autonomous", commands)
        self.assertEqual(komar["system"], auto["system"])
        self.assertTrue(komar["task"].startswith(auto["task"][:auto["task"].index("Condition:")] + "Condition: Komar.\n"))
        self.assertIn("at most 3 runs in total.", komar["task"])
        self.assertNotIn("jq", komar["task"])
        self.assertIn("read tool from komar/NAME.view", komar["task"])
        scenario = K.scenario_text(eval_command(case, "scenario"))
        self.assertIn(f"komar run komar/NAME.yaml -o komar/NAME.jsonl -- {scenario}\n", komar["task"])

    def test_compact_prepare_excludes_the_dumps(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            inst = d / "k"
            (inst / "bin").mkdir(parents=True)
            for f in (inst / "bin" / "komar", inst / "komar-agent.jar"):
                f.write_text("")
            (inst / "versions.json").write_text(json.dumps({"commit": "0" * 40}))
            ws, agent = d / "ws", d / "agent"
            (ws / ".git" / "info").mkdir(parents=True)
            agent.mkdir()
            cfg = load_config(write_config(d, paths={"java_home": str(d), "komar": str(inst)}, komar_commit="0" * 40))
            K.prepare(cfg, ws, agent, SCENARIO)
            self.assertFalse((ws / ".git" / "info" / "exclude").exists())
            K.prepare(cfg, ws, agent, SCENARIO, compact=True)
            self.assertIn("/komar/*.jsonl", (ws / ".git" / "info" / "exclude").read_text())

    def test_scenario_of_the_prompt_is_accepted(self):
        for case_id in ("mybatis-caching-executor-query", "klaw-envs-clusters-tenants-controller-service-add-new-env",
                        "petclinic-owner-get-pet"):
            case = load_case(case_id)
            scenario = G.komar_input(case)["scenario"]
            text = K.scenario_text(scenario)
            self.assertEqual(K.command_kind(f"komar run komar/a.yaml -o komar/a.jsonl -- {text}", scenario), "komar")

    def test_mode_and_defaults(self):
        self.assertEqual(AGENT_MODES, ("baseline", "autonomous", "komar"))
        self.assertEqual(G.komar_input(load_case("mybatis-caching-executor-query"))["compact"], True)
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(write_config(Path(d), paths={"java_home": d, "pi": "/bin/echo", "results": d + "/r"}))
            self.assertEqual(cfg.komar_dir, cfg.tools_dir / "komar")
            argv = A.pi_argv(cfg, {"system": "S", "task": "T"}, Path(d))
            self.assertEqual(argv[argv.index("-t") + 1].split(","), list(A.BUILTIN_TOOLS))
            with self.assertRaisesRegex(K.KomarError, "setup_komar.sh"):
                K.installation(cfg)

    def test_inputs(self):
        """The komar cells hash the installed Komar, their prompt templates and the run limit."""
        with tempfile.TemporaryDirectory() as d:
            case = load_case("jsoup-element-has-class")
            three = load_config(write_config(Path(d), paths={"java_home": d, "komar": d + "/k"}))
            five = load_config(write_config(Path(d), debug_runs=5, paths={"java_home": d, "komar": d + "/k"}))
            k = I.generation_inputs(three, case, "komar")
            self.assertNotEqual(k["prompts"], I.generation_inputs(three, case, "autonomous")["prompts"])
            self.assertIsNone(I.generation_inputs(three, case, "baseline")["mode_input"])
            self.assertNotEqual(k["config"], I.generation_inputs(five, case, "komar")["config"])
            (Path(d) / "k").mkdir()
            (Path(d) / "k" / "versions.json").write_text('{"commit": "x"}')
            self.assertNotEqual(I.generation_inputs(three, case, "komar")["mode_input"], k["mode_input"])

    def test_installed_commit_must_match_the_config(self):
        with tempfile.TemporaryDirectory() as d:
            inst = Path(d) / "k"
            (inst / "bin").mkdir(parents=True)
            (inst / "bin" / "komar").write_text("")
            (inst / "komar-agent.jar").write_text("")
            (inst / "versions.json").write_text(json.dumps({"commit": "0" * 40}))
            cfg = load_config(write_config(Path(d), paths={"java_home": d, "komar": str(inst)}))
            with self.assertRaisesRegex(K.KomarError, "built from"):
                K.installation(cfg)
            cfg.komar_commit = "0" * 40
            self.assertEqual(K.installation(cfg)["binary"], inst / "bin" / "komar")

    def test_missing_komar_is_an_infrastructure_failure(self):
        s = {"status": "failed", "phase": "agent", "error": "KomarError: Komar is not installed"}
        self.assertTrue(G.is_infra_failure(s, Path("/nonexistent")))


class EscapeCheckTest(unittest.TestCase):
    def test_view_commands_are_plain_commands_in_the_komar_mode(self):
        calls = [{"name": "bash", "arguments": {"command": "sed -n '1,40p' komar/a.view"}}]
        self.assertEqual(X.check_tool_calls(calls, [], "/w", komar_mode=True), [])
        bad = [{"name": "bash", "arguments": {"command": "jq . komar/a.jsonl | sh"}}]
        self.assertEqual([f["kind"] for f in X.check_tool_calls(bad, [], "/w", komar_mode=True)],
                         ["shell syntax in a command"])

    def test_test_reading_the_observations_is_an_escape(self):
        found = X.check_code('String s = Files.readString(Path.of("komar/a.jsonl"));', "T.java")
        self.assertIn("reads mode input", [f["kind"] for f in found])


def komar_installation() -> Path | None:
    for c in (os.environ.get("KEVAL_KOMAR"),
              os.environ.get("KEVAL_TOOLS") and str(Path(os.environ["KEVAL_TOOLS"]) / "komar")):
        if c and (Path(c) / "bin" / "komar").is_file():
            return Path(c)
    return None


class ViewNameTest(unittest.TestCase):
    def test_view_regexes_accept_view_and_reject_other_suffixes(self):
        self.assertIsNotNone(K.VIEW.match("komar/x.view"))
        self.assertIsNone(K.VIEW.match("komar/x.komar"))
        self.assertIsNone(K.VIEW.match("komar/x.jsonl"))
        guard = (ROOT / "pi" / "guard.ts").read_text()
        self.assertIn("const KOMAR_VIEW = new RegExp(`^komar/${KOMAR_NAME}\\\\.view$`);", guard)


class KomarIntegrationTest(unittest.TestCase):
    def test_wrapper_runs_komar_on_a_java_program(self):
        """The installed Komar under the wrapper: a probe of a sample class compiled with -g; the agent jar
        comes from the installation (KOMAR_AGENT_JAR), the output summary reaches the journal."""
        inst = komar_installation()
        java_home = Path(os.environ.get("JAVA_HOME", ""))
        if inst is None or not (java_home / "bin" / "javac").is_file():
            self.skipTest("an installed Komar (KEVAL_KOMAR) and JAVA_HOME with javac are needed")
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            ws, agent = d / "ws", d / "agent"
            (ws / "komar").mkdir(parents=True)
            agent.mkdir()
            (ws / "A.java").write_text("public class A {\n  int n = 2;\n  int twice(int x) {\n    int y = x * n;\n"
                                       "    return y;\n  }\n  public static void main(String[] a) {\n"
                                       "    System.out.println(new A().twice(21));\n  }\n}\n")
            subprocess.run([str(java_home / "bin" / "javac"), "-g", "A.java"], cwd=ws, check=True)
            scenario = [str(java_home / "bin" / "java"), "-cp", ".", "A"]
            (ws / "komar" / "s.yaml").write_text(
                "probes:\n  - id: m\n    where: { class: A, method: twice, at: [entry, exit] }\n    what: [this, args, ret]\n"
                "  - id: l\n    where: { class: A, method: twice, line: [5] }\n    what: [vars.y]\n")
            cfg_path = agent / "komar.json"
            cfg_path.write_text(json.dumps({
                "workspace": str(ws), "binary": str(inst / "bin" / "komar"), "jar": str(inst / "komar-agent.jar"),
                "scenario": scenario, "runs": 3, "log": str(agent / "log.jsonl"), "state": str(agent / "runs"),
                "pids": str(agent / "pids.txt")}))
            code = K.wrapper(str(cfg_path), ["run", "komar/s.yaml", "-o", "komar/s.jsonl", "--", *scenario])
            self.assertEqual(code, 0)
            rows = [json.loads(line) for line in (ws / "komar" / "s.jsonl").read_text().splitlines()]
            hits = [r for r in rows if r["kind"] == "hit"]
            exit_hit = next(h for h in hits if h.get("point") == "exit")
            self.assertEqual(exit_hit["what"]["ret"], 42)
            self.assertEqual(exit_hit["what"]["this"]["n"], 2)
            self.assertEqual(next(h for h in hits if h["probe"] == "l")["what"]["vars.y"], 42)
            log = K.read_log(agent / "log.jsonl")
            self.assertEqual(log[0]["hits"], {"m": 2, "l": 1})
            self.assertEqual(log[0]["end"]["code"], 0)
            # the view the prompt tells the agent to read is the file Komar writes, and it is accounted
            ext = re.search(r"komar/NAME\.(\w+), a compact view", (ROOT / "prompts" / "task-komar.txt").read_text()).group(1)
            self.assertTrue((ws / "komar" / f"s.{ext}").is_file(), f"Komar wrote no komar/s.{ext}")
            self.assertGreater(log[0].get("view_bytes", 0), 0)


if __name__ == "__main__":
    unittest.main()
