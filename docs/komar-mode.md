# Komar mode

The `autonomous` mode with the debugger replaced by [Komar](https://github.com/komartool/komar), a tool
that records what a JVM program does from a YAML spec. The agent gets no recorded execution data. It
writes a spec, runs the case's driving scenario under Komar, reads the compact view of the run and uses
the observed values in the test. Everything else is the same as in the autonomous mode: `system.txt` and
`task-common.txt`, model, limits, the guard, the limit of scenario runs (`debug_runs`) and the evaluation.

## Prompt

`prompts/task-komar.txt` replaces `task-autonomous.txt`. Its first paragraph is the debugger paragraph
with the tool names replaced. The remainder tells the agent where to write specs, gives the run command
and how to read the view, and carries a short extract of the Komar documentation at the pinned commit:
the spec example, the probe kinds and matching rules, the CEL variables, how values are read, the limits
with their defaults, and a description of the compact view with an example. The extract describes only
features that Komar has at the pinned commit; it gives no procedure.

## Commands

`pi/guard.ts` accepts these command forms besides the named case commands. It reads them word by word,
as bash would, and rejects them when bash would not read them literally (operators, expansions, globs,
comments, line breaks):

    komar run komar/NAME.yaml -o komar/NAME.jsonl -- <the scenario command of the case>
    sed -n 'A,Bp' komar/NAME.komar
    cut -c A-B komar/NAME.komar        (also A- and -B)

The scenario command is the one the harness checks and the debugger runs: `eval_command(case,
"scenario")`, with the project's `eval_maven_args`. `write` and `edit` may also write `komar/NAME.yaml`.
`komar run` writes the dump `NAME.jsonl` and, next to it, the compact view `NAME.komar`: the hits per
probe, warnings, the end record and each distinct execution of a probed method once, as a tree with its
collaborator calls and its outcome.

The agent reads only the view, with the read tool; `sed` and `cut` serve lines longer than the read tool
shows (the prompt does not mention them). The guard refuses `read` and `grep` of `komar/*.jsonl` and a
`grep` glob that could match the dumps; `runner/komar.py` adds `/komar/*.jsonl` to `.git/info/exclude`,
so a plain `grep` of the workspace, which keeps the ignore rules, skips them. `komar/` is a scratch
directory of the workspace. It is not part of the result, and the escape check flags a generated test
that names it.

`runner/komar.py` has the same rules in Python; `tests/test_komar.py` checks that both agree on a table
of commands.

## The wrapper and the run limit

`komar` on the PATH of the agent's commands is a wrapper (`runner/komar.py`, `wrapper`). For each call it:

- refuses a run beyond `debug_runs` (default 3) with `komar: limit of N scenario runs reached; write the
  test from the data you have`;
- refuses a spec with `calls: true`. As in the debugger, no code of the program runs for the observation.
  Komar reads values from fields unless a spec enables calls;
- runs the built binary with the installed agent jar (`KOMAR_AGENT_JAR`, nothing is extracted to the
  user cache) under a supervisor in a session of its own, which notes every process group of the run
  (Komar starts the scenario in a group of its own) in `komar-pids.txt`;
- bounds a run by 300 s (`RUN_TIMEOUT`, as the first `resume` of the debugger). The guard drops the
  `timeout` the agent gives the bash command of a `komar run`, so the agent cannot cut a run short. When
  the run outlives 300 s, the supervisor kills every group of the run (Komar, Maven, the test JVMs), the
  run counts as a run (`timed_out` in the journal), and the agent reads `komar: Komar run stopped after
  300 s; the scenario did not finish`. When the wrapper itself dies, the supervisor kills the run as well;
- journals the call to `artifacts/komar-log.jsonl`: spec text, exit code, seconds, output and view
  sizes, hits per probe, diagnostics by level, the `end` record.

A spec that Komar rejects (exit code 2, nothing started) does not count as a run. Neither does a refused
`calls: true`. The process groups of runs still alive when the agent stops are killed.

`summary.json` gets `komar`, with the counts of the cell: runs, spec errors, refused runs, runs without
an `end` record, runs whose scenario failed, hits, diagnostics, output bytes, view commands. It also gets
`komar_versions`. The specs, dumps and views of the cell are kept under `artifacts/komar/`; each file is
cut at 2 MB.

## Installation

    scripts/setup_komar.sh <config.yaml> [<dir>]

The script clones https://github.com/komartool/komar, checks out `komar_commit` of the config (a full
commit hash; `KOMAR_COMMIT` in the environment overrides it, `KOMAR_URL` the repository) and builds it
from source into `paths.komar`, by default `<paths.tools>/komar`. It uses Go 1.26.10, Gradle 9.8.2 and
the config's JDK 21. The Go and Gradle downloads are sha256-checked; Go modules are checked by Komar's
`go.sum`.

`versions.json` records the commit, the toolchain and the sha256 of the binary and the jar. Its hash is
the `mode_input` of the komar cells (`evaluation/inputs.py`). A run refuses an installation built from
another commit than the config's.
