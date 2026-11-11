import { appendFileSync, existsSync, readFileSync, realpathSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createBashTool } from "@earendil-works/pi-coding-agent";

/**
 * Pi extension that confines the agent to its workspace:
 *   - write/edit: only the generated test class;
 *   - read/grep/find/ls: only paths inside the workspace (symlinks resolved);
 *   - bash: only the named case commands, exactly as given: no redirection, no background, no
 *     second command (the command string must equal an allowed argv joined by spaces, up to
 *     repeated spaces or tabs; a line break is never accepted).
 * Denied calls return an error to the agent and are logged. Every command run and every version of
 * the generated class are logged for the runner. The debugger tools of the autonomous mode
 * (pi/debugger.ts) are allowed and logged here too; that extension confines them itself.
 * In the komar mode (config `komar`), bash also runs two more command forms, read word by word
 * (komarCommandKind; runner/komar.py has the same rules): the scenario under Komar with a spec and an
 * output in the scratch directory komar/, and jq over outputs there; write/edit may also write the
 * specs komar/NAME.yaml. With `komar.compact` (set by the runner in the komar mode) there is no jq: the agent
 * reads the compact view komar/NAME.view, and no tool reads the dumps komar/*.jsonl (compactReadDenied).
 *
 * Commands run with an environment reduced to GUARD_ENV_ALLOW (the runner's command environment):
 * no API key, no Pi or guard variables, no session metadata reach the project code.
 *
 * Environment (set by the runner): GUARD_CONFIG (JSON: workspace, output_test_file, commands),
 * GUARD_DENIED_LOG, GUARD_COMMAND_LOG, GUARD_OUTPUT_VERSION_LOG.
 */
interface GuardConfig {
	workspace: string;
	output_test_file: string;
	commands: Record<string, string[]>;
	/** names of the environment variables that commands may see */
	command_env: string[];
	/** komar mode: the scenario command the agent runs under Komar; compact: no jq, no reading the dumps */
	komar?: { scenario: string[]; compact?: boolean };
}

export function commandEnv(env: NodeJS.ProcessEnv, allowed: string[]): NodeJS.ProcessEnv {
	const keep = new Set(allowed);
	const out: NodeJS.ProcessEnv = {};
	for (const [k, v] of Object.entries(env)) {
		if (keep.has(k) && v !== undefined) {
			out[k] = v;
		}
	}
	return out;
}

function shellQuote(value: string): string {
	return /^[A-Za-z0-9_@%+=:,./-]+$/.test(value) ? value : `'${value.replace(/'/g, "'\\''")}'`;
}

/** Spaces and tabs collapsed; null when the command spans several lines (bash would run each). */
export function canonicalCommand(command: string): string | null {
	if (/[\r\n\u2028\u2029]/.test(command)) {
		return null;
	}
	return command.trim().replace(/[ \t]+/g, " ");
}

export function allowedCommands(commands: Record<string, string[]>): Map<string, string> {
	const allowed = new Map<string, string>();
	for (const [name, argv] of Object.entries(commands ?? {})) {
		allowed.set(argv.map(shellQuote).join(" "), name);
		allowed.set(argv.join(" "), name);
	}
	return allowed;
}

/** Name of the allowed command that `command` is, or undefined (deny). */
export function matchCommand(allowed: Map<string, string>, command: string): string | undefined {
	const canonical = canonicalCommand(command);
	return canonical === null ? undefined : allowed.get(canonical);
}

const KOMAR_NAME = "[A-Za-z0-9_-][A-Za-z0-9._-]*";
export const KOMAR_SPEC = new RegExp(`^komar/${KOMAR_NAME}\\.yaml$`);
const KOMAR_OUTPUT = new RegExp(`^komar/${KOMAR_NAME}\\.jsonl$`);
const KOMAR_VIEW = new RegExp(`^komar/${KOMAR_NAME}\\.view$`);
const JQ_FLAGS = /^-[crsnSej]+$/;
const JQ_LONG_FLAGS = new Set(["--compact-output", "--raw-output", "--slurp", "--null-input", "--sort-keys", "--tab",
	"--exit-status", "--join-output"]);

/** The words bash reads from `command` if it reads them literally (plain words of safe characters, '...'
 * and "..." without $, backquote, backslash or !), or null (operators, expansions, globs, comments). */
export function shellWords(command: string): string[] | null {
	if (/[\r\n\u2028\u2029]/.test(command)) {
		return null;
	}
	const s = command.trim();
	const words: string[] = [];
	let cur: string | null = null;
	let i = 0;
	while (i < s.length) {
		const c = s[i];
		if (c === " " || c === "\t") {
			if (cur !== null) {
				words.push(cur);
				cur = null;
			}
			i++;
		} else if (c === "'") {
			const j = s.indexOf("'", i + 1);
			if (j < 0) return null;
			cur = (cur ?? "") + s.slice(i + 1, j);
			i = j + 1;
		} else if (c === '"') {
			const j = s.indexOf('"', i + 1);
			if (j < 0 || /[$`\\!]/.test(s.slice(i + 1, j))) return null;
			cur = (cur ?? "") + s.slice(i + 1, j);
			i = j + 1;
		} else if (/[A-Za-z0-9_@%+=:,./#-]/.test(c) && !(c === "#" && cur === null)) {
			cur = (cur ?? "") + c;
			i++;
		} else {
			return null;
		}
	}
	if (cur !== null) words.push(cur);
	return words;
}

/** "komar" or "jq" for a command of the two forms of the komar mode, undefined otherwise; with `compact`
 * (the komar mode) the komar form and "view": `sed -n 'A,Bp'` or `cut -c A-B` of one view komar/NAME.view,
 * for lines too long for the read tool (its hint names sed). */
export function komarCommandKind(command: string, scenario: string[], compact = false): "komar" | "jq" | "view" | undefined {
	const words = shellWords(command);
	if (!words || words.length === 0) return undefined;
	if (words[0] === "komar") {
		const ok = words.length > 6 && words[1] === "run" && KOMAR_SPEC.test(words[2]) && words[3] === "-o"
			&& KOMAR_OUTPUT.test(words[4]) && words[5] === "--" && words.length - 6 === scenario.length
			&& scenario.every((a, k) => words[6 + k] === a);
		return ok ? "komar" : undefined;
	}
	if (compact && words.length === 4 && KOMAR_VIEW.test(words[3])
		&& ((words[0] === "sed" && words[1] === "-n" && /^\d+(?:,\d+)?p$/.test(words[2]))
			|| (words[0] === "cut" && words[1] === "-c" && /^(?:\d+-?\d*|-\d+)$/.test(words[2])))) {
		return "view";
	}
	if (words[0] === "jq" && !compact) {
		let i = 1;
		let nullInput = false;
		while (i < words.length && words[i].startsWith("-")) {
			const w = words[i];
			if (w === "--arg" || w === "--argjson") {
				if (i + 2 >= words.length || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(words[i + 1])) return undefined;
				i += 3;
				continue;
			}
			if (!(JQ_FLAGS.test(w) || JQ_LONG_FLAGS.has(w))) return undefined;
			nullInput = nullInput || w === "--null-input" || (!w.startsWith("--") && w.includes("n"));
			i++;
		}
		const rest = words.slice(i);
		if (rest.length === 0 || /\b(?:import|include)\b/.test(rest[0])) return undefined;
		const files = rest.slice(1);
		if (files.length === 0 && !nullInput) return undefined;
		return files.every((f) => KOMAR_OUTPUT.test(f)) ? "jq" : undefined;
	}
	return undefined;
}

const KOMAR_DUMP = /^komar\/.*\.jsonl$/i;
// a grep glob ending in a plain extension other than jsonl, or a negated glob: either keeps the ignore
// rules, so the dumps (excluded in .git/info/exclude by runner/komar.py) stay out of the search
const GREP_SAFE_GLOB = /^(?:!.*|[^{}[\]]*\.(?!jsonl$)[A-Za-z0-9_-]+)$/i;

/** compact view: why a read/grep/find/ls call would show a Komar dump, or undefined. `rel` is the
 * workspace-relative path of the call (symlinks resolved), `glob` the glob of a grep call. */
export function compactReadDenied(tool: string, rel: string, glob?: string): string | undefined {
	const path = rel === "" ? "." : rel;
	if ((tool === "read" || tool === "grep") && (KOMAR_DUMP.test(path) || /^komar\/.*\.jsonl\//i.test(path))) {
		return "the raw Komar dumps komar/*.jsonl are not available; read the compact view komar/NAME.view";
	}
	const scopeHasKomar = path === "." || path === "komar" || path.startsWith("komar/");
	if (tool === "grep" && glob !== undefined && glob !== "" && scopeHasKomar && !GREP_SAFE_GLOB.test(glob)) {
		return "this glob would search the raw Komar dumps komar/*.jsonl; use a glob with a file extension, e.g. '*.java'";
	}
	return undefined;
}

/** Tools of pi/debugger.ts (autonomous mode); Pi offers them only when that extension is loaded. */
export const DEBUGGER_TOOLS = ["debug_start", "set_breakpoint", "remove_breakpoint", "resume", "inspect"];

export default function (pi: ExtensionAPI) {
	const configPath = process.env.GUARD_CONFIG;
	if (!configPath) {
		return;
	}
	const config = JSON.parse(readFileSync(configPath, "utf-8")) as GuardConfig;
	const workspace = resolve(config.workspace);
	const outputPath = resolve(workspace, config.output_test_file);
	const deniedLog = process.env.GUARD_DENIED_LOG;
	const commandLog = process.env.GUARD_COMMAND_LOG;
	const versionLog = process.env.GUARD_OUTPUT_VERSION_LOG;
	let lastOutputVersion: string | undefined;

	// Record every state of the generated test class, so that the first attempt stays observable
	// after later repairs (the runner only sees the final file).
	function recordOutputVersion(toolName: string) {
		if (!versionLog || !existsSync(outputPath)) {
			return;
		}
		let content: string;
		try {
			content = readFileSync(outputPath, "utf-8");
		} catch {
			return;
		}
		if (content === lastOutputVersion) {
			return;
		}
		lastOutputVersion = content;
		appendFileSync(
			versionLog,
			`${JSON.stringify({ tool: toolName, timestamp: Date.now(), content })}\n`,
		);
	}

	const allowed = allowedCommands(config.commands);

	// bash with the reduced environment (no session metadata, nothing outside command_env)
	const bashTool = createBashTool(workspace, {
		exposeSessionEnvironment: false,
		spawnHook: ({ command, cwd, env }) => ({ command, cwd, env: commandEnv(env, config.command_env ?? []) }),
	});
	pi.registerTool({
		...bashTool,
		execute: async (id, params, signal, onUpdate) => bashTool.execute(id, params, signal, onUpdate),
	});

	const pending = new Map<string, { name: string; command: string; started: number }>();

	function record(entry: Record<string, unknown>) {
		if (commandLog) {
			appendFileSync(commandLog, `${JSON.stringify(entry)}\n`);
		}
	}

	function deny(kind: string, target: string, reason: string) {
		if (deniedLog) {
			appendFileSync(
				deniedLog,
				`${JSON.stringify({ kind, path: target, reason, timestamp: Date.now() })}\n`,
			);
		}
		return { block: true, reason };
	}

	const readRoot = canonical(workspace);

	function canonical(path: string): string {
		// resolve symlinks of the existing prefix so that a link cannot lead outside
		let probe = path;
		const rest: string[] = [];
		while (!existsSync(probe)) {
			const parent = resolve(probe, "..");
			if (parent === probe) {
				return path;
			}
			rest.unshift(probe.slice(parent.length + 1));
			probe = parent;
		}
		try {
			return resolve(realpathSync(probe), ...rest);
		} catch {
			return path;
		}
	}

	function readTarget(raw: string | undefined): string {
		return canonical(
			raw === undefined || raw === "" ? workspace : isAbsolute(raw) ? resolve(raw) : resolve(workspace, raw),
		);
	}

	function readable(raw: string | undefined): boolean {
		const rel = relative(readRoot, readTarget(raw));
		return rel === "" || (rel !== ".." && !rel.startsWith("../") && !isAbsolute(rel));
	}

	pi.on("tool_call", async (event) => {
		if (
			event.toolName === "read" ||
			event.toolName === "grep" ||
			event.toolName === "find" ||
			event.toolName === "ls"
		) {
			const raw = (event.input as { path?: string }).path;
			if (!readable(raw)) {
				return deny(event.toolName, String(raw), "reads are restricted to the workspace");
			}
			if (config.komar?.compact) {
				const glob = (event.input as { glob?: string }).glob;
				const reason = compactReadDenied(event.toolName, relative(readRoot, readTarget(raw)), glob);
				if (reason !== undefined) {
					return deny(event.toolName, String(raw), reason);
				}
			}
			return undefined;
		}
		if (event.toolName === "write" || event.toolName === "edit") {
			const raw = event.input.path as string;
			const target = isAbsolute(raw) ? resolve(raw) : resolve(workspace, raw);
			const inside = !relative(workspace, target).startsWith("..");
			if (!inside) {
				return deny(event.toolName, raw, "writes outside the workspace are not allowed");
			}
			const spec = config.komar !== undefined && KOMAR_SPEC.test(relative(workspace, target));
			if (target !== outputPath && !spec) {
				return deny(event.toolName, raw, `writes are restricted to ${config.output_test_file}`);
			}
			return undefined;
		}
		if (event.toolName === "bash") {
			const raw = String(event.input.command ?? "");
			const name =
				matchCommand(allowed, raw) ??
				(config.komar !== undefined ? komarCommandKind(raw, config.komar.scenario, config.komar.compact) : undefined);
			if (name === undefined) {
				const komar = config.komar === undefined ? ""
					: config.komar.compact ? ", the komar run command of the task, and sed -n 'A,Bp' or cut -c A-B of a view komar/NAME.view" : ", and the komar run and jq commands of the task";
				return deny(
					"bash",
					raw.slice(0, 2000),
					`only the named case commands are allowed, exactly as written: ${Object.keys(config.commands ?? {}).join(", ")}${komar}`,
				);
			}
			if (name === "komar") {
				// the wrapper bounds a Komar run itself (runner/komar.py RUN_TIMEOUT); a shorter bash timeout
				// would kill the shell while the scenario's JVMs keep running
				delete (event.input as { timeout?: number }).timeout;
			}
			pending.set(event.toolCallId, { name, command: raw, started: Date.now() });
			record({ event: "start", name, command: raw, timestamp: Date.now() });
			return undefined;
		}
		if (DEBUGGER_TOOLS.includes(event.toolName)) {
			record({ event: "debugger", tool: event.toolName, timestamp: Date.now() });
			return undefined;
		}
		return undefined;
	});

	pi.on("tool_result", async (event) => {
		if (event.toolName === "write" || event.toolName === "edit") {
			recordOutputVersion(event.toolName);
			return undefined;
		}
		if (event.toolName !== "bash") {
			return undefined;
		}
		const started = pending.get(event.toolCallId);
		pending.delete(event.toolCallId);
		const text = (event.content ?? [])
			.map((block) => (block as { text?: string }).text ?? "")
			.join("\n");
		const match = /Command exited with code (\d+)/.exec(text);
		const exitCode = match ? Number(match[1]) : event.isError ? null : 0;
		record({
			event: "end",
			name: started?.name ?? null,
			command: started?.command ?? null,
			isError: Boolean(event.isError),
			exit_code: exitCode,
			succeeded: !event.isError && exitCode === 0,
			duration_ms: started ? Date.now() - started.started : null,
			timestamp: Date.now(),
			output_tail: text.slice(-4000),
		});
		return undefined;
	});
}
