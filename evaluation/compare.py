"""Comparison of a candidate trace with the reference: scenario keys, nested levels, recall and precision.

The evaluation reports level B only (level_b): the generated test reproduces the arguments, the result
and the receiver state of the target invocations of the reference. The other levels are computed on the
way (B keys contain the A key, which contains the S key) and used by the reference audit.

Levels:
  S  structure       = outcome (return, or the class of the thrown exception) + ordered signatures of
                       outgoing calls from the target body;
  A  input/output    = S + arguments + returned value                       (main data level);
  B  with state      = A + receiver, where receiver fields that are collaborators are compared by
                       presence only (see "dependency fields" below)       (shown next to A);
  C  strict          = A + full receiver graph                              (appendix).

Scenario = unique invocation of the target at level C (identical invocations collapse). All levels
share one denominator: the number of distinct reference scenarios. B and C keys contain the A key,
which contains the S key, so hits satisfy S >= A >= B >= C by construction.

Dependency fields (level B), symmetric rule (compare() by default; the reference audit, run 1 against
run 2): decided for each pair of traces from their union, before any key is built, and masked
identically on both sides. A top-level receiver field is a dependency if, in some invocation on
either side, it holds
  * a mock or proxy (the tracer marks it "$opaque"),
  * an instance of a framework component class (components.json: Spring stereotypes),
  * an instance of a class from the project's test sources or of a test class (a hand-written stub),
or if it is null in every invocation on one side and a (non-JDK) object on the other side; a null
string, number or collection against a value is a data difference, not a collaborator. A masked
field keeps one bit: whether it was null. A null collaborator therefore does not match an object
(a mock or a real one), while a mock matches a real object of any content.

Level A applies the same idea to the arguments and the result: a position inside them that holds a
collaborator on either side (mock or proxy, object of a masked type, lambda, component, test-source
class) is compared by presence only, keeping the null bit (value_dependencies, pairwise).

Asymmetric rule with labels (level_b, the evaluation of generated tests): the dependency fields and
value positions are decided from the REFERENCE alone, so a test that puts a mock where the reference
holds a data object does not match there; the receiver fields labelled DEPENDENCY for the case
(dependency-labels.json: fields the target method delegates work to, which a unit test may replace
with a stub) are added to the dependency fields. Every other field and position is compared by content,
even if the candidate holds a mock there.
Level C is the receiver, arguments and result without any markers; it is also the scenario unit.

Normalisation, the same for every level and mode:
  * BTrace-internal pseudo calls (``##...``) are dropped;
  * JDK dynamic proxy names (jdk.proxyN.$ProxyM) are reduced to $Proxy;
  * workspace paths: an absolute path into the workspace equals the same path relative to it;
  * inherited field names lose the declaring-class prefix the tracer adds, unless they would clash;
  * synthetic proxy / mock class suffixes are reduced to the declared class name;
  * object identities ($id/$ref) are dropped, back references become a marker;
  * strings: workspace paths, JUnit / java.io temporary directories, identity hash codes,
    timestamps, epoch milliseconds and UUIDs are replaced by placeholders;
  * JDK collections are compared by interface: List in order, Set and Map without order;
  * the tracer writes at most 200 elements of a collection, in iteration order. For a List that is
    the first 200 elements, which is deterministic. For a Set or Map the iteration order depends on
    hash codes, so a truncated one is compared by its interface and size only;
  * values of types and fields listed in nondeterminism.json are masked.

The tracer also records, per invocation, the class that called the target (`caller`, the immediate
frame; `caller_effective`, the first frame that is not a mock, proxy or reflection frame) and whether
the scenario test class was on the stack (`via_scenario`). They are not part of any key;
caller_stats() reports them next to the levels.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

PROXY_RE = re.compile(
    r"(\$MockitoMock\$\w+|\$\$EnhancerBySpringCGLIB\$\$\w+|\$\$SpringCGLIB\$\$\d+|\$HibernateProxy\$\w+"
    r"|\$ByteBuddy\$\w+|\$\$Lambda[\$/][\w/\.\$]*|\$Proxy\d+)"
)
# JDK dynamic proxies live in numbered modules/packages: jdk.proxy2.$Proxy17 -> $Proxy
JDK_PROXY = re.compile(r"^(?:jdk\.proxy\d+\.|com\.sun\.proxy\.)?\$Proxy\d*$")
LAMBDA = re.compile(r"\$\$Lambda")
HERE = Path(__file__).resolve().parent
DEFAULT_NONDET = HERE / "nondeterminism.json"
DEFAULT_COMPONENTS = HERE / "components.json"
DEFAULT_TEST_CLASSES = HERE / "test-classes.json"
# per-project lists of the projects of cases-jdk11.csv (components, test_classes, nondet_fields; written
# by components.py --extra)
DEFAULT_EXTRA = HERE / "normalisation-extra.json"

IDENTITY_HASH = re.compile(r"@[0-9a-f]{4,}\b")
# timestamps embedded in strings (JSON payloads, messages): 2026-10-11 23:57:39, 2026-10-11T23:57:39.123Z
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# Every trace runs in its own copy of the checkout (<work>/ws/<neutral name>, runner.config.neutral_name:
# w + 12 hex digits), so absolute paths into the workspace differ between the reference and any
# candidate by construction. The work directory can have any name (paths.work, or a temporary
# directory when it is not set); older traces used <...>/work/ws/<name>.
WORKSPACE_PATH = re.compile(r"(?:/private)?/[^\s\"',;]*?/(?:ws/w[0-9a-f]{12}(?![0-9A-Za-z_-])|work/ws/[^/\s\"',;]+)")
# The same file given as an absolute path into the workspace and as a path relative to it compare
# equal: "<ws>/src/test/resources/x" and "src/test/resources/x" both become "src/test/resources/x".
WS_PREFIX = re.compile(r"<ws>/")
# JUnit temporary folders with the directory created inside them, named after the running test
# method: junit16315607296389577792/testDiff-1753139585, junit123/indexWithGitmodules1987433262.
# The test method name differs between the scenario and any generated test, so the pair is replaced
# as a whole.
JUNIT_DIR = re.compile(r"\bjunit\d{6,}(?:/[^/\s\"',;]*?-?\d{6,})?")
# Remaining random suffixes of temporary files and directories (File.createTempFile, @TempDir):
# tmp_10881433622251430644, prefix-1987433262.
TEMP_SUFFIX = re.compile(r"(?<=[A-Za-z_])-?\d{6,}")
TEMP_DIR = re.compile(r"(?:/private)?/(?:tmp|var/folders/[^\s\"',;]+?/T)/[^\s\"',;]*")
# Epoch milliseconds inside strings (2023-2033), e.g. JSON payloads with deletedAt, test data built
# with "johnny" + System.currentTimeMillis().
EPOCH_MS = re.compile(r"(?<!\d)1[789]\d{11}(?!\d)")
# Generated test classes are not in the dataset's test sources; their nested stubs are recognised by
# the name of the outer class.
TEST_NAME = re.compile(r"(Test|Tests|TestCase|IT)$")

def collaborator_marker(v: Any, keep_null: bool = True) -> dict[str, Any]:
    """Marker of a collaborator position. The null bit is dropped at masked positions (see
    masked_positions): a masked value carries no data, so it equals null too."""
    if not keep_null:
        return {"$t": "$collaborator"}
    return {"$t": "$collaborator", "null": v is None}


MASKED = {"$masked": True}


def norm_str(s: str) -> str:
    s = WORKSPACE_PATH.sub("<ws>", s)
    s = JUNIT_DIR.sub("<tmp>", s)
    if "<ws>" in s or "<tmp>" in s or TEMP_DIR.search(s):
        s = TEMP_SUFFIX.sub("<n>", s)
    s = WS_PREFIX.sub("", s)
    s = EPOCH_MS.sub("<millis>", s)
    return UUID_RE.sub("<uuid>", TIMESTAMP.sub("<time>", IDENTITY_HASH.sub("@#", s)))


def norm_type(name: str) -> str:
    name = name or ""
    if JDK_PROXY.match(name):
        return "$Proxy"
    return PROXY_RE.sub("", name)


def norm_sig(sig: str) -> str:
    return PROXY_RE.sub("", sig)


def collection_kind(t: str, v: dict[str, Any]) -> str | None:
    """Interface of a JDK collection value: 'List', 'Set', 'Map', 'Collection'; None otherwise."""
    if not t.startswith("java.") or t.startswith("["):
        return None
    if "$entries" in v:
        return "Map"
    if "$items" not in v:
        return None
    simple = t.rsplit(".", 1)[-1]
    if "Set" in simple:
        return "Set"
    if "Values" in simple or "PriorityQueue" in simple:
        return "Collection"
    return "List"


# ---------------------------------------------------------------------------------------------
# project data: framework components and test-source classes (components.py writes both files)

_JSON_CACHE: dict[Path, dict[str, list[str]]] = {}


def _load_json(path: Path) -> dict[str, list[str]]:
    if path not in _JSON_CACHE:
        _JSON_CACHE[path] = json.loads(path.read_text()) if path.is_file() else {}
    return _JSON_CACHE[path]


_COMPONENTS: dict[str, list[str]] | None = None  # test hook: overrides components.json
_TEST_CLASSES: dict[str, list[str]] | None = None  # test hook: overrides test-classes.json


def load_extra(project: str | None) -> dict[str, list[str]]:
    """Entry of a project in normalisation-extra.json ({} for the projects of cases.csv)."""
    return dict(_load_json(DEFAULT_EXTRA).get(project or "", {}))


def load_components(project: str | None) -> set[str]:
    if _COMPONENTS is not None:
        return set(_COMPONENTS.get(project or "", []))
    return set(_load_json(DEFAULT_COMPONENTS).get(project or "", [])) | set(load_extra(project).get("components", []))


def load_test_classes(project: str | None) -> set[str]:
    if _TEST_CLASSES is not None:
        return set(_TEST_CLASSES.get(project or "", []))
    return set(_load_json(DEFAULT_TEST_CLASSES).get(project or "", [])) | set(load_extra(project).get("test_classes", []))


def load_nondet(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_NONDET
    if p.is_file():
        return json.loads(p.read_text())
    return {"types": [], "fields": {}}


# ---------------------------------------------------------------------------------------------


class Normaliser:
    def __init__(self, nondet: dict[str, Any], project: str | None = None,
                 components: set[str] | None = None, test_classes: set[str] | None = None):
        self.components = load_components(project) if components is None else components
        self.test_classes = load_test_classes(project) if test_classes is None else test_classes
        self.types = set(nondet.get("types", []))
        self.prefixes = tuple(nondet.get("type_prefixes", []))
        fields = nondet.get("fields", {})
        self.fields = set(fields.get("*", [])) | set(fields.get(project or "", [])) | set(load_extra(project).get("nondet_fields", []))
        self.masked = 0
        self.cuts = 0
        # paths (args..., value..., self...) holding a masked value on some side: there a masked
        # value and null compare equal (masked_positions); set by compare() for the pair of traces
        self.masked_paths: set[str] = set()

    def value(self, v: Any, deps: "set[str] | None" = None, path: str = "") -> Any:
        """Canonical form: identities dropped (a back reference becomes a marker), JDK collections by
        interface, identity hash codes and masked values removed. Positions listed in deps (paths
        inside arguments and results, see value_dependencies) become a collaborator marker."""
        if deps and path in deps:
            return collaborator_marker(v, keep_null=path not in self.masked_paths)
        if path and path in self.masked_paths and (v is None or self.is_nondet_type(v)):
            if v is not None:
                self.masked += 1
            return dict(MASKED)
        if isinstance(v, list):
            return [self.value(x, deps, f"{path}[{i}]") for i, x in enumerate(v)]
        if isinstance(v, str):
            return norm_str(v)
        if not isinstance(v, dict):
            return v
        if "$ref" in v:
            return {"$ref": True}
        if v.get("$t") == "$collaborator":
            return dict(v)
        t = norm_type(v.get("$t", ""))
        if t in self.types or (self.prefixes and t.startswith(self.prefixes)):
            self.masked += 1
            return {"$t": t, "$masked": True}
        kind = collection_kind(t, v)
        out: dict[str, Any] = {"$t": kind or t}
        if v.get("$cut"):
            self.cuts += 1
            out["$cut"] = True
        if v.get("$opaque"):
            out["$opaque"] = True
        for k in ("$enum", "v", "$len"):
            if k in v:
                out[k] = v[k]
        if "$str" in v:
            out["$str"] = norm_str(v["$str"])
        unordered = kind in ("Set", "Map", "Collection")
        shown = len(v.get("$items", v.get("$entries", [])))
        truncated = isinstance(v.get("$len"), int) and v["$len"] > shown
        if unordered and truncated:
            # which elements were written depends on the iteration order: compare the size only
            out["$truncated"] = True
        elif "$items" in v:
            # positions inside unordered collections are not stable: no per-position markers there
            sub = None if unordered else deps
            items = [self.value(x, sub, f"{path}[{i}]") for i, x in enumerate(v["$items"])]
            out["$items"] = sorted(items, key=_sort_key) if unordered else items
        elif "$entries" in v:
            entries = [[self.value(a), self.value(b)] for a, b in v["$entries"]]
            out["$entries"] = sorted(entries, key=_sort_key) if unordered else entries
        if "$fields" in v:
            fs = {}
            for name, fv in v["$fields"].items():
                short = field_name(name, v["$fields"])
                if short in self.fields or f"{t}.{short}" in self.fields:
                    self.masked += 1
                    fs[short] = {"$masked": True}
                else:
                    fs[short] = self.value(fv, deps, f"{path}.{short}")
            out["$fields"] = fs
        return out

    def is_masked_type(self, v: Any) -> bool:
        if not isinstance(v, dict) or "$t" not in v:
            return False
        t = norm_type(v.get("$t", ""))
        return t in self.types or bool(self.prefixes and t.startswith(self.prefixes))

    def is_nondet_type(self, v: Any) -> bool:
        """A value of a nondeterministic type listed by name in nondeterminism.json (time, UUID, ...).
        Type prefixes (infrastructure objects) are collaborators, not nondeterministic data."""
        return isinstance(v, dict) and "$t" in v and norm_type(v.get("$t", "")) in self.types

    def is_value_collaborator(self, v: Any) -> bool:
        """A value inside arguments or a result that is a collaborator rather than data on its own:
        a mock or proxy, an object of a masked type (JDBC driver, ...), a lambda, a component.
        Test-source classes are handled pairwise in value_dependencies: in some projects (MyBatis)
        the domain objects of the scenarios live in test sources, so they are data unless the two
        sides hold different types there."""
        if not isinstance(v, dict) or "$ref" in v:
            return False
        if v.get("$opaque") or self.is_masked_type(v) or LAMBDA.search(v.get("$t", "")):
            return True
        return norm_type(v.get("$t", "")) in self.components

    # -- dependency fields of the receiver (level B) --------------------------------------------

    def is_test_class(self, t: str) -> bool:
        outer = norm_type(t).split("$", 1)[0]
        return outer in self.test_classes or bool(TEST_NAME.search(outer.rsplit(".", 1)[-1]))

    def is_collaborator(self, v: Any) -> bool:
        if not isinstance(v, dict) or "$ref" in v:
            return False
        if v.get("$opaque"):
            return True
        t = norm_type(v.get("$t", ""))
        return t in self.components or self.is_test_class(t)

    def receiver(self, v: Any, deps: set[str]) -> Any:
        """Receiver with dependency fields reduced to a marker (level B)."""
        if not (isinstance(v, dict) and "$fields" in v) or not deps:
            return self.value(v, path="self")
        fields = {}
        for name, fv in v["$fields"].items():
            short = field_name(name, v["$fields"])
            if short in deps:
                fv = collaborator_marker(fv, keep_null=f"self.{short}" not in self.masked_paths)
            fields[name] = fv
        return self.value({**v, "$fields": fields}, path="self")


def field_name(name: str, fields: dict[str, Any]) -> str:
    """Field name without the declaring-class prefix the tracer adds for inherited fields
    ("Base.count" -> "count"), unless that would clash with another field of the same object."""
    if "." not in name:
        return name
    short = name.rsplit(".", 1)[-1]
    clash = sum(1 for other in fields if other.rsplit(".", 1)[-1] == short) > 1
    return name if clash else short


def _sort_key(x: Any) -> str:
    return json.dumps(x, sort_keys=True)


def _is_object(v: Any) -> bool:
    """A non-JDK object (field graph, mock, cut or back reference): something that can be a
    collaborator. Strings, numbers, enums, JDK values and collections are data."""
    if not isinstance(v, dict) or "$enum" in v:
        return False
    if "$ref" in v or v.get("$opaque"):
        return True
    return ("$fields" in v or bool(v.get("$cut"))) and not norm_type(v.get("$t", "")).startswith("java.")


def masked_positions(sides: Iterable[list[dict[str, Any]]], norm: Normaliser) -> set[str]:
    """Paths (args..., value..., self...) where some invocation on any side holds a value of a type
    listed by name in the nondeterminism list (time, UUID, ...; not the infrastructure prefixes). At these paths a masked value and null compare equal at every level
    (symmetric, pairwise like the dependency rules): a scenario with a timestamp and a test that
    leaves it null differ only in a value the comparison ignores anyway. Paths inside unordered
    collections and map entries are not tracked; fields masked by name already ignore null."""
    out: set[str] = set()

    def walk(v: Any, path: str) -> None:
        if isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
            return
        if not isinstance(v, dict) or "$ref" in v or v.get("$t") == "$collaborator":
            return
        if norm.is_nondet_type(v):
            out.add(path)
            return
        if norm.is_masked_type(v):
            return
        t = norm_type(v.get("$t", ""))
        if "$fields" in v:
            for raw, fv in v["$fields"].items():
                walk(fv, f"{path}.{field_name(raw, v['$fields'])}")
        if "$items" in v and collection_kind(t, v) not in ("Set", "Map", "Collection"):
            for i, x in enumerate(v["$items"]):
                walk(x, f"{path}[{i}]")

    for invocations in sides:
        for inv in invocations or []:
            walk(inv.get("args", []), "args")
            if inv.get("outcome") == "return":
                walk(inv.get("value"), "value")
            walk(inv.get("self"), "self")
    return out


def dependency_fields(sides: Iterable[list[dict[str, Any]]], norm: Normaliser) -> set[str]:
    """Receiver fields treated as collaborators, from all traces at once (pairwise rule)."""
    deps: set[str] = set()
    null_only: list[set[str]] = []
    objects: list[set[str]] = []
    for invocations in sides:
        seen_null: dict[str, bool] = {}
        seen_obj: set[str] = set()
        for inv in invocations or []:
            self_ = inv.get("self")
            if not (isinstance(self_, dict) and "$fields" in self_):
                continue
            for raw, fv in self_["$fields"].items():
                name = field_name(raw, self_["$fields"])
                if norm.is_collaborator(fv) or norm.is_masked_type(fv):
                    deps.add(name)
                if fv is None:
                    seen_null.setdefault(name, True)
                else:
                    seen_null[name] = False
                    if _is_object(fv):
                        seen_obj.add(name)
        null_only.append({n for n, only in seen_null.items() if only})
        objects.append(seen_obj)
    for i, nulls in enumerate(null_only):
        for j, objs in enumerate(objects):
            if i != j:
                deps |= nulls & objs
    return deps


def value_dependencies(sides: Iterable[list[dict[str, Any]]], norm: Normaliser) -> set[str]:
    """Positions inside arguments and results (paths such as args[0].dataSource or value) that hold
    a collaborator on any side: a mock or proxy, an object of a masked type, a lambda, a component or
    a test-source class. Pairwise like dependency_fields: computed from the reference and one
    candidate together, and a position that is null in every invocation on one side and an object on
    the other is a dependency too (the marker keeps the null bit, so null still differs from an
    object). Positions inside unordered collections are not tracked."""
    deps: set[str] = set()
    null_only: list[set[str]] = []
    objects: list[set[str]] = []
    test_typed: list[set[str]] = []
    types: list[dict[str, set[str]]] = []

    def walk(v: Any, path: str, st: dict[str, Any]) -> None:
        if v is None:
            st["nulls"].setdefault(path, True)
            return
        st["nulls"][path] = False
        if isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]", st)
            return
        if not isinstance(v, dict) or "$ref" in v:
            return
        if norm.is_value_collaborator(v):
            st["coll"].add(path)
            return
        t = norm_type(v.get("$t", ""))
        st["types"].setdefault(path, set()).add(t)
        if _is_object(v):
            st["objs"].add(path)
            if norm.is_test_class(t):
                st["test"].add(path)
        if "$fields" in v:
            for raw, fv in v["$fields"].items():
                walk(fv, f"{path}.{field_name(raw, v['$fields'])}", st)
        if "$items" in v and collection_kind(t, v) not in ("Set", "Map", "Collection"):
            for i, x in enumerate(v["$items"]):
                walk(x, f"{path}[{i}]", st)

    for invocations in sides:
        st: dict[str, Any] = {"coll": set(), "nulls": {}, "objs": set(), "test": set(), "types": {}}
        for inv in invocations or []:
            walk(inv.get("args", []), "args", st)
            if inv.get("outcome") == "return":
                walk(inv.get("value"), "value", st)
        deps |= st["coll"]
        null_only.append({p for p, only in st["nulls"].items() if only})
        objects.append(st["objs"])
        test_typed.append(st["test"])
        types.append(st["types"])
    for i, ns in enumerate(null_only):
        for j, objs in enumerate(objects):
            if i != j:
                deps |= ns & objs
                # a stub from test sources on one side, another class on the other side
                deps |= {p for p in test_typed[i]
                         if p in types[j] and types[j][p] != types[i].get(p, set())}
    return deps


def outcome(inv: dict[str, Any]) -> str | None:
    """'return', or 'throw <exception class>' ('throw' when the class is unknown)."""
    if inv.get("outcome") == "throw" and inv.get("exception"):
        return f"throw {norm_type(inv['exception'])}"
    return inv.get("outcome")


def structure_key(inv: dict[str, Any]) -> str:
    calls = [norm_sig(c) for c in inv.get("calls", []) if "##" not in c]
    return json.dumps([outcome(inv), calls])


def io_key(inv: dict[str, Any], norm: Normaliser, vdeps: set[str] | None = None) -> str:
    return json.dumps(
        [
            json.loads(structure_key(inv)),
            norm.value(inv.get("args", []), vdeps, "args"),
            norm.value(inv.get("value"), vdeps, "value") if inv.get("outcome") == "return" else None,
        ],
        sort_keys=True,
    )


def state_key(inv: dict[str, Any], norm: Normaliser, deps: set[str], vdeps: set[str] | None = None) -> str:
    return json.dumps([json.loads(io_key(inv, norm, vdeps)), norm.receiver(inv.get("self"), deps)],
                      sort_keys=True)


def strict_key(inv: dict[str, Any], norm: Normaliser) -> str:
    """Level C: no collaborator markers anywhere. It is also the scenario unit, so the number of
    reference scenarios does not depend on the candidate."""
    return json.dumps([json.loads(io_key(inv, norm)), norm.value(inv.get("self"), path="self")],
                      sort_keys=True)


def data_key(inv: dict[str, Any], norm: Normaliser, strict: bool = False) -> str:
    """Compatibility: level A key, or the strict (C) key."""
    return strict_key(inv, norm) if strict else io_key(inv, norm)


LEVELS = ("structure", "io", "state", "strict")


def scenarios(invocations: list[dict[str, Any]], norm: Normaliser,
              deps: set[str] | None = None, vdeps: set[str] | None = None) -> dict[str, tuple[str, str, str]]:
    """strict key -> (structure key, io key, state key), for distinct scenarios."""
    if deps is None:
        deps = dependency_fields([invocations], norm)
        norm.masked_paths = norm.masked_paths | masked_positions([invocations], norm)
    if vdeps is None:
        vdeps = value_dependencies([invocations], norm)
    out: dict[str, tuple[str, str, str]] = {}
    for inv in invocations:
        out.setdefault(strict_key(inv, norm),
                       (structure_key(inv), io_key(inv, norm, vdeps), state_key(inv, norm, deps, vdeps)))
    return out


def labelled_dependencies(path: Path, case_id: str) -> set[str]:
    """Receiver fields of the case labelled DEPENDENCY in a labels file (scripts/dep_labels.py)."""
    if not path.is_file():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {x["field"] for x in data.get("labels", []) if x.get("case") == case_id and x.get("label") == "DEPENDENCY"}


def compare(reference: list[dict[str, Any]], candidate: list[dict[str, Any]] | None,
            nondet: dict[str, Any] | None = None, project: str | None = None,
            components: set[str] | None = None, test_classes: set[str] | None = None,
            asymmetric: bool = False, labelled: set[str] | None = None) -> dict[str, Any]:
    """Recall/precision at levels S, A, B, C. candidate=None means the test did not run.
    `asymmetric`: dependency fields and value positions from the reference only, plus `labelled`."""
    norm = Normaliser(nondet if nondet is not None else load_nondet(), project, components, test_classes)
    sides = [reference] if asymmetric else [reference, candidate or []]
    deps = dependency_fields(sides + ([[]] if asymmetric else []), norm) | set(labelled or ())
    vdeps = value_dependencies(sides + ([[]] if asymmetric else []), norm)
    norm.masked_paths = masked_positions([reference, candidate or []], norm)
    ref = scenarios(reference, norm, deps, vdeps)
    n = len(ref)
    out: dict[str, Any] = {"scenarios": n}
    if candidate is None:
        z = 0.0 if n else None
        for level in LEVELS:
            out[f"{level}_hits"] = 0
            out[f"recall_{level}"] = z
            out[f"precision_{level}"] = None
        out["candidate_scenarios"] = 0
    else:
        cand = scenarios(candidate, norm, deps, vdeps)
        m = len(cand)
        ref_keys = {"strict": set(ref)}
        cand_keys = {"strict": set(cand)}
        for i, level in enumerate(("structure", "io", "state")):
            ref_keys[level] = {k[i] for k in ref.values()}
            cand_keys[level] = {k[i] for k in cand.values()}

        def level_of(keys: tuple[str, str, str], strict: str, level: str) -> str:
            return strict if level == "strict" else keys[("structure", "io", "state").index(level)]

        for level in LEVELS:
            hits = sum(1 for sk, ks in ref.items() if level_of(ks, sk, level) in cand_keys[level])
            ok = sum(1 for sk, ks in cand.items() if level_of(ks, sk, level) in ref_keys[level])
            out[f"{level}_hits"] = hits
            out[f"recall_{level}"] = hits / n if n else None
            out[f"precision_{level}"] = ok / m if m else None
        out["candidate_scenarios"] = m
        # reference scenarios reproduced at level A (short ids), for stability across rounds
        out["io_hit_ids"] = sorted(hashlib.sha1(sk.encode()).hexdigest()[:12] for sk, ks in ref.items()
                                   if ks[1] in cand_keys["io"])
    # "data" is an alias of the main data level A
    out["data_hits"] = out["io_hits"]
    out["recall_data"] = out["recall_io"]
    out["precision_data"] = out["precision_io"]
    out["dependency_fields"] = sorted(deps)
    out["dependency_fields_count"] = len(deps)
    out["value_dependencies"] = sorted(vdeps)
    out["value_dependencies_count"] = len(vdeps)
    out["masked"] = norm.masked
    out["cuts"] = norm.cuts
    return out


def own_invocations(candidate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The invocations of the target that the generated test makes itself: those reached through the
    scenario test (its class on the stack, `via_scenario`) are dropped, so that a test running the
    scenario cannot reproduce it by construction."""
    return [inv for inv in candidate if not inv.get("via_scenario")]


def level_b(reference: list[dict[str, Any]], candidate: list[dict[str, Any]] | None, project: str | None,
            labelled: set[str]) -> dict[str, Any]:
    """Level B of a candidate under the asymmetric rule with the case's labelled dependency fields,
    over the invocations the generated test makes itself (own_invocations)."""
    counted = None if candidate is None else own_invocations(candidate)
    r = compare(reference, counted, load_nondet(), project, asymmetric=True, labelled=labelled)
    return {"b": r["recall_state"], "hits": r["state_hits"], "scenarios": r["scenarios"],
            "invocations_total": None if candidate is None else len(candidate),
            "invocations_counted": None if counted is None else len(counted),
            "precision": r["precision_state"], "candidate_scenarios": r["candidate_scenarios"],
            "dependency_fields": r["dependency_fields"], "value_dependencies": r["value_dependencies"],
            "masked": r["masked"], "cuts": r["cuts"]}


def _from_class(caller: str | None, test_class: str) -> bool:
    return bool(caller) and (caller == test_class or caller.startswith(test_class + "$"))


def caller_stats(candidate: list[dict[str, Any]], test_class: str, scenario_test: str,
                 not_from_test_limit: float = 0.5) -> dict[str, Any]:
    """Where the candidate's invocations of the target come from. Level B drops the ones reached
    through the scenario test (own_invocations); these fields show whether the generated test calls
    the target itself.

      called_from_test_class   share of distinct candidate scenarios (level C) with at least one
                               invocation called from the generated class or its nested classes
      invocations_not_from_test invocations whose effective caller is another class
      via_scenario_invocations invocations with the scenario test class on the stack
      runs_scenario            the scenario class was on the stack, or more than
                               `not_from_test_limit` of the invocations come from other classes
    """
    scenario_class = scenario_test.split("#", 1)[0]
    norm = Normaliser({"types": [], "fields": {}})
    by_key: dict[str, bool] = {}
    not_from_test = via = 0
    known = 0
    for inv in candidate:
        caller = inv.get("caller_effective") or inv.get("caller")
        if caller is not None:
            known += 1
        own = _from_class(caller, test_class)
        if caller is not None and not own:
            not_from_test += 1
        if inv.get("via_scenario"):
            via += 1
        key = strict_key(inv, norm)
        by_key[key] = by_key.get(key, False) or own
    share = (sum(by_key.values()) / len(by_key)) if by_key and known else None
    reasons = []
    if via:
        reasons.append(f"scenario class {scenario_class} on the stack in {via} invocations")
    if known and not_from_test / known > not_from_test_limit:
        reasons.append(f"{not_from_test} of {known} invocations not called from the generated class")
    return {"called_from_test_class": share, "invocations_not_from_test": not_from_test,
            "via_scenario_invocations": via, "runs_scenario": bool(reasons),
            "runs_scenario_reasons": reasons}


def unstable_between_runs(a: list[dict[str, Any]], b: list[dict[str, Any]], norm: "Normaliser") -> dict[str, int]:
    """Paths that differ between two traces of the same scenario test, after normalisation.

    Invocations are matched by their structure key in order of appearance (the i-th invocation
    with a given structure in run 1 against the i-th in run 2), so that the comparison works when
    the runs have different numbers of invocations; unmatched invocations count as
    `[invocation count] <structure>`."""
    def groups(invs):
        g: dict[str, list] = {}
        for inv in invs:
            g.setdefault(structure_key(inv), []).append(inv)
        return g
    ga, gb = groups(a), groups(b)
    diffs: dict[str, int] = {}
    for key in set(ga) | set(gb):
        xs, ys = ga.get(key, []), gb.get(key, [])
        for x, y in zip(xs, ys):
            for p in unstable_paths(x, y, norm=norm):
                diffs[p] = diffs.get(p, 0) + 1
        if len(xs) != len(ys):
            label = f"[invocation count] {key[:200]}"
            diffs[label] = diffs.get(label, 0) + abs(len(xs) - len(ys))
    return diffs


def unstable_paths(a: Any, b: Any, path: str = "", norm: "Normaliser | None" = None) -> list[str]:
    """Paths where two traces of the same scenario differ after normalisation (audit of the
    nondeterminism list)."""
    if norm is not None and path == "":
        # a and b are invocations: normalise the value parts, keep outcome and call signatures
        def canon(inv: dict[str, Any]) -> dict[str, Any]:
            return {"structure": json.loads(structure_key(inv)), "self": norm.value(inv.get("self")),
                    "args": norm.value(inv.get("args", [])),
                    "value": norm.value(inv.get("value")) if inv.get("outcome") == "return" else None}
        a, b = canon(a), canon(b)
    if type(a) is not type(b):
        return [path or "/"]
    if isinstance(a, dict):
        out = []
        for k in set(a) | set(b):
            if k in ("$id", "$ref"):
                continue
            out += unstable_paths(a.get(k), b.get(k), f"{path}/{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}[len]"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += unstable_paths(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [path or "/"]
