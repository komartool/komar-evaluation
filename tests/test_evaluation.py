"""Metrics: comparison levels (level B and the rule for dependencies), trace reading."""
import json
import unittest

from evaluation import compare as C
from evaluation import trace as T

ND = {"types": ["java.util.UUID"], "type_prefixes": ["org.h2."], "fields": {"*": ["birthTime"], "p": ["id"]}}


def inv(args, value="r", calls=("virtual int A#b()",), outcome="return", self=None):
    return {"self": self, "args": args, "calls": list(calls), "outcome": outcome, "value": value}


class CompareTest(unittest.TestCase):
    def test_identical_invocations_collapse(self):
        ref = [inv(["a"]), inv(["a"]), inv(["b"])]
        r = C.compare(ref, [inv(["a"])], ND, "p")
        self.assertEqual(r["scenarios"], 2)
        self.assertEqual(r["data_hits"], 1)
        self.assertEqual(r["structure_hits"], 2)

    def test_data_never_exceeds_structure(self):
        ref = [inv(["a"]), inv(["b"], calls=("virtual int A#c()",))]
        r = C.compare(ref, [inv(["x"]), inv(["b"], calls=("virtual int A#c()",))], ND, "p")
        self.assertLessEqual(r["data_hits"], r["structure_hits"])

    def test_not_run_counts_as_zero(self):
        r = C.compare([inv(["a"])], None, ND, "p")
        self.assertEqual(r["recall_data"], 0.0)
        self.assertEqual(r["recall_structure"], 0.0)

    def test_proxy_suffix_normalised_in_calls(self):
        a = inv(["a"], calls=("virtual int Repo$MockitoMock$123#find()",))
        b = inv(["a"], calls=("virtual int Repo$$EnhancerBySpringCGLIB$$abc#find()",))
        self.assertEqual(C.compare([a], [b], ND, "p")["data_hits"], 1)

    def test_masked_types_fields_and_strings(self):
        g1 = {"$t": "x.E", "$id": 1, "$fields": {"id": 5, "u": {"$t": "java.util.UUID", "$str": "1"},
                                              "db": {"$t": "org.h2.Session", "$id": 2}, "birthTime": 1,
                                              "msg": "at 2026-10-11 23:57:39 obj@1a2b3c"}}
        g2 = {"$t": "x.E", "$id": 7, "$fields": {"id": 9, "u": {"$t": "java.util.UUID", "$str": "2"},
                                              "db": {"$t": "org.h2.Session", "$id": 8}, "birthTime": 2,
                                              "msg": "at 2026-10-12 01:00:00 obj@ffee00"}}
        self.assertEqual(C.compare([inv([g1])], [inv([g2])], ND, "p")["data_hits"], 1)

    NO_ND = {"types": [], "fields": {}}

    @staticmethod
    def svc(dep, name):
        return {"$t": "x.Svc", "$id": 1, "$fields": {"dep": dep, "name": name}}

    def cmp(self, ref, cand, components=(), test_classes=()):
        return C.compare(ref, cand, self.NO_ND, "p", set(components), set(test_classes))

    def hits(self, r):
        return tuple(r[f"{lvl}_hits"] for lvl in C.LEVELS)

    def test_each_level(self):
        call = ("virtual int A#b()",)
        ref = [inv(["a"], value="r", calls=call, self=self.svc(None, "n"))]
        # S only: other arguments
        self.assertEqual(self.hits(self.cmp(ref, [inv(["z"], value="r", calls=call, self=self.svc(None, "n"))])),
                         (1, 0, 0, 0))
        # S and A: same input/output, other receiver data
        self.assertEqual(self.hits(self.cmp(ref, [inv(["a"], value="r", calls=call, self=self.svc(None, "m"))])),
                         (1, 1, 0, 0))
        # nothing: other outgoing calls
        self.assertEqual(self.hits(self.cmp(ref, [inv(["a"], value="r", calls=("virtual int A#c()",))])),
                         (0, 0, 0, 0))
        # all levels
        r = self.cmp(ref, [inv(["a"], value="r", calls=call, self=self.svc(None, "n"))])
        self.assertEqual(self.hits(r), (1, 1, 1, 1))
        self.assertEqual(r["recall_data"], r["recall_io"])

    def test_receiver_ignored_at_io_level(self):
        ref = [inv(["a"], self={"$t": "x.T", "$fields": {"tabWidth": 4}})]
        cand = [inv(["a"], self={"$t": "x.T", "$fields": {"tabWidth": 8}})]
        self.assertEqual(self.hits(self.cmp(ref, cand)), (1, 1, 0, 0))

    def test_pairwise_mock_vs_real_object(self):
        real = {"$t": "x.Executor", "$id": 2, "$fields": {"n": 7}}
        mock = {"$t": "x.Executor$MockitoMock$1", "$opaque": True}
        ref = [inv([1], self=self.svc(real, "a"))]
        # mock only on the candidate side: the field is a dependency on both sides
        r = self.cmp(ref, [inv([1], self=self.svc(mock, "a"))])
        self.assertEqual(self.hits(r), (1, 1, 1, 0))
        self.assertEqual(r["dependency_fields"], ["dep"])
        # the remaining receiver data still counts at level B
        self.assertEqual(self.hits(self.cmp(ref, [inv([1], self=self.svc(mock, "b"))])), (1, 1, 0, 0))
        # and the rule is symmetric
        self.assertEqual(self.hits(self.cmp([inv([1], self=self.svc(mock, "a"))], ref)), (1, 1, 1, 0))

    def test_component_stub_and_null_dependencies(self):
        bean = {"$t": "x.Repo", "$id": 2, "$fields": {"cache": 1}}
        other = {"$t": "x.Repo", "$id": 2, "$fields": {"cache": 2}}
        stub = {"$t": "x.SvcCarvedTest$FakeRepo", "$id": 2, "$fields": {}}
        ref = [inv([1], self=self.svc(bean, "a"))]
        self.assertEqual(self.hits(self.cmp(ref, [inv([1], self=self.svc(other, "a"))], components=["x.Repo"])),
                         (1, 1, 1, 0))
        self.assertEqual(self.hits(self.cmp(ref, [inv([1], self=self.svc(other, "a"))])), (1, 1, 0, 0))
        self.assertEqual(self.hits(self.cmp(ref, [inv([1], self=self.svc(stub, "a"))])), (1, 1, 1, 0))
        self.assertEqual(self.hits(self.cmp(ref, [inv([1], self=self.svc({"$t": "x.Stub", "$fields": {}}, "a"))],
                                            test_classes=["x.Stub"])), (1, 1, 1, 0))
        # null against an object: the field is a dependency, but the null bit stays in the key
        r = self.cmp(ref, [inv([1], self=self.svc(None, "a"))])
        self.assertEqual((self.hits(r), r["dependency_fields"]), ((1, 1, 0, 0), ["dep"]))
        # a mock and a real object of any content match at B; a null does not match a mock
        mock = {"$t": "x.Repo$MockitoMock$1", "$opaque": True}
        self.assertEqual(self.hits(self.cmp(ref, [inv([1], self=self.svc(mock, "a"))])), (1, 1, 1, 0))
        self.assertEqual(self.hits(self.cmp([inv([1], self=self.svc(None, "a"))],
                                            [inv([1], self=self.svc(mock, "a"))])), (1, 1, 0, 0))
        # a null string against a value is a data difference, not a dependency
        r = self.cmp([inv([1], self=self.svc("text", "a"))], [inv([1], self=self.svc(None, "a"))])
        self.assertEqual((self.hits(r), r["dependency_fields"]), ((1, 1, 0, 0), []))

    def test_levels_nested(self):
        import random
        rnd = random.Random(7)
        pick = lambda xs: xs[rnd.randrange(len(xs))]
        for _ in range(200):
            def one():
                return inv([pick(["a", "b"])], value=pick(["r", "q"]),
                           calls=(pick(["virtual int A#b()", "virtual int A#c()"]),),
                           self=self.svc(pick([None, {"$t": "x.D", "$fields": {"k": pick([1, 2])}},
                                               {"$t": "x.D$MockitoMock$1", "$opaque": True}]),
                                         pick(["n", "m"])))
            r = self.cmp([one() for _ in range(3)], [one() for _ in range(3)])
            h = self.hits(r)
            self.assertTrue(h[0] >= h[1] >= h[2] >= h[3], h)

    def test_junit_temporary_directories(self):
        a = "/data/results/work/ws/c-r1-reference/m/target/junit16315607296389577792/testDiff-1753139585/x"
        b = "/private/var/folders/ab/T/results/work/ws/c-komar-r1-b/m/target/junit987654321/carvedDiff1987433262/x"
        self.assertEqual(C.norm_str(a), C.norm_str(b))
        self.assertEqual(C.norm_str("/tmp/junit1234567/x"), C.norm_str("/tmp/junit7654321/x"))
        self.assertNotEqual(C.norm_str("/tmp/junit1234567/x"), C.norm_str("/tmp/junit1234567/y"))

    def test_collections_by_interface(self):
        al = {"$t": "java.util.ArrayList", "$len": 2, "$items": [1, 2]}
        ll = {"$t": "java.util.Arrays$ArrayList", "$len": 2, "$items": [1, 2]}
        im = {"$t": "java.util.ImmutableCollections$ListN", "$len": 2, "$items": [1, 2]}
        rev = {"$t": "java.util.LinkedList", "$len": 2, "$items": [2, 1]}
        self.assertEqual(self.cmp([inv([al])], [inv([ll]), inv([im])])["io_hits"], 1)
        self.assertEqual(self.cmp([inv([al])], [inv([rev])])["io_hits"], 0)
        hs = {"$t": "java.util.HashSet", "$len": 2, "$items": ["a", "b"]}
        ts = {"$t": "java.util.TreeSet", "$len": 2, "$items": ["b", "a"]}
        self.assertEqual(self.cmp([inv([hs])], [inv([ts])])["io_hits"], 1)
        hm = {"$t": "java.util.HashMap", "$len": 1, "$entries": [["k", 1]]}
        lm = {"$t": "java.util.LinkedHashMap", "$len": 1, "$entries": [["k", 1]]}
        self.assertEqual(self.cmp([inv([hm])], [inv([lm])])["io_hits"], 1)
        # a list is not a set
        self.assertEqual(self.cmp([inv([al])], [inv([{**hs, "$items": [1, 2]}])])["io_hits"], 0)

    def test_unordered_collections_sorted(self):
        a = {"$t": "java.util.HashMap", "$entries": [["k1", 1], ["k2", 2]]}
        b = {"$t": "java.util.HashMap", "$entries": [["k2", 2], ["k1", 1]]}
        self.assertEqual(C.compare([inv([a])], [inv([b])], ND, "p")["data_hits"], 1)
        la = {"$t": "java.util.ArrayList", "$items": [1, 2]}
        lb = {"$t": "java.util.ArrayList", "$items": [2, 1]}
        self.assertEqual(C.compare([inv([la])], [inv([lb])], ND, "p")["data_hits"], 0)

    def test_dependencies_are_per_pair(self):
        real = {"$t": "x.Executor", "$id": 2, "$fields": {"n": 7}}
        other = {"$t": "x.Executor", "$id": 2, "$fields": {"n": 8}}
        mock = {"$t": "x.Executor$MockitoMock$1", "$opaque": True}
        ref = [inv([1], self=self.svc(real, "a"))]
        # a mock in one candidate does not make the field a dependency for another candidate
        self.assertEqual(self.hits(self.cmp(ref, [inv([1], self=self.svc(mock, "a"))])), (1, 1, 1, 0))
        r = self.cmp(ref, [inv([1], self=self.svc(other, "a"))])
        self.assertEqual((self.hits(r), r["dependency_fields_count"]), ((1, 1, 0, 0), 0))

    def test_exception_class_is_part_of_the_structure(self):
        iae = {**inv(["a"], outcome="throw", value=None), "exception": "java.lang.IllegalArgumentException"}
        npe = {**inv(["a"], outcome="throw", value=None), "exception": "java.lang.NullPointerException"}
        self.assertEqual(self.hits(self.cmp([iae], [npe])), (0, 0, 0, 0))
        self.assertEqual(self.hits(self.cmp([iae], [dict(iae)])), (1, 1, 1, 1))
        # unknown class (fallback detection) stays "throw"
        self.assertEqual(C.outcome(inv(["a"], outcome="throw")), "throw")

    def test_truncated_unordered_collection_compared_by_size(self):
        big = lambda items: {"$t": "java.util.HashSet", "$len": 500, "$items": items}
        self.assertEqual(self.cmp([inv([big([1, 2])])], [inv([big([3, 4])])])["io_hits"], 1)
        self.assertEqual(self.cmp([inv([big([1, 2])])], [inv([{**big([1, 2]), "$len": 499}])])["io_hits"], 0)
        # a truncated list keeps its first elements, which are deterministic
        lst = lambda items: {"$t": "java.util.ArrayList", "$len": 500, "$items": items}
        self.assertEqual(self.cmp([inv([lst([1, 2])])], [inv([lst([3, 4])])])["io_hits"], 0)

    def test_caller_stats(self):
        own = {**inv(["a"]), "caller": "p.FooCarvedTest", "caller_effective": "p.FooCarvedTest"}
        nested = {**inv(["b"]), "caller": "p.FooCarvedTest$Inner", "caller_effective": "p.FooCarvedTest$Inner"}
        lib = {**inv(["c"]), "caller": "p.Parser", "caller_effective": "p.Parser"}
        s = C.caller_stats([own, nested], "p.FooCarvedTest", "p.ScenarioTest#m")
        self.assertEqual((s["called_from_test_class"], s["runs_scenario"]), (1.0, False))
        s = C.caller_stats([own, lib, {**lib, "args": ["d"]}], "p.FooCarvedTest", "p.ScenarioTest")
        self.assertAlmostEqual(s["called_from_test_class"], 1 / 3)
        self.assertTrue(s["runs_scenario"])
        s = C.caller_stats([{**own, "via_scenario": True}], "p.FooCarvedTest", "p.ScenarioTest")
        self.assertTrue(s["runs_scenario"])

    def test_unstable_between_runs_with_different_counts(self):
        norm = C.Normaliser(self.NO_ND, "p")
        a = [inv(["x"]), inv(["y"])]
        b = [inv(["x"]), inv(["z"]), inv(["w"], calls=("virtual int A#c()",))]
        d = C.unstable_between_runs(a, b, norm)
        self.assertIn("/args[0]", d)
        self.assertTrue(any(k.startswith("[invocation count]") for k in d))

    # -- normalisation refinements -------------------------------------------------------------------

    def test_collaborator_argument_matches_mock_level_a(self):
        nd = {"types": [], "type_prefixes": ["org.hsqldb."], "fields": {}}
        real = {"$t": "org.hsqldb.jdbc.JDBCPreparedStatement", "$fields": {"sql": "insert"}}
        mock = {"$t": "java.sql.PreparedStatement$MockitoMock$1", "$opaque": True}
        r = C.compare([inv([real, "x"])], [inv([mock, "x"])], nd, "p")
        self.assertEqual(r["io_hits"], 1)
        self.assertIn("args[0]", r["value_dependencies"])
        # plain data next to it still counts
        self.assertEqual(C.compare([inv([real, "x"])], [inv([mock, "y"])], nd, "p")["io_hits"], 0)

    def test_lambda_argument_matches_test_function(self):
        lam = {"$t": "io.k.ScenarioTest$$Lambda/0x1", "$opaque": True}
        fn = {"$t": "io.k.GeneratedTest$$Lambda/0x2", "$opaque": True}
        self.assertEqual(C.compare([inv([lam])], [inv([fn])], ND, "p")["io_hits"], 1)

    def test_collaborator_argument_keeps_null_bit(self):
        mock = {"$t": "a.Repo$MockitoMock$1", "$opaque": True}
        self.assertEqual(C.compare([inv([mock])], [inv([None])], ND, "p")["io_hits"], 0)

    def test_masked_value_equals_null_at_every_level(self):
        nd = {"types": ["java.util.Date"], "type_prefixes": [], "fields": {}}
        date = {"$t": "java.util.Date", "$str": "Mon Oct 12 10:00:00 UTC 2026"}
        ref = {"$t": "a.Item", "$fields": {"key": "k", "created": date}}
        cand = {"$t": "a.Item", "$fields": {"key": "k", "created": None}}
        for a, b in ((ref, cand), (cand, ref)):
            r = C.compare([inv([a], self=a)], [inv([b], self=b)], nd, "p")
            self.assertEqual((r["io_hits"], r["state_hits"], r["strict_hits"]), (1, 1, 1))
        r = C.compare([inv([date])], [inv([None])], nd, "p")
        self.assertEqual(r["io_hits"], 1)

    def test_plain_value_against_null_is_a_difference(self):
        ref = {"$t": "a.Item", "$fields": {"key": "k", "comment": "c"}}
        cand = {"$t": "a.Item", "$fields": {"key": "k", "comment": None}}
        r = C.compare([inv([ref], self=ref)], [inv([cand], self=cand)], ND, "p")
        self.assertEqual((r["io_hits"], r["state_hits"], r["strict_hits"]), (0, 0, 0))

    def test_infrastructure_prefix_is_not_masked_against_null(self):
        # type prefixes mark collaborators (null bit kept), not nondeterministic values
        session = {"$t": "org.h2.Session", "$fields": {"n": 1}}
        self.assertEqual(C.compare([inv([session])], [inv([None])], ND, "p")["io_hits"], 0)

    def test_nested_collaborator_in_argument(self):
        real = {"$t": "a.Req", "$fields": {"tx": {"$t": "org.h2.Session", "$fields": {"n": 1}}, "v": 3}}
        cand = {"$t": "a.Req", "$fields": {"tx": {"$t": "a.Tx$MockitoMock$1", "$opaque": True}, "v": 3}}
        self.assertEqual(C.compare([inv([real])], [inv([cand])], ND, "p")["io_hits"], 1)
        cand["$fields"]["v"] = 4
        self.assertEqual(C.compare([inv([real])], [inv([cand])], ND, "p")["io_hits"], 0)

    def test_test_source_domain_object_is_data_when_types_agree(self):
        old = C._TEST_CLASSES
        C._TEST_CLASSES = {"p": ["m.Item"]}
        try:
            a = {"$t": "m.Item", "$fields": {"name": "x"}}
            b = {"$t": "m.Item", "$fields": {"name": "y"}}
            r = C.compare([inv([a])], [inv([b])], ND, "p")
            self.assertEqual(r["io_hits"], 0)
            self.assertEqual(r["value_dependencies"], [])
            stub = {"$t": "m.Item", "$fields": {}}
            other = {"$t": "m.RealItem", "$fields": {"name": "x"}}
            self.assertEqual(C.compare([inv([other])], [inv([stub])], ND, "p")["io_hits"], 1)
        finally:
            C._TEST_CLASSES = old

    def test_value_dependencies_symmetric(self):
        real = {"$t": "org.h2.Conn", "$fields": {}}
        mock = {"$t": "java.sql.Connection$MockitoMock$1", "$opaque": True}
        a = C.compare([inv([real])], [inv([mock])], ND, "p")
        b = C.compare([inv([mock])], [inv([real])], ND, "p")
        self.assertEqual((a["io_hits"], a["value_dependencies"]), (b["io_hits"], b["value_dependencies"]))

    def test_scenario_count_does_not_depend_on_candidate(self):
        ref = [inv([{"$t": "org.h2.C", "$fields": {"n": 1}}]), inv([{"$t": "org.h2.C", "$fields": {"n": 2}}])]
        mock = {"$t": "a.C$MockitoMock$1", "$opaque": True}
        self.assertEqual(C.compare(ref, [inv([mock])], ND, "p")["scenarios"],
                         C.compare(ref, [inv(["x"])], ND, "p")["scenarios"])

    def test_workspace_paths_under_any_work_directory(self):
        rel = "src/main/resources/com/puppycrawl/x.xml"
        wa = "/data/exp/work-a/tg-3m8bmih3/ws/w7e0dcf25d1f0/" + rel
        other = "/data/exp/work-b/tg-vko__v_m/ws/w0123456789ab/" + rel
        tmp_work = "/data/tmp/work/tg-abc/ws/wfedcba987654/" + rel
        no_work = "/private/var/folders/x1/abc/T/tg-k2j3h4/ws/w00aa11bb22cc/" + rel  # paths.work: null
        for p in (wa, other, tmp_work, no_work):
            self.assertEqual(C.norm_str(p), rel, p)
        self.assertEqual(C.norm_str(f"[{wa}, {other}]"), f"[{rel}, {rel}]")
        # not a workspace: a project path outside the copies, a w-name of another length, a ws/ that is not ours
        for p in ("/data/exp/projects/checkstyle/" + rel,
                  "/data/ws/w7e0dcf25d1f0aa/" + rel, "/data/ws/wxyz/" + rel, "/data/ws/w7e0dcf25d1f0-x/" + rel):
            self.assertEqual(C.norm_str(p), p, p)
        # JUnit temporary directories keep their own normalisation
        self.assertEqual(C.norm_str(wa.replace(rel, "target/junit16315607296389577792/t-1753139585/x")),
                         C.norm_str(tmp_work.replace(rel, "target/junit987654321/u-1987433262/x")))

    def test_jgit_test_repository_paths_under_work_directories(self):
        r1 = ("/data/exp/work-a/tg-3hflh1vv/ws/w8269230a2a14/org.eclipse.jgit.test/target/"
              "junit16795016498900921597/testDiff1464214632/tmp_15589098419987861741/.git/objects")
        r2 = ("/data/exp/work-b/tg-vko__v_m/ws/w3889c6f42154/org.eclipse.jgit.test/target/"
              "junit15181521996069582742/testDiff2003712137/tmp_8144109260320554822/.git/objects")
        self.assertEqual(C.norm_str(r1), C.norm_str(r2))
        self.assertEqual(C.norm_str(r1), "org.eclipse.jgit.test/target/<tmp>/tmp_<n>/.git/objects")
        a = inv(["x"], self={"$t": "org.eclipse.jgit.internal.storage.file.ObjectDirectory",
                              "$fields": {"objects": {"$t": "java.io.File", "$str": r1}}})
        b = inv(["x"], self={"$t": "org.eclipse.jgit.internal.storage.file.ObjectDirectory",
                              "$fields": {"objects": {"$t": "java.io.File", "$str": r2}}})
        r = C.compare([a], [b], ND, "jgit")
        self.assertEqual((r["recall_state"], r["recall_strict"]), (1.0, 1.0))
        other = r1.replace("/.git/objects", "/.git/refs")
        self.assertNotEqual(C.norm_str(r1), C.norm_str(other))

    def test_relative_and_absolute_workspace_paths_equal(self):
        a = inv(["/home/u/work/ws/w1a2b/src/test/resources/x.java"])
        b = inv(["src/test/resources/x.java"])
        self.assertEqual(C.compare([a], [b], ND, "p")["io_hits"], 1)

    def test_inherited_field_names_without_class_prefix(self):
        a = inv([], self={"$t": "x.T", "$fields": {"Base.count": 1}})
        b = inv([], self={"$t": "x.T", "$fields": {"count": 1}})
        self.assertEqual(C.compare([a], [b], ND, "p")["strict_hits"], 1)
        clash = {"$t": "x.T", "$fields": {"Base.count": 1, "count": 2}}
        self.assertEqual(set(C.Normaliser(ND, "p").value(clash)["$fields"]), {"Base.count", "count"})

    def test_jdk_proxy_names_normalised(self):
        self.assertEqual(C.norm_type("jdk.proxy2.$Proxy17"), C.norm_type("jdk.proxy3.$Proxy4"))
        self.assertEqual(C.norm_type("com.sun.proxy.$Proxy9"), "$Proxy")

    def test_shipped_list_masks_spring_transactions_not_apollo_id(self):
        nd = C.load_nondet()
        self.assertNotIn("id", nd["fields"]["apollo"])
        self.assertIn("org.springframework.transaction.", nd["type_prefixes"])
        self.assertIn("org.springframework.orm.", nd["type_prefixes"])

    def test_btrace_internal_calls_dropped(self):
        a = inv(["a"], calls=("virtual int A#b()", "static void ##mc$test:timed"))
        self.assertEqual(C.structure_key(a), C.structure_key(inv(["a"])))


class TraceTest(unittest.TestCase):
    def test_parse_signature(self):
        self.assertEqual(T.parse_signature("(java.util.List<x.Y>, int) -> java.util.Map<a,b>"),
                         (["java.util.List", "int"], "java.util.Map"))
        self.assertEqual(T.parse_signature("() -> void"), ([], "void"))

    def test_read_trace_closes_open_invocations(self):
        import json, tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        (d / "trace-1.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"ev": "enter", "id": 1, "self": None, "args": [1]},
            {"ev": "call", "id": 1, "sig": "s"},
            {"ev": "return", "id": 1, "value": 2, "void": False},
            {"ev": "enter", "id": 2, "self": None, "args": [3]},
        ]))
        t = T.read_trace(d)
        self.assertEqual([i["outcome"] for i in t["invocations"]], ["return", "throw"])

    def test_read_trace_keeps_exception_and_caller(self):
        import json, tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        (d / "trace-1.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"ev": "enter", "id": 1, "self": None, "args": [1], "caller": "a.T", "caller_effective": "a.T",
             "via_scenario": False},
            {"ev": "throw", "id": 1, "exception": "java.lang.IllegalStateException"},
        ]))
        inv_ = T.read_trace(d)["invocations"][0]
        self.assertEqual((inv_["outcome"], inv_["exception"], inv_["caller"]),
                         ("throw", "java.lang.IllegalStateException", "a.T"))

    def test_remove_paths_removes_compiled_classes_of_directories_and_groovy(self):
        import tempfile
        from pathlib import Path
        ws = Path(tempfile.mkdtemp())
        for rel in ("src/test/java/org/x/sub/ATest.java", "target/test-classes/org/x/sub/ATest.class",
                    "target/test-classes/org/x/sub/Mapper.class", "target/test-classes/org/x/Keep.class",
                    "m/src/test/groovy/org/y/BTest.groovy", "m/target/test-classes/org/y/BTest.class",
                    "m/target/test-classes/org/y/BTest$_closure1.class", "m/target/test-classes/org/y/BTester.class",
                    "src/test/java/org/z/CTest.java", "target/test-classes/org/z/CTest.class",
                    "target/test-classes/org/z/CTest$1.class", "target/test-classes/org/z/CTestUtil.class"):
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_text("")
        removed = T.remove_paths(ws, ["src/test/java/org/x/sub", "m/src/test/groovy/org/y/BTest.groovy",
                                      "src/test/java/org/z/CTest.java"])
        self.assertFalse((ws / "target/test-classes/org/x/sub").exists())
        self.assertTrue((ws / "target/test-classes/org/x/Keep.class").exists())
        self.assertFalse((ws / "m/target/test-classes/org/y/BTest.class").exists())
        self.assertFalse((ws / "m/target/test-classes/org/y/BTest$_closure1.class").exists())
        self.assertTrue((ws / "m/target/test-classes/org/y/BTester.class").exists())
        self.assertFalse((ws / "target/test-classes/org/z/CTest$1.class").exists())
        self.assertTrue((ws / "target/test-classes/org/z/CTestUtil.class").exists())
        self.assertEqual(len(removed), 6)

    def test_read_trace_drops_launcher_jvms(self):
        import json, tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        enter = {"ev": "enter", "id": 1, "self": None, "args": [1]}
        (d / "trace-1-1.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"ev": "jvm", "pid": 1, "main": "org.codehaus.plexus.classworlds.launcher.Launcher"}, enter]))
        (d / "trace-2-2.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"ev": "jvm", "pid": 2, "main": "/tmp/surefire/surefirebooter1.jar"}, enter]))
        t = T.read_trace(d)
        self.assertEqual(len(t["invocations"]), 1)
        self.assertEqual(len(t["skipped_launcher_files"]), 1)


class LevelBRuleTest(unittest.TestCase):
    """level_b: dependencies decided from the reference only, plus the labelled fields."""
    MOCK = {"$t": "a.Repo$MockitoMock$1", "$opaque": True}
    DATA = {"$t": "a.Config", "$fields": {"limit": 3}}

    def recv(self, field):
        return {"$t": "a.Service", "$fields": {"cfg": field, "name": "x"}}

    def test_mock_in_the_test_where_the_reference_has_data_does_not_match(self):
        ref = [inv(["a"], self=self.recv(self.DATA))]
        cand = [inv(["a"], self=self.recv(self.MOCK))]
        self.assertEqual(C.compare(ref, cand, ND, "p")["recall_state"], 1.0)  # symmetric rule
        self.assertEqual(C.level_b(ref, cand, "p", set())["b"], 0.0)

    def test_labelled_dependency_matches_a_mock(self):
        ref = [inv(["a"], self=self.recv(self.DATA))]
        cand = [inv(["a"], self=self.recv(self.MOCK))]
        self.assertEqual(C.level_b(ref, cand, "p", {"cfg"})["b"], 1.0)

    def test_reference_side_mock_still_a_dependency(self):
        ref = [inv(["a"], self=self.recv(self.MOCK))]
        cand = [inv(["a"], self=self.recv(self.DATA))]
        self.assertEqual(C.level_b(ref, cand, "p", set())["b"], 1.0)

    def test_data_fields_compared_by_content(self):
        ref = [inv(["a"], self=self.recv(self.DATA))]
        other = {"$t": "a.Config", "$fields": {"limit": 4}}
        self.assertEqual(C.level_b(ref, [inv(["a"], self=self.recv(other))], "p", set())["b"], 0.0)
        self.assertEqual(C.level_b(ref, [inv(["a"], self=self.recv(self.DATA))], "p", set())["b"], 1.0)

    def test_value_position_mock_only_where_the_reference_has_one(self):
        nd = {"types": [], "type_prefixes": [], "fields": {}}
        real = {"$t": "a.Session", "$fields": {"id": 1}}
        mock = {"$t": "a.Session$MockitoMock$1", "$opaque": True}
        ref, cand = [inv([real])], [inv([mock])]
        self.assertEqual(C.compare(ref, cand, nd, "p")["recall_state"], 1.0)
        self.assertEqual(C.compare(ref, cand, nd, "p", asymmetric=True)["recall_state"], 0.0)
        self.assertEqual(C.compare([inv([mock])], [inv([real])], nd, "p", asymmetric=True)["recall_state"], 1.0)

    def test_labels_file(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "l.json"
            f.write_text(json.dumps({"labels": [{"case": "c", "field": "a", "label": "DEPENDENCY"},
                                                {"case": "c", "field": "b", "label": "STATE"},
                                                {"case": "d", "field": "x", "label": "DEPENDENCY"}]}))
            self.assertEqual(C.labelled_dependencies(f, "c"), {"a"})
            self.assertEqual(C.labelled_dependencies(Path(d) / "none.json", "c"), set())

    def test_shipped_labels_cover_the_cases(self):
        from runner.config import ROOT
        data = json.loads((ROOT / "evaluation" / "dependency-labels.json").read_text())
        self.assertTrue(all(x["label"] in ("DEPENDENCY", "STATE") for x in data["labels"]))
        self.assertGreater(len({x["case"] for x in data["labels"]}), 70)


if __name__ == "__main__":
    unittest.main()
