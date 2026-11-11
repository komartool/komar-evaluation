# komar-evaluation

Evaluation of [Komar](https://github.com/komartool/komar) as a runtime-observation interface for a coding
agent. The task is generating a unit test for a target method from the execution of a high-level
scenario test. The agent ([Pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) with any
OpenAI-compatible model) observes that execution in one of three ways, and the generated test is compared
with the recorded behaviour of the target method.

## Modes

| Mode | What the agent gets besides the code |
|---|---|
| `baseline` | nothing: the scenario test and the production code only |
| `autonomous` | an interactive debugger over the scenario (`debug_start`, `set_breakpoint`, `remove_breakpoint`, `resume`, `inspect`; `docs/autonomous-mode.md`) |
| `komar` | Komar over the scenario: the agent writes a YAML spec of probes, runs the scenario under Komar and reads the compact view of the recorded run (`docs/komar-mode.md`) |

Everything else is the same in every mode: the system prompt and the common task prompt, the model and its
settings, the wall-clock limit, the three commands the agent may run (`build`, `scenario`,
`generated_test`, with the same Maven arguments as every other run of the case), the workspace guard.
`autonomous` and `komar` may run the scenario at most `debug_runs` times (default 3): a debugger run counts
once its JVM has reached the debugger, a Komar run once the spec is valid.

## Metric: level B

A scenario is a distinct invocation of the target method in the reference trace of the case (the
scenario test run twice under a BTrace tracer; both runs must agree). Level B of a generated test is the
share of reference scenarios that the test reproduces: the outcome (return or exception class), the
signatures of the calls the target method makes, the arguments, the returned value and the receiver state
(field graphs up to depth 5). A cell whose test does not compile or pass scores 0, as does a test that
passes but fails under the tracer (counted apart). Only the invocations the generated test makes itself
count: an invocation reached through the scenario test (its class on the stack) is dropped.

Precision is the share of the test's distinct counted invocations of the target that match a reference
scenario at level B; it is reported next to B and does not enter it.

Collaborators. A unit test may replace a collaborator of the target method with a stub, so some values are
compared by presence only (null or not):

- receiver fields that hold, **in the reference**, a mock or proxy, a framework component, a class from the
  project's test sources or an object of an infrastructure type (logging, JDBC drivers, transaction and
  context objects), plus the receiver fields labelled `DEPENDENCY` for the case in
  `evaluation/dependency-labels.json`;
- positions inside the arguments and the result that hold such an object in the reference.

Every other field and position is compared by content, also where the generated test puts a mock: a mock
in place of a data object of the reference does not match.

Dependency labels. The 277 object-valued receiver fields of the cases were labelled `DEPENDENCY` (an object
the target method delegates work to, which a unit test may replace) or `STATE` (an object whose data
determine the behaviour or the result) by a language model that saw the target class and the developers'
tests of the class, never a generated test or a result: two runs with different phrasings of the same
definition, disagreements decided by a third run (`scripts/dep_labels.py`; agreement and the model are in
the file's `meta`; `<out>/review.csv` lists every label for a manual check).

The same normalisation applies to every mode: time values, UUIDs, identity hash codes, workspace and
temporary paths, JDK collections by interface, and the types and fields of
`evaluation/nondeterminism.json`. The tracer writes at most 200 elements of a collection; differences
deeper than the trace depth are not seen.

## Cases

101 cases in 11 open-source Java projects (`projects.yaml`: repository, commit, JDK, license):

| List | Projects | JDK |
|---|---|---|
| `cases/cases.csv` (70) | Apollo, Checkstyle, JGit, jsoup, Klaw, Liquibase, MyBatis 3 | 21 |
| `cases/cases-jdk11.csv` (31) | PDFBox, GraphHopper, Spring PetClinic, Lab-Insurance | 11 |

`cases/<id>/case.yaml`: target method, scenario test, output test class, hidden tests (the existing tests
of the target class, removed from the agent's workspace), build and test commands. `references/<id>/`: the
reference traces (`run1.json`, `run2.json`) and their audit, recorded by `evaluation/reference.py`
(`scripts/run.py` records them when they are missing).

## Setup

Requirements: Linux or macOS, 8+ cores and 16 GB RAM recommended; JDK 21 (and JDK 11 for
`cases-jdk11.csv`), Maven 3.9, git, Python 3.10+ with PyYAML, curl; an API key of an OpenAI-compatible
gateway.

```bash
cp config.example.yaml config.yaml        # gateway, model, paths
scripts/setup.sh config.yaml              # Node.js + Pi, BTrace, the debugger server, the subject projects
scripts/setup_komar.sh config.yaml        # Komar from github.com/komartool/komar at komar_commit
scripts/install_jdk11.sh <dir>            # JDK 11 for cases-jdk11.csv (then a second config, see below)
```

`setup.sh` clones the projects at their commits and builds them once online, so that every case command
runs offline afterwards. `setup_komar.sh` builds Komar from source with a pinned Go and Gradle (downloads
are sha256-checked).

For the 31 JDK 11 cases use a second config with `jdk: 11`, `case_list: cases-jdk11.csv` and
`paths.java_home` pointing at the JDK 11 (and its own `paths.projects`, `paths.results`), then run
`scripts/setup.sh` with it. Komar itself is built with JDK 21+ and its agent runs on JDK 11; with a JDK 11
config give the build JDK explicitly:

```
KOMAR_BUILD_JDK=/opt/jdk-21 scripts/setup_komar.sh config-jdk11.yaml
```

## Configuration

`config.example.yaml` documents every key. The model settings:

| Key | Environment | Command line | What |
|---|---|---|---|
| `provider.base_url` | `KEVAL_BASE_URL` | `--base-url` | OpenAI-compatible endpoint (`.../v1`) |
| `provider.api_key_env` | — | — | name of the environment variable that holds the key (default `KEVAL_API_KEY`) |
| `provider.fallback_api_key_env` | — | — | optional second key, used to retry after a gateway error |
| `model` | `KEVAL_MODEL` | `--model` | model id at the gateway |
| `thinking` | `KEVAL_THINKING` | `--thinking` | reasoning effort: `off`, `minimal`, `low`, `medium`, `high`, `xhigh` |
| `timeout` | `KEVAL_TIMEOUT` | `--timeout` | wall-clock limit of one agent session, s |
| `rounds` | `KEVAL_ROUNDS` | `--rounds` | rounds, e.g. `[1, 2]` |
| `parallel_cases` | `KEVAL_PARALLEL_CASES` | `--parallel-cases` | cases processed at the same time |
| `modes` | — | `--modes` | any of `baseline`, `autonomous`, `komar` |
| `komar_commit` | `KOMAR_COMMIT` | `--komar-commit` | full commit hash of komartool/komar |

The command line overrides the environment, which overrides the file. The key itself is read only from the
environment and reaches only the Pi provider extension. Changing a setting changes the input hashes of the
cells it affects (see below).

## Running

```bash
export KEVAL_API_KEY=...

# smoke: one case per project kind, all three modes, one round
python3 scripts/run.py --config config.yaml --rounds 1 \
    --cases jsoup-token-queue-chomp-balanced klaw-approval-service-add-approval

python3 scripts/run.py --config config.yaml              # the full run (in tmux or similar)
python3 scripts/run.py --config config.yaml --status
python3 -m evaluation.aggregate --config config.yaml --config config-jdk11.yaml
```

For every case `scripts/run.py` records the reference traces if missing, then generates and evaluates every
cell (mode × round). A cell is a fresh export of the project commit with the hidden tests removed; the agent
may read the workspace, write only the output test class and run only the three case commands (and, in
the komar mode, Komar and the view commands). The evaluation runs on a fresh copy with the result diff
applied: `verify` (compiles and passes; at least one test run, none failed or skipped) and `b` (the test
runs under the tracer and is compared with the reference).

Before the first cell one request checks that the model answers. A cell that failed for an infrastructure
reason (gateway errors, a workspace that could not be prepared) is moved aside as `<run-id>.infra-*` and
generated again; a 401/403 stops the run. After the agent, `runner/escape_check.py` checks that neither the
agent nor its test reached outside the workspace.

Everything is resumable. Every cell and evaluation step records hashes of its inputs (code, case, prompts,
config, project, Komar build, normalisation lists, dependency labels, reference); a stored result whose
inputs changed is never reused silently. `python3 -m evaluation.evaluate --config config.yaml --steps b
--force --reuse-traces` recomputes level B from the kept candidate traces without running the tests again.

`evaluation.aggregate` reports, per case list and together: cells, passing cells, mean B and mean precision
per mode (a case is the mean over its rounds), cells where invocations through the scenario were dropped,
cells whose test failed under the tracer, the runs of the debugger (JVM runs, failed and refused starts) and
of Komar (runs, spec errors, refused runs), and every pair of modes compared per case (cases, mean
difference, bootstrap 95% interval, wins/ties/losses, Wilcoxon signed-rank p). It stops if a cell of the
config is incomplete (not generated, not evaluated, or a passing test without level B) and lists those
cells: rerun the evaluation, or the cell under the infrastructure rule; `--exclude-incomplete` drops their
cases from every mode instead.

## Relabelling the dependency fields

```bash
python3 scripts/dep_labels.py --config config.yaml units
python3 scripts/dep_labels.py --config config.yaml label --run p1
python3 scripts/dep_labels.py --config config.yaml label --run p2
python3 scripts/dep_labels.py --config config.yaml merge          # disagreements -> p3
python3 scripts/dep_labels.py --config config.yaml label --run p3
python3 scripts/dep_labels.py --config config.yaml merge          # <out>/labels.json, review.csv
```

The labelling model is `labelling.model` of the config (`KEVAL_LABEL_MODEL`); the gateway and the key are
those of `provider` unless `labelling` sets its own. The prompts are in `evaluation/labelling-prompts/`.
Point `evaluation.dependency_labels` at the new file (or replace `evaluation/dependency-labels.json`); the
`b` steps whose labels changed are then stale and are recomputed with `--steps b --force --reuse-traces`.

## Layout

| Path | What |
|---|---|
| `cases/`, `projects.yaml`, `references/` | the cases, the subject projects, the reference traces |
| `prompts/` | system prompt, common task prompt, one block per mode |
| `pi/` | Pi extensions: model provider, workspace guard, the debugger tools; `pi/debugger/` the JDI server |
| `runner/` | one generation cell: workspace, scenario check, agent, Komar wrapper, debugger, escape check |
| `evaluation/` | tracer, references, comparison (level B), dependency labels, evaluation of a cell, aggregation |
| `scripts/` | setup, the run, scenario checks, labelling |
| `tests/` | unit tests: `python3 -m unittest discover -s tests` (tests that need an installed Komar, JDK or Maven repository skip themselves) |

## Known limitations

- There is no operating-system sandbox. The guard confines the agent's tools to the workspace and the
  allowed commands; build and test commands execute project code, including the generated test. The escape
  check records findings per cell; no cell is excluded automatically.
- Other tests of a project may still call the target method and are visible to the agent in every mode.
- Model latency counts against the wall-clock limit of a cell.

## Licenses

Apache License 2.0 (`LICENSE`). `pi/debugger/JdiDapServer.java` is derived from mcp-debugger (MIT,
`pi/debugger/NOTICE`). The subject projects are not included; `scripts/setup.sh` clones them. The cases and
references contain fragments of their code and data under the projects' licenses: Apache-2.0 (Apollo, Klaw,
MyBatis 3, PDFBox, GraphHopper, Spring PetClinic, Lab-Insurance), MIT (jsoup), LGPL-2.1 (Checkstyle), EDL-1.0 (JGit),
FSL-1.1-ALv2 (Liquibase). BTrace (GPL-2.0) and Komar are downloaded by
the setup and not redistributed.
