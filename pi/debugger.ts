import { spawn, type ChildProcess } from "node:child_process";
import { appendFileSync, type Dirent, existsSync, openSync, readdirSync, readFileSync, realpathSync } from "node:fs";
import { connect, type Socket } from "node:net";
import { isAbsolute, relative, resolve } from "node:path";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
	ANSWER_CAP,
	classOfSource,
	fitValue,
	type Frame,
	frameText,
	localsText,
	stopLocalsText,
	matchLine,
	type Node,
	parseMethod,
	projectFrames,
	valueText,
} from "./debugger/format.ts";

/**
 * Pi extension of the autonomous mode: five debugger tools over the driving scenario of the case.
 *
 *   debug_start()        start the case's scenario command with the JDWP agent (suspended) and attach;
 *                        called again: the previous run is killed and the scenario runs again (run #2,
 *                        ...), breakpoints are kept
 *   set_breakpoint(...)  on a source line given by its text (`code`, optional `line`), or on the entry
 *                        or exit of a method (`method`, `on`); optional `condition`
 *   remove_breakpoint(id)
 *   resume(timeout_s)    run to the next stop, the end of the run, or the timeout
 *   inspect(expr, frame, depth)
 *
 * The JDI server (pi/debugger/JdiDapServer.java, compiled by runner/debugger.py) does the debugging;
 * this extension starts it, speaks the Debug Adapter Protocol framing to it, and turns its answers
 * into short text. Every call and stop is written to the journal (DEBUGGER_LOG, JSON lines). The
 * scenario JVM is killed before any bash command of the agent and when Pi exits; its process group
 * id is written to DEBUGGER_PIDS so that the runner can kill what is left after a timeout.
 *
 * Environment (set by the runner): DEBUGGER_CONFIG (JSON, see DebuggerConfig), DEBUGGER_LOG,
 * DEBUGGER_PIDS.
 */
interface DebuggerConfig {
	workspace: string;
	java: string;
	server_classes: string;
	scenario: string[];
	port: number;
	command_env: string[];
	start_timeout: number;
	/** How many times debug_start may start the scenario in the cell. */
	debug_runs: number;
	scenario_log_dir: string;
	target: { class: string; method: string };
}

/** Default waits of resume, seconds: the first resume of a run covers the scenario reaching the target
 * (Spring contexts and MyBatis need 100-150 s for that), later ones the gaps between calls. */
export const FIRST_RESUME_TIMEOUT = 300;
export const RESUME_TIMEOUT = 60;

/**
 * Source roots of the workspace: for the first .java file met in a directory, its directory minus its
 * package path. Covers Maven layouts (src/main/java) and others (JGit: src/, tst/, exttst/).
 */
export function findSourceRoots(workspace: string): string[] {
	const roots = new Set<string>();
	const walk = (dir: string, depth: number) => {
		if (depth > 12) return;
		let entries: Dirent[];
		try {
			entries = readdirSync(dir, { withFileTypes: true });
		} catch {
			return;
		}
		if ([...roots].some((r) => dir === r || dir.startsWith(r + "/"))) return;
		const java = entries.find((e) => e.isFile() && e.name.endsWith(".java"));
		if (java) {
			let pkg = "";
			try {
				const m = /^\s*package\s+([\w.]+)\s*;/m.exec(readFileSync(resolve(dir, java.name), "utf8").slice(0, 20000));
				pkg = m ? m[1] : "";
			} catch {
				/* unreadable: treat as the default package */
			}
			const parts = pkg ? pkg.split(".") : [];
			const tail = dir.split("/").slice(-parts.length);
			if (parts.length === 0) {
				roots.add(dir);
				return;
			}
			if (tail.join(".") === pkg) {
				roots.add(dir.split("/").slice(0, -parts.length).join("/"));
				return;
			}
		}
		for (const e of entries) {
			if (!e.isDirectory() || e.name.startsWith(".") || e.name === "target" || e.name === "node_modules" || e.name === "build") continue;
			walk(resolve(dir, e.name), depth + 1);
		}
	};
	walk(workspace, 0);
	return roots.size ? [...roots] : [workspace];
}

export const TOOLS = ["debug_start", "set_breakpoint", "remove_breakpoint", "resume", "inspect"] as const;

export function debugArgv(scenario: string[], port: number): string[] {
	const agent = `-Dmaven.surefire.debug=-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:${port}`;
	return [scenario[0], agent, ...scenario.slice(1)];
}

type Msg = { type: string; seq?: number; request_seq?: number; command?: string; event?: string; success?: boolean; message?: string; body?: any };

class DapClient {
	private seq = 1;
	private buffer = Buffer.alloc(0);
	private pending = new Map<number, (m: Msg) => void>();
	private listeners: Array<(m: Msg) => void> = [];
	closed = false;

	constructor(private socket: Socket) {
		socket.on("data", (chunk: Buffer) => this.onData(chunk));
		socket.on("close", () => {
			this.closed = true;
			for (const [, done] of this.pending) done({ type: "response", success: false, message: "debug server closed" });
			this.pending.clear();
			this.emit({ type: "event", event: "closed" });
		});
		socket.on("error", () => undefined);
	}

	onEvent(listener: (m: Msg) => void) {
		this.listeners.push(listener);
	}

	private emit(m: Msg) {
		for (const l of this.listeners) l(m);
	}

	private onData(chunk: Buffer) {
		this.buffer = Buffer.concat([this.buffer, chunk]);
		for (;;) {
			const end = this.buffer.indexOf("\r\n\r\n");
			if (end < 0) return;
			const header = this.buffer.subarray(0, end).toString("utf8");
			const m = /Content-Length:\s*(\d+)/i.exec(header);
			if (!m) {
				this.buffer = this.buffer.subarray(end + 4);
				continue;
			}
			const length = Number(m[1]);
			if (this.buffer.length < end + 4 + length) return;
			const body = this.buffer.subarray(end + 4, end + 4 + length).toString("utf8");
			this.buffer = this.buffer.subarray(end + 4 + length);
			let msg: Msg;
			try {
				msg = JSON.parse(body);
			} catch {
				continue;
			}
			if (msg.type === "response" && msg.request_seq !== undefined) {
				const done = this.pending.get(msg.request_seq);
				this.pending.delete(msg.request_seq);
				done?.(msg);
			} else if (msg.type === "event") {
				this.emit(msg);
			}
		}
	}

	request(command: string, args: Record<string, unknown> = {}): Promise<Msg> {
		if (this.closed) return Promise.resolve({ type: "response", success: false, message: "debug server closed" });
		const seq = this.seq++;
		const json = JSON.stringify({ seq, type: "request", command, arguments: args });
		return new Promise((done) => {
			this.pending.set(seq, done);
			this.socket.write(`Content-Length: ${Buffer.byteLength(json)}\r\n\r\n${json}`);
		});
	}
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
	return new Promise((done) => {
		const timer = setTimeout(done, ms);
		signal?.addEventListener("abort", () => {
			clearTimeout(timer);
			done();
		});
	});
}

function text(s: string) {
	const out = s.length > ANSWER_CAP + 400 ? `${s.slice(0, ANSWER_CAP)}\n…(answer cut at ${ANSWER_CAP} characters)` : s;
	return { content: [{ type: "text" as const, text: out }], details: {} };
}

export default function (pi: ExtensionAPI) {
	const configPath = process.env.DEBUGGER_CONFIG;
	if (!configPath) {
		return;
	}
	const config = JSON.parse(readFileSync(configPath, "utf-8")) as DebuggerConfig;
	const maxRuns = Math.max(1, Number(config.debug_runs ?? 3));
	const workspace = resolve(config.workspace);
	const journal = process.env.DEBUGGER_LOG;
	const pidsFile = process.env.DEBUGGER_PIDS;
	const scenarioEnv: NodeJS.ProcessEnv = {};
	for (const name of config.command_env ?? []) {
		if (process.env[name] !== undefined) scenarioEnv[name] = process.env[name];
	}

	let server: ChildProcess | undefined;
	let client: DapClient | undefined;
	let scenario: ChildProcess | undefined;
	let scenarioExit: number | null | undefined;
	let run = 0;
	let state: "none" | "suspended" | "running" | "over" = "none";
	let nextId = 1;
	const hits = new Map<number, number>();
	let stopsInRun = 0;
	/** The first resume of a run also waits for the build and the startup of the scenario. */
	let resumedInRun = false;
	const queue: Msg[] = [];
	let wake: (() => void) | undefined;
	const bps = new Map<number, string>(); // id -> description

	function log(entry: Record<string, unknown>) {
		if (journal) appendFileSync(journal, `${JSON.stringify({ timestamp: Date.now(), run, ...entry })}\n`);
	}

	function inWorkspace(path: string): boolean {
		const rel = relative(realpathSync(workspace), path);
		return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
	}

	function sourceFile(raw: string): { path: string; rel: string } | { error: string } {
		const cleaned = raw.replace(/^@/, "");
		const path = isAbsolute(cleaned) ? resolve(cleaned) : resolve(workspace, cleaned);
		// a path outside the workspace gets the same answer whether it exists or not
		const outside = { error: "breakpoints are restricted to files in the workspace" };
		const lexical = relative(resolve(workspace), path);
		if (lexical.startsWith("..") || isAbsolute(lexical)) return outside;
		if (!existsSync(path)) return { error: `no file ${raw} in the workspace` };
		const real = realpathSync(path);
		if (!inWorkspace(real)) return outside;
		return { path: real, rel: relative(realpathSync(workspace), real) };
	}

	const sourceCache = new Map<string, boolean>();
	function inProject(source: string | null): boolean {
		// a frame is a project frame when its source file (package path) exists under the workspace
		if (!source) return false;
		if (!sourceCache.has(source)) sourceCache.set(source, projectHas(source));
		return sourceCache.get(source)!;
	}

	const sourceRoots: string[] = [];
	function projectHas(source: string): boolean {
		if (sourceRoots.length === 0) sourceRoots.push(...findSourceRoots(workspace));
		return sourceRoots.some((r) => existsSync(resolve(r, source)));
	}

	function onEvent(m: Msg) {
		if (m.event === "stopped" || m.event === "terminated" || m.event === "closed") {
			queue.push(m);
			wake?.();
		}
	}

	async function ensureServer(): Promise<DapClient> {
		if (client && !client.closed) return client;
		server = spawn(config.java, ["-cp", config.server_classes, "JdiDapServer", "--port", "0"], {
			cwd: workspace,
			env: scenarioEnv,
			stdio: ["ignore", "pipe", "pipe"],
		});
		const port = await new Promise<number>((done, fail) => {
			let out = "";
			server!.stdout!.on("data", (c: Buffer) => {
				out += c.toString();
				const m = /LISTENING (\d+)/.exec(out);
				if (m) done(Number(m[1]));
			});
			server!.on("exit", (code) => fail(new Error(`debug server exited with code ${code}`)));
			setTimeout(() => fail(new Error("debug server did not start")), 30000);
		});
		server.stderr!.on("data", () => undefined);
		const socket = await new Promise<Socket>((done, fail) => {
			const s = connect(port, "127.0.0.1", () => done(s));
			s.on("error", fail);
		});
		client = new DapClient(socket);
		client.onEvent(onEvent);
		await client.request("initialize");
		return client;
	}

	function killScenario(): Promise<void> {
		const proc = scenario;
		scenario = undefined;
		if (!proc || proc.exitCode !== null || proc.pid === undefined) return Promise.resolve();
		return new Promise((done) => {
			const finish = () => done();
			proc.once("exit", finish);
			try {
				process.kill(-proc.pid!, "SIGTERM");
			} catch {
				done();
				return;
			}
			setTimeout(() => {
				try {
					process.kill(-proc.pid!, "SIGKILL");
				} catch {
					/* gone */
				}
				setTimeout(finish, 500);
			}, 3000);
		});
	}

	async function endRun(reason: string) {
		if (state === "none") return;
		const c = client;
		if (c && !c.closed) await c.request("terminate");
		await killScenario();
		if (state !== "over") log({ event: "run_end", reason, stops: stopsInRun });
		state = "over";
	}

	function nextEvent(timeoutMs: number, signal?: AbortSignal): Promise<Msg | undefined> {
		if (queue.length) return Promise.resolve(queue.shift());
		return new Promise((done) => {
			const timer = setTimeout(() => {
				wake = undefined;
				done(undefined);
			}, timeoutMs);
			signal?.addEventListener("abort", () => {
				clearTimeout(timer);
				wake = undefined;
				done(undefined);
			});
			wake = () => {
				clearTimeout(timer);
				wake = undefined;
				done(queue.shift());
			};
		});
	}

	function breakpointLine(st: any): string {
		const id = st.id;
		const where = (st.where ?? []).length ? ` at ${(st.where as string[]).join(", ")}` : "";
		const status = st.status === "set" ? "set" : st.classLoaded === false || st.classLoaded === undefined ? "pending (class not loaded yet; set when it loads)" : "not set";
		const notes = (st.notes ?? []).length ? ` — ${(st.notes as string[]).join("; ")}` : "";
		return `#${id} ${bps.get(id) ?? ""} ${status}${where}${notes}`.replace(/\s+/g, " ").trim();
	}

	/** Attach to the scenario JVM; also used when a JVM ends while the scenario command goes on. */
	async function attach(c: DapClient, deadline: number, signal?: AbortSignal): Promise<Msg | undefined> {
		while (Date.now() < deadline && !signal?.aborted) {
			if (scenario && scenario.exitCode !== null) return undefined;
			if (!scenario) return undefined;
			const r = await c.request("attach", { host: "127.0.0.1", port: config.port });
			if (r.success) return r;
			await sleep(500, signal);
		}
		return undefined;
	}

	function scenarioTail(): string {
		try {
			const s = readFileSync(resolve(config.scenario_log_dir, `scenario-debug-run${run}.log`), "utf-8");
			return s.split(/\r?\n/).filter((l) => l.trim()).slice(-15).join("\n");
		} catch {
			return "";
		}
	}

	async function waitScenarioExit(ms: number): Promise<number | null | undefined> {
		const proc = scenario;
		if (!proc) return scenarioExit;
		if (proc.exitCode !== null) return proc.exitCode;
		await Promise.race([new Promise((d) => proc.once("exit", d)), sleep(ms)]);
		return proc.exitCode;
	}

	function inTarget(frame: Frame | undefined): boolean {
		return !!frame && frame.class === config.target.class && frame.method === config.target.method;
	}

	function stopAnswer(b: any): { text: string; event: Record<string, unknown> } {
		const frames: Frame[] = b.frames ?? [];
		const ids: number[] = b.breakpoints ?? [];
		stopsInRun++;
		const hitText = ids.map((id) => {
			const n = (hits.get(id) ?? 0) + 1;
			hits.set(id, n);
			return `#${id} (hit ${n})`;
		}).join(", ");
		const top = frames[0];
		const lines: string[] = [];
		let place = top ? frameText(top) : "?";
		let targetFrame = top;
		if (b.reason === "breakpoint") {
			lines.push(`stopped at breakpoint ${hitText} — ${place} [thread ${b.thread}]`);
		} else if (b.reason === "entry") {
			lines.push(`stopped at entry ${hitText} of ${b.method ?? place} [thread ${b.thread}]`);
		} else if (b.reason === "exit") {
			lines.push(`stopped at exit ${hitText} of ${b.method} — ${place} [thread ${b.thread}]`);
		} else if (b.reason === "exception") {
			targetFrame = frames[b.targetFrame] ?? top;
			place = targetFrame ? frameText(targetFrame) : place;
			const msg = b.message !== undefined ? `: ${JSON.stringify(String(b.message).slice(0, 300))}` : "";
			lines.push(`stopped: exception ${b.exceptionType}${msg} thrown out of ${b.method} ${hitText} — thrown at ${b.thrownAt} [thread ${b.thread}]`);
			if (b.targetFrame > 0) lines.push(`(${b.method} is frame ${b.targetFrame}: inspect with frame=${b.targetFrame})`);
		}
		const { shown, more } = projectFrames(frames, inProject);
		lines.push(`frames: ${shown.map(frameText).join(" ← ")}${more ? ` … (+${more} project frames)` : ""}`);
		for (const e of b.conditionErrors ?? []) lines.push(`condition: ${e}`);
		if (b.reason === "exit" && b.returned !== undefined) lines.push(`returned = ${valueText(b.returned as Node, 0)}`);
		if (b.exception) lines.push(`exception = ${valueText(b.exception as Node, 0)}`);
		const locals = (b.locals ?? []).filter((l: any) => l.name !== "returned" && l.name !== "exception");
		const head = lines.join("\n").length;
		lines.push(locals.length ? `locals:\n${stopLocalsText(locals, Math.max(1000, ANSWER_CAP - head - 20))}` : "locals: none");
		const event = {
			kind: "stop", reason: b.reason, breakpoints: ids, place, in_target: inTarget(targetFrame),
			frames: frames.length,
		};
		return { text: lines.join("\n"), event };
	}

	function register(
		name: (typeof TOOLS)[number],
		description: string,
		parameters: any,
		run_: (params: any, signal?: AbortSignal) => Promise<{ text: string; ok: boolean; extra?: Record<string, unknown> }>,
	) {
		pi.registerTool({
			name,
			label: name,
			description,
			promptSnippet: description.split(".")[0],
			parameters,
			executionMode: "sequential",
			async execute(_id: string, params: any, signal?: AbortSignal) {
				const started = Date.now();
				let out: { text: string; ok: boolean; extra?: Record<string, unknown> };
				try {
					out = await run_(params ?? {}, signal);
				} catch (error) {
					out = { text: `error: ${(error as Error).message}`, ok: false };
				}
				const answer = text(out.text);
				log({
					tool: name, args: params, ok: out.ok, answer_chars: answer.content[0].text.length,
					seconds: (Date.now() - started) / 1000, ...(out.extra ?? {}),
				});
				return answer;
			},
		} as any);
	}

	register(
		"debug_start",
		`Start the driving scenario of the task under the debugger and attach to its JVM, which waits suspended before running. Set breakpoints, then call resume. Calling debug_start again stops the current run and starts the scenario again; breakpoints are kept. At most ${maxRuns} runs. Any bash command ends the debug JVM, so the next debug_start uses up a run.`,
		Type.Object({}),
		async (_p, signal) => {
			if (run >= maxRuns) {
				return {
					ok: false,
					text: `limit of ${maxRuns} scenario runs reached; write the test from the data you have`,
					extra: { event: { kind: "start_refused", runs: run, limit: maxRuns } },
				};
			}
			const c = await ensureServer();
			if (state !== "none") await endRun("restart");
			queue.length = 0;
			run++;
			hits.clear();
			stopsInRun = 0;
			resumedInRun = false;
			const argv = debugArgv(config.scenario, config.port);
			const logFile = resolve(config.scenario_log_dir, `scenario-debug-run${run}.log`);
			const fd = openSync(logFile, "a");
			scenarioExit = undefined;
			scenario = spawn(argv[0].startsWith("./") ? resolve(workspace, argv[0]) : argv[0], argv.slice(1), {
				cwd: workspace,
				env: scenarioEnv,
				detached: true,
				stdio: ["ignore", fd, fd],
			});
			const proc = scenario;
			proc.on("exit", (code) => {
				scenarioExit = code;
			});
			if (pidsFile && proc.pid) appendFileSync(pidsFile, `${proc.pid}\n`);
			const r = await attach(c, Date.now() + config.start_timeout * 1000, signal);
			if (!r) {
				const code = await waitScenarioExit(1000);
				await killScenario();
				state = "over";
				log({ event: "run_end", reason: "no_jvm", exit_code: code ?? null });
				const tail = scenarioTail();
				return {
					ok: false,
					text: `run #${run}: the scenario ${code === undefined || code === null ? `started no test JVM within ${config.start_timeout} s` : `command exited with code ${code} before a test JVM started`}${tail ? `\nlast output:\n${tail}` : ""}`,
					extra: { event: { kind: "start_failed" } },
				};
			}
			state = "suspended";
			const sts = (r.body?.breakpoints ?? []) as any[];
			const lines = [`run #${run}: scenario JVM started and suspended before its first instruction; call resume to run it`];
			lines.push(sts.length ? `breakpoints:\n${sts.map((s) => `  ${breakpointLine(s)}`).join("\n")}` : "breakpoints: none");
			return { ok: true, text: lines.join("\n"), extra: { event: { kind: "start" } } };
		},
	);

	register(
		"set_breakpoint",
		"Set a breakpoint. On a source line: file and code (the text of the line; add line when the text occurs on several lines). On a method: method (pkg.Class#name, with (argument types) for overloads) and on (entry or exit; an exit stop shows the returned value or the exception thrown out of the method). Optional condition: an expression like those of inspect; the breakpoint stops only when it is true. Breakpoints are kept across runs.",
		Type.Object({
			file: Type.Optional(Type.String({ description: "source file, relative to the workspace" })),
			code: Type.Optional(Type.String({ description: "text of the source line (at most 200 characters)" })),
			line: Type.Optional(Type.Integer({ description: "line number, when the code occurs on several lines" })),
			method: Type.Optional(Type.String({ description: "pkg.Class#name or pkg.Class#name(int, java.lang.String)" })),
			on: Type.Optional(Type.String({ description: "entry or exit (with method)" })),
			condition: Type.Optional(Type.String({ description: "stop only when this expression is true" })),
		}),
		async (p) => {
			const c = await ensureServer();
			let args: Record<string, unknown>;
			let label: string;
			if (p.method !== undefined) {
				if (p.file !== undefined || p.code !== undefined || p.line !== undefined) {
					return { ok: false, text: "error: give either method (with on) or file and code" };
				}
				const on = String(p.on ?? "");
				if (on !== "entry" && on !== "exit") return { ok: false, text: "error: on must be entry or exit" };
				const m = parseMethod(String(p.method));
				if ("error" in m) return { ok: false, text: `error: ${m.error}` };
				if (!m.className.includes(".")) return { ok: false, text: "error: give the fully qualified class name, e.g. org.example.Foo#bar" };
				args = { kind: on, className: m.className, method: m.method, argTypes: m.argTypes };
				label = `${on} of ${String(p.method).trim()}`;
			} else {
				if (p.file === undefined || p.code === undefined) {
					return { ok: false, text: "error: a line breakpoint needs file and code (or use method and on)" };
				}
				if (p.on !== undefined) return { ok: false, text: "error: on is for method breakpoints" };
				const f = sourceFile(String(p.file));
				if ("error" in f) return { ok: false, text: `error: ${f.error}` };
				const source = readFileSync(f.path, "utf-8");
				const found = matchLine(source, f.rel, String(p.code), p.line);
				if ("error" in found) return { ok: false, text: `error: ${found.error}` };
				args = { kind: "line", className: classOfSource(source, f.rel), line: found.line };
				label = `${f.rel}:${found.line} \`${found.text.slice(0, 120)}\``;
			}
			const id = nextId++;
			bps.set(id, label);
			const r = await c.request("setBreakpoint", { id, condition: p.condition ?? null, ...args });
			if (!r.success) {
				bps.delete(id);
				return { ok: false, text: `error: ${r.message}` };
			}
			return { ok: true, text: breakpointLine(r.body), extra: { breakpoint: { id, ...args, condition: p.condition ?? null } } };
		},
	);

	register(
		"remove_breakpoint",
		"Remove a breakpoint by its id.",
		Type.Object({ id: Type.Integer() }),
		async (p) => {
			const c = await ensureServer();
			const r = await c.request("removeBreakpoint", { id: p.id });
			const removed = !!r.body?.removed;
			if (removed) bps.delete(p.id);
			return { ok: removed, text: removed ? `#${p.id} removed` : `error: no breakpoint #${p.id}` };
		},
	);

	register(
		"resume",
		"Let the scenario JVM run until the next stop at a breakpoint, the end of the run, or timeout_s seconds. A stop shows the place, the project frames and the local variables of the top frame.",
		Type.Object({ timeout_s: Type.Optional(Type.Integer({ minimum: 1, maximum: 600, description: `seconds to wait; default ${FIRST_RESUME_TIMEOUT} for the first resume of a run, ${RESUME_TIMEOUT} later` })) }),
		async (p, signal) => {
			const c = await ensureServer();
			if (state === "none") return { ok: false, text: "error: no scenario run; call debug_start" };
			if (state === "over") return { ok: false, text: `error: run #${run} is over; call debug_start to run the scenario again` };
			const fallback = resumedInRun ? RESUME_TIMEOUT : FIRST_RESUME_TIMEOUT;
			resumedInRun = true;
			const timeout = Math.max(1, Math.min(600, Number(p.timeout_s ?? fallback))) * 1000;
			const deadline = Date.now() + timeout;
			if (state === "suspended") {
				const r = await c.request("resume");
				if (!r.success) return { ok: false, text: `error: ${r.message}` };
				state = "running";
			}
			for (;;) {
				const m = await nextEvent(Math.max(0, deadline - Date.now()), signal);
				if (!m) {
					return {
						ok: true,
						text: `still running after ${Math.round(timeout / 1000)} s (call resume to keep waiting, or debug_start to run again)`,
						extra: { event: { kind: "timeout" } },
					};
				}
				if (m.event === "stopped") {
					state = "suspended";
					const a = stopAnswer(m.body);
					return { ok: true, text: a.text, extra: { event: a.event } };
				}
				// the JVM ended: the scenario command may start another test JVM (another module)
				if (scenario && scenario.exitCode === null && m.event === "terminated") {
					const again = await attach(c, Math.min(deadline, Date.now() + 20000), signal);
					if (again) continue;
				}
				const code = await waitScenarioExit(30000);
				const stops = stopsInRun;
				await endRun("terminated");
				const tail = code ? scenarioTail() : "";
				return {
					ok: true,
					text: `terminated: run #${run} ended, ${stops} stop${stops === 1 ? "" : "s"} in this run; scenario command ${code === null || code === undefined ? "killed" : `exit code ${code}`}${tail ? `\nlast output:\n${tail}` : ""}`,
					extra: { event: { kind: "terminated", exit_code: code ?? null, stops } },
				};
			}
		},
	);

	register(
		"inspect",
		"Evaluate an expression at the current stop and show its value, objects expanded depth levels (0-3). Expressions: local variables, this, returned and exception at an exit stop, fields (a.b.c), array elements, @N for an object shown before, literals, comparisons, arithmetic, and the calls size(), isEmpty() on JDK collections and maps, get(i) on JDK lists, get(k), containsKey(k) on JDK maps and contains(k) on JDK sets with a string, number or enum key, length(), charAt(i) on strings, toString(), equals(x) on strings and boxed values, getClass().getName(). No other method calls: the debugger runs no code of the program, read fields instead. inspect(\"locals\", frame) lists the local variables of a frame.",
		Type.Object({
			expr: Type.String(),
			frame: Type.Optional(Type.Integer({ minimum: 0, description: "stack frame index, 0 = top" })),
			depth: Type.Optional(Type.Integer({ minimum: 0, maximum: 3, description: "levels of objects to expand, default 1" })),
		}),
		async (p) => {
			const c = await ensureServer();
			if (state !== "suspended") return { ok: false, text: "error: not stopped at a breakpoint" };
			const depth = Math.max(0, Math.min(3, Number(p.depth ?? 1)));
			const r = await c.request("evaluate", { expression: String(p.expr), frame: Number(p.frame ?? 0), depth });
			if (!r.success) return { ok: false, text: `error: ${r.message}` };
			if (r.body?.note) return { ok: true, text: String(r.body.note) };
			if (r.body?.locals) {
				const t = localsText(r.body.locals, depth);
				return { ok: true, text: t.length ? t : "no local variables" };
			}
			const fit = fitValue(r.body?.value as Node, depth);
			const note = fit.depth < depth ? `\n(shown to depth ${fit.depth} to stay under ${ANSWER_CAP} characters; inspect @N for parts)` : "";
			return { ok: true, text: fit.text + note };
		},
	);

	// the scenario JVM must not run next to an agent's build or test command
	pi.on("tool_call", async (event) => {
		if (event.toolName === "bash" && state !== "none" && state !== "over") {
			await endRun("bash");
		}
		return undefined;
	});

	const shutdown = async () => {
		await endRun("shutdown");
		try {
			server?.kill("SIGKILL");
		} catch {
			/* gone */
		}
	};
	pi.on("session_shutdown", shutdown);
	process.on("exit", () => {
		if (scenario?.pid) {
			try {
				process.kill(-scenario.pid, "SIGKILL");
			} catch {
				/* gone */
			}
		}
		try {
			server?.kill("SIGKILL");
		} catch {
			/* gone */
		}
	});
}
