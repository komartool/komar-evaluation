"""Prompt templates (prompts/) rendered for one case and mode."""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from runner.cases import Case, eval_command, load_projects

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
class PromptError(Exception):
    pass


def render_template(text: str, context: dict[str, Any], name: str) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            missing.append(key)
            return match.group(0)
        return str(context[key])

    out = PLACEHOLDER.sub(replace, text)
    if missing:
        raise PromptError(f"{name}: unknown placeholder(s): {', '.join(sorted(set(missing)))}")
    return out


def context(case: Case, commands: dict[str, list[str]], mode: str, debug_runs: int = 3) -> dict[str, Any]:
    """Placeholders of the templates. The scenario is given the same way for every case and mode:
    the id of the scenario test and the path of its source file."""
    project = load_projects()[case.project]
    return {
        "case_id": case.id,
        "repository": project.checkout_name,
        "commit": project.commit,
        "language": "java",
        "jdk": project.jdk,
        "working_directory": ".",
        "target_class": case.target["class"],
        "target_method": case.target["method"],
        "target_signature": case.target["signature"],
        "scenario_description": f"test {case.scenario_test}",
        "scenario_source_paths": case.scenario_source,
        "output_test_file": case.output_test_file,
        "commands": json.dumps(commands, indent=2),
        "command_names": ", ".join(sorted(commands)),
        "debug_runs": debug_runs,
        # komar mode: the scenario command as the harness checks it (runner/cell.py, komar_input)
        "komar_scenario": " ".join(shlex.quote(a) for a in eval_command(case, "scenario")),
    }


def render(prompts_dir: Path, case: Case, mode: str, commands: dict[str, list[str]],
           debug_runs: int = 3) -> dict[str, str]:
    ctx = context(case, commands, mode, debug_runs)
    files = ("system.txt", "task-common.txt", f"task-{mode}.txt")
    text = {}
    for name in files:
        path = prompts_dir / name
        if not path.is_file():
            raise PromptError(f"prompt file not found: {path}")
        text[name] = render_template(path.read_text(encoding="utf-8"), ctx, name)
    return {"system": text["system.txt"], "task": f"{text['task-common.txt']}\n{text[f'task-{mode}.txt']}"}
