"""Surefire reports of the generated test class: how many tests ran, failed, errored, were skipped.

A Maven exit code alone does not show that a test ran: the case commands carry
-DfailIfNoTests=false, so a class whose tests are all @Disabled, all stopped by an assumption, or
not seen by the project's JUnit engine (a Jupiter @Test in a JUnit 4 module) exits with 0.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)


def module_dir(workspace: Path, test_file: str) -> Path:
    """Directory of the Maven module that contains the test file (nearest pom.xml)."""
    p = (workspace / test_file).parent
    while p != workspace and not (p / "pom.xml").is_file():
        p = p.parent
    return p


def test_class_name(workspace: Path, test_file: str) -> str:
    src = (workspace / test_file).read_text(encoding="utf-8", errors="replace")
    m = PACKAGE.search(src)
    return (m.group(1) + "." if m else "") + Path(test_file).stem


def test_class_name_from_diff(diff: Path, test_file: str) -> str:
    """Fully qualified name of the generated class, read from the added lines of result.diff."""
    added = "\n".join(line[1:] for line in diff.read_text(encoding="utf-8", errors="replace").splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    m = PACKAGE.search(added)
    return (m.group(1) + "." if m else "") + Path(test_file).stem


def report_files(workspace: Path, test_file: str) -> list[Path]:
    """TEST-<class>.xml of the generated class and of its nested classes (JUnit 5 @Nested)."""
    reports = module_dir(workspace, test_file) / "target" / "surefire-reports"
    name = test_class_name(workspace, test_file)
    return sorted(p for p in reports.glob("TEST-*.xml")
                  if p.stem[len("TEST-"):] == name or p.stem[len("TEST-"):].startswith(name + "$"))


def counts(workspace: Path, test_file: str) -> dict[str, Any]:
    """Totals over the report files of the class; reports=0 when surefire wrote none."""
    out = {"reports": 0, "tests_run": 0, "failures": 0, "errors": 0, "skipped": 0}
    for f in report_files(workspace, test_file):
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        out["reports"] += 1
        for s in suites:
            for key, attr in (("tests_run", "tests"), ("failures", "failures"), ("errors", "errors"),
                              ("skipped", "skipped")):
                try:
                    out[key] += int(s.get(attr) or 0)
                except ValueError:
                    pass
    return out


def passes(exit_ok: bool, c: dict[str, Any]) -> bool:
    """A test passes when the command succeeded and at least one test ran, none failed, errored
    or was skipped."""
    return bool(exit_ok and c["tests_run"] >= 1 and c["failures"] == 0 and c["errors"] == 0
                and c["skipped"] == 0)
