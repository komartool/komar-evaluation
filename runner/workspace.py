"""Workspaces: an exported commit of the subject project with the hidden tests removed."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path


class WorkspaceError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result


def export_commit(checkout: Path, commit: str, dest: Path) -> Path:
    """Plain copy of the commit (git archive): no history, no build output."""
    if not (checkout / ".git").exists():
        raise WorkspaceError(f"not a git checkout: {checkout} (run scripts/setup.sh)")
    if dest.exists():
        raise WorkspaceError(f"workspace already exists: {dest}")
    dest.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(checkout), "archive", "--format=tar", commit], capture_output=True)
    if archive.returncode != 0:
        raise WorkspaceError(f"git archive {commit} failed: {archive.stderr.decode(errors='replace').strip()}")
    subprocess.run(["tar", "-x", "-f", "-", "-C", str(dest)], input=archive.stdout, check=True)
    return dest


def hide_paths(workspace: Path, paths: list[str]) -> list[str]:
    """Remove the existing tests of the target class; returns the removed paths."""
    removed = []
    for rel in paths:
        p = workspace / rel
        if p.is_dir():
            shutil.rmtree(p)
            removed.append(rel)
        elif p.exists():
            p.unlink()
            removed.append(rel)
    return removed


def install_tree(source: Path | None, workspace: Path) -> list[str]:
    """Copy a directory tree into the workspace (same relative paths); returns the paths."""
    if source is None:
        return []
    installed = []
    for f in sorted(source.rglob("*")):
        if f.is_file():
            rel = f.relative_to(source).as_posix()
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            installed.append(rel)
    return installed


def init_repo(workspace: Path) -> None:
    """A single-commit repository, so that the agent sees no project history."""
    git(workspace, "init", "--quiet", "-b", "main")
    git(workspace, "config", "user.email", "runner@localhost")
    git(workspace, "config", "user.name", "runner")
    git(workspace, "config", "commit.gpgsign", "false")
    git(workspace, "add", "--all")
    git(workspace, "commit", "--quiet", "--allow-empty", "-m", "prepared workspace")


def snapshot(workspace: Path) -> dict[str, str]:
    out = {}
    for f in workspace.rglob("*"):
        rel = f.relative_to(workspace).as_posix()
        if f.is_file() and not f.is_symlink() and not (rel == ".git" or rel.startswith(".git/")):
            out[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


BUILD_OUTPUT = ("target/", "build/", ".gradle/", ".mvn/wrapper/")


def unexpected_changes(before: dict[str, str], after: dict[str, str], allowed: set[str]) -> list[str]:
    """Files changed by the agent other than the allowed ones (build output ignored)."""
    changed = {p for p in set(before) ^ set(after)} | {p for p in set(before) & set(after) if before[p] != after[p]}
    return sorted(p for p in changed if p not in allowed
                  and not any(p.startswith(b) or f"/{b}" in p for b in BUILD_OUTPUT))


def result_diff(checkout: Path, commit: str, files: dict[str, Path], dest: Path) -> str:
    """Diff of the generated files against the original commit (applies with `git apply`)."""
    git(checkout, "clone", "--quiet", "--no-checkout", str(checkout), str(dest))
    git(dest, "checkout", "--quiet", "--detach", commit)
    git(dest, "config", "user.email", "runner@localhost")
    git(dest, "config", "user.name", "runner")
    for rel, source in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        git(dest, "add", "--force", "--", rel)
    if not git(dest, "diff", "--cached", "--name-only").stdout.strip():
        return ""
    return git(dest, "diff", "--cached", "--binary", commit, "--", *files).stdout
