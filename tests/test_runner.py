"""Cases, prompts, Pi command line and the cell logic that does not need a project checkout."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from runner import agent as A
from runner import cell as G
from runner import workspace as W
from runner.cases import case_rows, load_case, load_projects
from runner.config import AGENT_MODES, ROOT, load_config
from runner.prompts import render


def write_config(tmp: Path, **extra) -> Path:
    data = {"provider": {"name": "p", "base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY"},
            "model": "m", "thinking": "medium",
            "paths": {"projects": str(tmp / "projects"), "results": str(tmp / "results"), "java_home": str(tmp)},
            **extra}
    path = tmp / "config.yaml"
    path.write_text(yaml.dump(data))
    return path


class CasesTest(unittest.TestCase):
    def test_every_case_loads_and_is_consistent(self):
        projects = load_projects()
        for case_list, n, jdk in (("cases.csv", 70, 21), ("cases-jdk11.csv", 31, 11)):
            rows = case_rows(case_list=case_list)
            self.assertEqual(len(rows), n)
            for row in rows:
                case = load_case(row["id"])
                self.assertEqual((case.project, case.group), (row["project"], row["group"]))
                self.assertEqual(projects[case.project].jdk, jdk, case.id)
                test_class = case.scenario_test.split("#")[0].rsplit(".", 1)[-1]
                self.assertEqual(Path(case.scenario_source).stem, test_class, case.id)
                self.assertIn(case.group, ("direct", "indirect"))
                self.assertTrue((ROOT / "references" / case.id / "run1.json").is_file(), case.id)
        self.assertEqual({p for p, v in projects.items() if v.prebuild}, {"lab-insurance"})

    def test_case_list_of_the_config(self):
        from runner.cases import case_ids
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(write_config(Path(d), case_list="cases-jdk11.csv"))
            self.assertEqual(len(case_ids(cfg)), 31)
            self.assertEqual(len(case_ids(load_config(write_config(Path(d))))), 70)
            with self.assertRaises(Exception):
                load_config(write_config(Path(d), case_list="../cases.csv"))

    def test_check_jdk(self):
        from runner import cases as CS
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(write_config(Path(d), jdk=11))
            with mock.patch.object(CS, "java_major", lambda _h: 11), mock.patch.dict(os.environ, {"JAVA_HOME": d}):
                CS.check_jdk(cfg, ["pdfbox-catalog-get-viewer-preferences"])
                with self.assertRaisesRegex(CS.CaseError, "jsoup needs JDK 21"):
                    CS.check_jdk(cfg, ["jsoup-normalise-whitespace"])
            with mock.patch.object(CS, "java_major", lambda _h: 21):
                with self.assertRaisesRegex(CS.CaseError, "is JDK 21"):
                    CS.check_jdk(cfg, ["pdfbox-catalog-get-viewer-preferences"])

    def test_repository_has_no_local_paths(self):
        pattern = re.compile(r"/Users/|/home/|/private/var/|/var/folders/|\.\./dataset")
        for path in (ROOT / "cases").rglob("*.yaml"):
            self.assertIsNone(pattern.search(path.read_text()), path)


class PromptsTest(unittest.TestCase):
    def test_neutral_scenario(self):
        case = load_case("mybatis-caching-executor-query")
        commands = {n: case.commands[n] for n in ("build", "scenario", "generated_test")}
        for mode in AGENT_MODES:
            p = render(ROOT / "prompts", case, mode, commands)
            self.assertIn("Driving scenario: test org.apache.ibatis.submitted.cache.CacheTest#testplan1\n", p["task"])
            self.assertIn("Scenario test source: src/test/java/org/apache/ibatis/submitted/cache/CacheTest.java\n",
                          p["task"])
            self.assertIn("Repository: mybatis-3 at commit ", p["task"])
            self.assertNotIn("{{", p["task"] + p["system"])

    def test_effective_system_prompt_matches_pi(self):
        self.assertEqual(A.effective_system_prompt("S", Path("/w/x")), "S\nCurrent working directory: /w/x")

    def test_baseline_block_states_only_the_absence_of_data(self):
        text = (ROOT / "prompts" / "task-baseline.txt").read_text()
        self.assertEqual(text, "Condition: source code only.\n\nNo execution data is provided.\n")


class ConfigAndPiTest(unittest.TestCase):
    def test_config_defaults_and_pi_argv(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = load_config(write_config(tmp, paths={"java_home": d, "pi": "/bin/echo",
                                                        "results": str(tmp / "r")}))
            self.assertEqual((cfg.timeout, cfg.rounds, cfg.run_id(2)), (600, [1], "r2"))
            argv = A.pi_argv(cfg, {"system": "S", "task": "T"}, tmp / "s")
            for flag in ("--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--provider") + 1], "p")
            self.assertEqual(argv[-1], "T")
            self.assertTrue(argv[argv.index("-e") + 1].endswith("pi/provider.ts"))

    def test_agent_env_passes_only_the_selected_key(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(write_config(Path(d)))
            os.environ["TEST_KEY"] = "secret"
            try:
                env = A.agent_env(cfg, "TEST_KEY")
            finally:
                os.environ.pop("TEST_KEY")
            self.assertEqual(env["KEVAL_API_KEY"], "secret")
            self.assertNotIn("TEST_KEY", env)
            self.assertEqual(env["KEVAL_BASE_URL"], "https://example.invalid/v1")

    def test_command_env_is_an_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(write_config(Path(d), env={"MAVEN_OPTS": "-Xmx1g"}))
            saved = dict(os.environ)
            os.environ.update({"TEST_KEY": "secret", "OPENAI_API_KEY": "s2", "AWS_SECRET_ACCESS_KEY": "s3",
                               "LC_ALL": "C", "KEVAL_API_KEY": "s4"})
            try:
                env = cfg.command_env()
            finally:
                os.environ.clear()
                os.environ.update(saved)
            for name in ("TEST_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "KEVAL_API_KEY"):
                self.assertNotIn(name, env)
            self.assertEqual(env["LC_ALL"], "C")
            self.assertEqual(env["MAVEN_OPTS"], "-Xmx1g")
            self.assertTrue(env["PATH"].startswith(str(Path(d) / "bin")))
            self.assertNotIn("secret", json.dumps(env))

    def test_work_dir_outside_results_and_repository(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(write_config(Path(d)))
            work = cfg.work_dir
            self.assertNotIn(cfg.results_dir, [work, *work.parents])
            self.assertNotIn(ROOT, [work, *work.parents])
            from runner.config import ConfigError
            with self.assertRaises(ConfigError):
                load_config(write_config(Path(d), paths={"java_home": d, "results": str(Path(d) / "r"),
                                                         "work": str(Path(d) / "r" / "w")}))

    def test_neutral_directory_names(self):
        from runner.config import neutral_name
        name = neutral_name("jsoup-normalise-whitespace", "komar", "r1")
        self.assertRegex(name, r"^w[0-9a-f]{12}$")
        self.assertNotEqual(name, neutral_name("jsoup-normalise-whitespace", "baseline", "r1"))


class CellLogicTest(unittest.TestCase):
    def test_eval_commands_skip_format_checks_agent_commands_unchanged(self):
        from runner.cases import eval_command
        case = load_case("klaw-util-methods-get-user-name")
        for name in ("build", "generated_test", "scenario"):
            argv = eval_command(case, name)
            self.assertEqual(argv[0], case.commands[name][0])
            self.assertIn("-Dspotless.check.skip=true", argv)
            self.assertNotIn("-Dspotless.check.skip=true", case.commands[name])
            self.assertEqual([a for a in argv if not a.startswith("-Dspotless")], case.commands[name])
        jsoup = load_case("jsoup-normalise-whitespace")
        self.assertEqual(eval_command(jsoup, "build"), jsoup.commands["build"])
        self.assertFalse(hasattr(G, "spotless_apply_argv"))

    def test_infrastructure_failures(self):
        cell = Path("/nonexistent")
        self.assertTrue(G.is_infra_failure({"status": "failed", "ended_on_gateway_error": True}, cell))
        self.assertTrue(G.is_infra_failure({"status": "timed_out", "gateway_errors": 3}, cell))
        self.assertFalse(G.is_infra_failure({"status": "timed_out", "gateway_errors": 1}, cell))
        self.assertTrue(G.is_infra_failure({"status": "failed", "phase": "scenario-check"}, cell))
        self.assertFalse(G.is_infra_failure({"status": "failed", "phase": "agent", "tool_calls": 5,
                                             "agent_exit_code": 1}, cell))
        self.assertFalse(G.is_infra_failure({"status": "completed"}, cell))

    def test_provider_client_errors_before_any_tool_call_are_infrastructure(self):
        # a gateway that lists a model but does not serve it answers 400 invalid_request_error; Pi exits 0
        # with one turn and no tool calls
        err = {"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage":
               '400: {"message":"Invalid request.","type":"invalid_request_error"}'}}
        self.assertEqual(A.client_errors([err, err, err]), (3, True, False))
        self.assertEqual(A.gateway_errors([err]), (0, False))
        auth = {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                    "errorMessage": "401: invalid api key"}}
        self.assertEqual(A.client_errors([auth]), (1, True, True))
        rate = {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                    "errorMessage": "429 rate limit"}}
        self.assertEqual(A.client_errors([rate]), (0, False, False))
        cell = Path("/nonexistent")
        failed = {"status": "failed", "phase": "done", "error": "the agent left no test class", "agent_exit_code": 0,
                  "tool_calls": 0, "usage": {"turns": 1}, "gateway_errors": 0, "ended_on_gateway_error": False,
                  "ended_on_provider_client_error": True}
        self.assertTrue(G.is_infra_failure(failed, cell))
        # after tool calls a 4xx may come from the session itself (too long a context): the cell's failure
        self.assertFalse(G.is_infra_failure({**failed, "tool_calls": 7}, cell))
        self.assertFalse(G.is_infra_failure({**failed, "ended_on_provider_client_error": False}, cell))
        # summaries written before the field existed keep their meaning
        old = {k: v for k, v in failed.items() if k != "ended_on_provider_client_error"}
        self.assertFalse(G.is_infra_failure(old, cell))

    def test_model_probe_gates_the_run(self):
        import importlib.util
        import io
        import urllib.error
        spec = importlib.util.spec_from_file_location("run_script", ROOT / "scripts" / "run.py")
        run_script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_script)
        cfg = mock.Mock(model="m", base_url="https://gw/v1", api_key_env="K1", fallback_api_key_env="K2")

        class Answer:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def refuse(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"error":"invalid sk-secret"}'))
        with mock.patch.dict(os.environ, {"K1": "sk-secret", "K2": "sk-other"}):
            with mock.patch("urllib.request.urlopen", side_effect=refuse):
                ok, notes = run_script.probe_model(cfg)
            self.assertFalse(ok)
            self.assertIn("K1: 400", notes)
            self.assertNotIn("sk-secret", notes)
            with mock.patch("urllib.request.urlopen", side_effect=[refuse, lambda *a, **k: None][0]) as m:
                m.side_effect = [urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"busy")), Answer()]
                ok, notes = run_script.probe_model(cfg)
            self.assertTrue(ok)
            self.assertIn("K2: 200", notes)
        with mock.patch.dict(os.environ, {}, clear=True):
            ok, notes = run_script.probe_model(cfg)
            self.assertFalse(ok)
            self.assertIn("K1: not set", notes)

    def test_provider_unreachable_before_first_turn(self):
        """Pi could not reach the provider: stored with phase "done", 0 turns."""
        real = ('[provider] failed to fetch models: fetch failed\n'
                'Error: Unknown provider "gateway". Use --list-models to see available providers/models.\n')
        failed = {"status": "failed", "phase": "done", "error": "agent exited with code 1",
                  "agent_exit_code": 1, "tool_calls": 0, "usage": {"turns": 0}, "gateway_errors": 0}
        with tempfile.TemporaryDirectory() as d:
            cell = Path(d)
            (cell / "artifacts").mkdir()
            stderr = cell / "artifacts" / "pi-stderr.txt"
            stderr.write_text(real)
            self.assertTrue(G.is_infra_failure(failed, cell))
            self.assertTrue(G.is_infra_failure({**failed, "phase": "result"}, cell))
            stderr.write_text("[provider] failed to fetch models (attempt 4/4): fetch failed\n"
                              "[provider] model list unavailable: https://h/v1/models failed 4 times (fetch failed)\n")
            self.assertTrue(G.is_infra_failure(failed, cell))
            stderr.write_text(real)
            # the model answered: a failure of the model, not of the provider
            self.assertFalse(G.is_infra_failure({**failed, "usage": {"turns": 1}}, cell))
            self.assertFalse(G.is_infra_failure({**failed, "tool_calls": 2}, cell))
            self.assertFalse(G.is_infra_failure({**failed, "agent_exit_code": 0, "status": "failed",
                                                 "error": "the agent left no test class"}, cell))
            self.assertFalse(G.is_infra_failure({**failed, "status": "completed"}, cell))
            # Pi failed for another reason
            stderr.write_text("Error: Model \"x\" not found\n")
            self.assertFalse(G.is_infra_failure(failed, cell))
            stderr.unlink()
            self.assertFalse(G.is_infra_failure(failed, cell))

    def test_session_usage_and_gateway_errors(self):
        events = [
            {"type": "message_end", "message": {"role": "assistant", "usage": {"input": 10, "output": 2,
                                                                                "cost": {"total": 0.5}}}},
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                                "errorMessage": "502 upstream_empty_response"}},
        ]
        u = A.usage(events)
        self.assertEqual((u["turns"], u["input_tokens"], u["cost_usd"]), (2, 10, 0.5))
        self.assertEqual(A.gateway_errors(events), (1, True))

    def test_request_timeouts_mid_session_are_gateway_errors(self):
        # the gateway stops answering after five tool calls; Pi retries and gives up, the agent exits 0
        # without a test class
        ok = {"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse",
                                                "usage": {"input": 900, "output": 40}}}
        err = {"type": "message_end", "message": {
            "role": "assistant", "content": [], "api": "openai-completions", "provider": "gateway",
            "model": "OpenAI/gpt-6.1-sol", "usage": {"input": 0, "output": 0}, "stopReason": "error",
            "errorMessage": "Request timed out."}}
        events = [ok] * 5 + [err] * 12 + [{"type": "auto_retry_end", "success": False, "attempt": 3,
                                           "finalError": "Request timed out."}, {"type": "agent_settled"}]
        self.assertEqual(A.gateway_errors(events), (12, True))
        with tempfile.TemporaryDirectory() as d:
            s = {"status": "failed", "phase": "done", "error": "the agent left no test class",
                 "tool_calls": 5, "agent_exit_code": 0}
            s["gateway_errors"], s["ended_on_gateway_error"] = A.gateway_errors(events)
            self.assertTrue(G.is_infra_failure(s, Path(d)))
            # recovered: the session went on after the timeouts, so it did not end on them
            n, last = A.gateway_errors([ok, err, err, ok])
            self.assertEqual((n, last), (2, False))
        for text in ("Connection error.", "500 Internal Server Error", "read ECONNRESET", "connect ETIMEDOUT 1.2.3.4"):
            self.assertTrue(A.is_gateway_error(text), text)
        for text in ("Context window exceeded", "Invalid tool call", "model refused"):
            self.assertFalse(A.is_gateway_error(text), text)

    def test_final_failed_commands(self):
        entries = [{"event": "end", "name": "build", "succeeded": False},
                   {"event": "end", "name": "build", "succeeded": True},
                   {"event": "end", "name": "scenario", "succeeded": False}]
        self.assertEqual(G.final_failed_commands(entries), ["scenario"])


class WorkspaceTest(unittest.TestCase):
    def test_export_hide_and_diff(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            (repo / "src").mkdir()
            (repo / "src" / "A.java").write_text("class A {}\n")
            (repo / "src" / "ATest.java").write_text("class ATest {}\n")
            for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
                subprocess.run(["git", "-C", str(repo), *args], check=True)
            commit = W.git(repo, "rev-parse", "HEAD").stdout.strip()
            ws = W.export_commit(repo, commit, Path(d) / "ws")
            self.assertEqual(W.hide_paths(ws, ["src/ATest.java", "src/Missing.java"]), ["src/ATest.java"])
            before = W.snapshot(ws)
            (ws / "src" / "ACarvedTest.java").write_text("class ACarvedTest {}\n")
            (ws / "target").mkdir()
            (ws / "target" / "x.class").write_text("")
            self.assertEqual(W.unexpected_changes(before, W.snapshot(ws), {"src/ACarvedTest.java"}), [])
            diff = W.result_diff(repo, commit, {"src/ACarvedTest.java": ws / "src" / "ACarvedTest.java"},
                                 Path(d) / "result")
            self.assertIn("+class ACarvedTest {}", diff)


class PiExtensionsTest(unittest.TestCase):
    def test_guard_confines_reads_writes_and_commands(self):
        source = (ROOT / "pi" / "guard.ts").read_text()
        for tool in ('"read"', '"grep"', '"find"', '"ls"', '"write"', '"edit"', '"bash"'):
            self.assertIn(f"event.toolName === {tool}", source)
        self.assertIn("realpathSync", source)

    def test_provider_reads_only_environment(self):
        source = (ROOT / "pi" / "provider.ts").read_text()
        self.assertIn("KEVAL_BASE_URL", source)
        self.assertIn("delete process.env.KEVAL_API_KEY", source)
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("*", "/*", "//")))
        self.assertNotRegex(code, r"https?://")

    @staticmethod
    def pi_package() -> Path | None:
        import shutil
        import os
        candidates = [ROOT / ".tools" / "pi" / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent"]
        if os.environ.get("KEVAL_TOOLS"):  # paths.tools of a config outside the repository
            candidates.insert(0, Path(os.environ["KEVAL_TOOLS"]) / "pi" / "lib" / "node_modules"
                              / "@earendil-works" / "pi-coding-agent")
        pi = shutil.which("pi")
        if pi:
            p = Path(pi).resolve()
            for parent in p.parents:
                if (parent / "package.json").is_file() and parent.name == "pi-coding-agent":
                    candidates.append(parent)
                    break
        return next((c for c in candidates if (c / "node_modules" / "jiti").is_dir()), None)

    def test_guard_and_provider_behaviour(self):
        """The real extension code under node: command matching, environment of commands, key escaping."""
        import shutil
        pi = self.pi_package()
        if pi is None or shutil.which("node") is None:
            self.skipTest("node and an installed Pi package are needed")
        r = subprocess.run(["node", str(ROOT / "tests" / "node" / "extensions.mjs"), str(pi), str(ROOT)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        out = json.loads(r.stdout)
        self.assertEqual(out["exact"], "build")
        self.assertEqual(out["extraSpaces"], "build")
        self.assertEqual(out["quotedHash"], "scenario")
        self.assertEqual(out["unquotedHash"], "scenario")
        for bypass in ("bypassRedirect", "redirect", "background", "chained", "piped", "newline", "substitution"):
            self.assertIsNone(out[bypass], bypass)
        self.assertEqual(out["env"], {"PATH": "/bin", "HOME": "/h"})
        self.assertEqual(out["literal"], ["plain-key", "a$$B", "$!rm"])
        self.assertEqual(out["modelsRecovered"], {"ids": ["m"], "calls": 3})
        self.assertEqual(out["modelsDown"]["calls"], 4)
        self.assertIn("model list unavailable", out["modelsDown"]["error"])
        self.assertIn("fetch failed", out["modelsDown"]["error"])
        self.assertIn("empty model list", out["modelsEmpty"])
        self.assertEqual(out["retryDelays"], [2000, 5000, 10000])


if __name__ == "__main__":
    unittest.main()
