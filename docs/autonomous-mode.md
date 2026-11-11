# Autonomous mode

The agent gets no recorded execution data; instead it can run the case's driving scenario under a
debugger, observe the executions of the target method and use the observed values in the test.
Everything else (prompts `system.txt` and `task-common.txt`, model, limits, guard, evaluation) is the
same as in the other modes.

## Prompt

`prompts/task-autonomous.txt` names the tools and the
condition: observed values are test data and expected results, invented values are not; when the data
are not sufficient, the breakpoints may be changed and the scenario run again. No procedure (number
of breakpoints, phases, evidence files) is prescribed.

## Tools (pi/debugger.ts)

| Tool | Arguments | Answer |
|---|---|---|
| `debug_start` | none | starts the scenario command of the case with the JDWP agent (`-Dmaven.surefire.debug=…suspend=y`, a port of the cell) and attaches; the JVM waits before its first instruction. Called again: the current run is killed and the scenario runs again (run #2, …); breakpoints are kept. At most `debug_runs` runs per cell (config, default 3; failed starts count): a further call answers `limit of N scenario runs reached; write the test from the data you have` and is journalled as `start_refused`. Any bash command ends the debug JVM, so the next `debug_start` uses up a run. |
| `set_breakpoint` | line form: `file`, `code` (the text of the line, at most 200 characters, compared with whitespace collapsed), optional `line`; method form: `method` (`pkg.Class#name`, argument types only for overloads), `on` = `entry` / `exit`; optional `condition` | `#id … set` / `pending (class not loaded yet)`. A text that occurs on several lines without `line` is an error listing the line numbers; a given `line` must hold the text. |
| `remove_breakpoint` | `id` | |
| `resume` | `timeout_s` (default 300 for the first resume of a run, 60 later; at most 600) | the next stop: kind (breakpoint, entry, exit, exception), breakpoint ids with hit numbers, at most 5 project frames (`… (+N project frames)`), the top frame: the fields of `this` one level deep (instance methods), the arguments to depth 1, other locals as values or `Type@N`; `returned = …` at an exit; `exception: X: "message" thrown out of <method>` when the method is left by an exception; `terminated: run #N ended, K stops` at the end of the run; `still running after N s` |
| `inspect` | `expr`, `frame` (default 0), `depth` (0–3, default 1) | the value; `inspect("locals", frame)` lists the locals of a frame; `inspect("this")` in a static method answers `static method: no this` |

Values: strings cut at 2000 characters (300 in the locals of a stop), collections and arrays at 20
elements (`…+N more`), objects at 40 fields, an answer at about 8 KB (the depth is lowered until it
fits; in the locals of a stop, expanded entries fall back to `Type@N`, the last first). Objects are shown as `Type@N`; `@N` can be used in later expressions of the same JVM run.

Expressions (`inspect`, `condition`): variables, `this`, `returned` / `exception` at an exit stop,
field chains, array indices, `@N`, literals, comparisons, arithmetic, and only these method calls:
`size`, `isEmpty` on JDK collections and maps, `get(int)` on JDK lists, `get`, `containsKey` on JDK
maps and `contains` on JDK sets with a string, boxed or enum key (not on a sorted container with a
comparator), `length`, `charAt`, `isEmpty` on strings, `toString`, `equals` on strings and boxes,
`getClass().getName()`. Any other call is refused (“this call could run code of the program and is not
allowed”).

No code of the program runs in the debugger: a method of the program could change the state being
observed (a lazy load, a cache) or leave other traces. JDK collections and maps (`ArrayList`,
`HashMap`, `LinkedHashMap`, `TreeMap`, `HashSet`, `ConcurrentHashMap`, immutable and unmodifiable
collections, ...) are shown and searched by walking their internal fields (`JdkContainers`), so no
`hashCode`, `equals`, `compareTo` or `toString` of a key runs; the allowed calls are answered from
fields as well (`JdkCalls`). Other collection types are shown as objects with their fields. Holders
(`AtomicReference`, `AtomicInteger`, `Optional`, ...) show the value they hold. JDK methods run only
for the text of a few immutable JDK value types (`BigDecimal`, `java.time`, `UUID`, `URI`, `Locale`,
`StringBuilder`); a string concatenation shows an object as `Type@N`. The integration test
`test_no_code_of_the_program_runs` checks this with keys that print a marker from these methods.
Objects shown as `@N` are kept from garbage collection until the JVM run ends (at most 10 000), so a
later `inspect("@N")` finds them.

The fields of class objects, class loaders, threads, `System`, `Runtime`, processes, modules,
reflection, `java.security`, `java.lang.invoke`/`ref` and JDK internals (`jdk.internal.*`, `sun.*`,
`com.sun.*`) are neither shown nor readable (“the fields of … are not shown”): they lead to the class
path, the file system or the environment, not to the state of the program. A breakpoint file outside
the workspace gets the same answer whether the file exists or not. The escape check
(`runner/escape_check.py`) also flags expressions that probe class loaders, the class path, threads or
the user directories; class names there are case-sensitive, so a field called `files` or `runtime` is
not taken for an escape.

Frames: a frame belongs to the project when its source file exists under a `src/{main,test}/java`
directory of the workspace; the top frame is always shown.

The scenario JVM is killed before every bash command of the agent (two Maven processes in one
workspace interfere) and when Pi exits; the runner kills what is left after a timeout.

## Backend

`pi/debugger/JdiDapServer.java` is derived from JdiDapServer.java of mcp-debugger (commit be84732,
MIT license, see `pi/debugger/NOTICE`):
the DAP framing, the JSON reader/writer and the expression evaluator are kept. Changed: attach only
(no launch, no class redefinition, no stepping), breakpoints on lines and on method entry/exit
(`MethodExitRequest` with the return value, `ExceptionRequest` for the exit by an exception, both
filtered to the thread and stack depth of the invocation), the method whitelist of the evaluator,
rendering with limits on the server. Events raised while the evaluator runs a JDK method are resumed
at once (they would otherwise deadlock the invocation). It is compiled by `scripts/setup.sh` or
`python3 -m runner.debugger --config config.yaml` into `<tools>/debugger/<hash of the source>/`.

## Journal and metrics

`artifacts/debug-log.jsonl` of a cell: one line per tool call (arguments, ok, answer size, seconds,
run number) and per stop (kind, place, whether the top frame — for an exception stop, the frame of
the method left — is the target method), plus the ends of runs. `summary.json: debugger` gives:
calls by tool, JVM runs and the run limit (`run_limit`, cells that used all runs, cells with a
refused start), failed starts, stops, stops in the target method, plan revisions (breakpoints set or
removed after the first stop), answer characters. The agent writes no evidence
file. `artifacts/scenario-debug-run<N>.log` keeps the output of every scenario run.

Escape check: every `inspect` expression and breakpoint condition is matched against file system,
process, environment, class loader and reflection names (the server refuses such calls anyway).

## Running

`debug_runs` (default 3) limits the scenario runs of a cell, is rendered into the prompt
(`{{debug_runs}}`) and enters the config hash of the autonomous and komar cells; `paths.debug_ports`
(default `5100-5199`) is the range of JDWP ports, one per cell at a time (lock files in
`$KEVAL_PORT_LOCKS`, default `<temp dir>/keval-debug-ports`, shared by every run of the machine).
