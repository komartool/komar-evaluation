"""Labels of receiver fields as DEPENDENCY or STATE (evaluation/dependency-labels.json, level B).

Level B compares the receiver of the target method; its dependency fields are compared by presence
only. Which fields are dependencies is decided from the reference (compare.level_b): the fields that
hold a mock, proxy, component, test-source class or masked infrastructure object in the reference, plus
the fields labelled DEPENDENCY here. A language model labels every object-valued receiver field of a
case from the sources of the case (scripts/dep_labels.py); it never sees generated tests or results.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from evaluation import compare as C

LABELS = ("DEPENDENCY", "STATE")


def receiver_fields(invocations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Top-level receiver fields as the comparison names them, with the runtime classes seen in the
    reference and whether the field holds an object (compare._is_object) in some invocation."""
    out: dict[str, dict[str, Any]] = {}
    for inv in invocations or []:
        self_ = inv.get("self")
        if not (isinstance(self_, dict) and "$fields" in self_):
            continue
        for raw, v in self_["$fields"].items():
            name = C.field_name(raw, self_["$fields"])
            f = out.setdefault(name, {"raw": raw, "object": False, "classes": set(), "kinds": set()})
            if C._is_object(v):
                f["object"] = True
                if isinstance(v, dict) and v.get("$t"):
                    f["classes"].add(C.norm_type(v["$t"]))
            f["kinds"].add(_kind(v))
    for f in out.values():
        f["classes"] = sorted(f["classes"])
        f["kinds"] = sorted(f["kinds"])
    return out


def _kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, (bool, int, float, str)):
        return "scalar"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        if "$enum" in v:
            return "enum"
        if C._is_object(v):
            return "object"
        return "jdk"
    return "other"


def parse_response(text: str) -> dict[str, dict[str, str]]:
    """{"fields": [{"name", "label", "reason"}]} from a model answer (code fences tolerated).
    Labels are upper-cased; anything but DEPENDENCY/STATE raises ValueError."""
    s = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the answer")
    data = json.loads(s[start:end + 1])
    out: dict[str, dict[str, str]] = {}
    for item in data.get("fields", []):
        label = str(item.get("label", "")).strip().upper()
        if label not in LABELS:
            raise ValueError(f"bad label {item.get('label')!r} for {item.get('name')!r}")
        out[str(item["name"])] = {"label": label, "reason": str(item.get("reason", "")).strip()}
    return out


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    if not a or len(a) != len(b):
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((a.count(k) / n) * (b.count(k) / n) for k in LABELS)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def majority(votes: Iterable[str]) -> str | None:
    vs = [v for v in votes if v]
    if not vs:
        return None
    best = max(LABELS, key=lambda k: vs.count(k))
    return best if vs.count(best) * 2 > len(vs) else None
