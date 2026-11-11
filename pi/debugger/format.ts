/**
 * Pure helpers of pi/debugger.ts (no Pi or Node imports, so that tests load them directly):
 * matching a breakpoint line by its content, the class of a source file, project frames, and the
 * text form of the values the JDI server sends (pi/debugger/JdiDapServer.java, Renderer).
 */

export const CODE_CAP = 200;
export const ANSWER_CAP = 8000;
export const PROJECT_FRAMES = 5;

export type LineMatch = { line: number; text: string } | { error: string };

function norm(s: string): string {
	return s.replace(/\s+/g, " ").trim();
}

/**
 * The line of `source` that holds `code` (one source line, compared with runs of whitespace collapsed).
 * Lines equal to the code are preferred; otherwise lines that contain it. Several matches without
 * `line` are an error listing them; a given `line` must hold the code.
 */
export function matchLine(source: string, file: string, code: string, line?: number): LineMatch {
	if (/[\r\n]/.test(code.trim())) {
		return { error: "code must be a single source line" };
	}
	const want = norm(code);
	if (!want) {
		return { error: "code is empty: pass the text of the source line" };
	}
	if (want.length > CODE_CAP) {
		return { error: `code is longer than ${CODE_CAP} characters: pass the text of one source line` };
	}
	const lines = source.split(/\r?\n/);
	const exact: number[] = [];
	const partial: number[] = [];
	lines.forEach((text, i) => {
		const n = norm(text);
		if (n === want) exact.push(i + 1);
		else if (n.includes(want)) partial.push(i + 1);
	});
	const found = exact.length ? exact : partial;
	if (line !== undefined && line !== null) {
		if (!Number.isInteger(line) || line < 1 || line > lines.length) {
			return { error: `${file} has ${lines.length} lines, no line ${line}` };
		}
		if (norm(lines[line - 1]).includes(want)) {
			return { line, text: lines[line - 1].trim() };
		}
		const where = found.length ? `; the code is at line${found.length > 1 ? "s" : ""} ${found.join(", ")}` : "";
		return { error: `line ${line} of ${file} is \`${lines[line - 1].trim().slice(0, CODE_CAP)}\`${where}` };
	}
	if (found.length === 0) {
		return { error: `no line of ${file} contains this code` };
	}
	if (found.length > 1) {
		return {
			error: `the code occurs at lines ${found.join(", ")} of ${file}; call set_breakpoint again with line`,
		};
	}
	return { line: found[0], text: lines[found[0] - 1].trim() };
}

/** Binary name of the top-level class of a Java source file: package + file name. */
export function classOfSource(source: string, file: string): string {
	const stripped = source.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
	const m = /^\s*package\s+([\w.]+)\s*;/m.exec(stripped);
	const base = file.replace(/\\/g, "/").split("/").pop()!.replace(/\.java$/, "");
	return m ? `${m[1]}.${base}` : base;
}

export interface MethodRef {
	className: string;
	method: string;
	argTypes: string[] | null;
}

/** `pkg.Class#name(type, ...)`; the argument list is optional; `Class#Class` or `#<init>` is a constructor. */
export function parseMethod(spec: string): MethodRef | { error: string } {
	const m = /^\s*([\w.$]+)\s*#\s*([\w$<>]+)\s*(?:\(([^)]*)\))?\s*$/.exec(spec);
	if (!m) {
		return { error: "method must look like pkg.Class#name or pkg.Class#name(int, java.lang.String)" };
	}
	const className = m[1];
	let method = m[2];
	const simple = className.split(/[.$]/).pop();
	if (method === simple) method = "<init>";
	const argTypes = m[3] === undefined ? null : m[3].trim() === "" ? [] : splitTypes(m[3]);
	return { className, method, argTypes };
}

/** Argument types split at the commas outside type arguments; type arguments dropped (JDI sees the erasure). */
export function splitTypes(list: string): string[] {
	const out: string[] = [];
	let depth = 0;
	let cur = "";
	for (const ch of list) {
		if (ch === "<") depth++;
		else if (ch === ">") depth = Math.max(0, depth - 1);
		if (ch === "," && depth === 0) {
			out.push(cur);
			cur = "";
		} else {
			cur += ch;
		}
	}
	out.push(cur);
	return out.map((t) => eraseType(t));
}

function eraseType(t: string): string {
	let out = "";
	let depth = 0;
	for (const ch of t) {
		if (ch === "<") depth++;
		else if (ch === ">") depth = Math.max(0, depth - 1);
		else if (depth === 0) out += ch;
	}
	return out.replace(/\s+/g, "").replace(/\.\.\.$/, "[]");
}

export interface Frame {
	index: number;
	class: string;
	method: string;
	line: number;
	source: string | null;
}

function simpleName(type: string): string {
	const s = type.split(".").pop() ?? type;
	return s;
}

export function frameText(f: Frame): string {
	const method = f.method === "<init>" ? "<init>" : f.method;
	return `${simpleName(f.class)}.${method}:${f.line}`;
}

/** The frames whose source file is in the project (the top frame always), at most `limit`. */
export function projectFrames(
	frames: Frame[],
	inProject: (source: string | null) => boolean,
	limit = PROJECT_FRAMES,
): { shown: Frame[]; more: number } {
	const project = frames.filter((f, i) => i === 0 || inProject(f.source));
	return { shown: project.slice(0, limit), more: Math.max(0, project.length - limit) };
}

// ---- values -------------------------------------------------------------------------------------

export interface Node {
	t: string;
	type?: string;
	ref?: number;
	v?: string;
	len?: number;
	size?: number;
	cut?: boolean;
	more?: number;
	items?: Array<Node | { k: Node; v: Node }>;
	fields?: Array<{ name: string; value: Node | null }>;
	/** a JDK holder (AtomicReference, Optional, ...): the value it holds */
	value?: Node | null;
}

function quote(s: string, q: string): string {
	return (
		q +
		s
			.replace(/\\/g, "\\\\")
			.replace(new RegExp(q, "g"), `\\${q}`)
			.replace(/\n/g, "\\n")
			.replace(/\r/g, "\\r")
			.replace(/\t/g, "\\t") +
		q
	);
}

/** Text of a value; objects deeper than `depth` (counted from this value) are shown as Type@N. */
export function valueText(n: Node | null | undefined, depth = 99, indent = ""): string {
	if (!n) return "?";
	const name = n.type ? simpleName(n.type) : "";
	const at = n.ref !== undefined ? `@${n.ref}` : "";
	switch (n.t) {
		case "null":
			return "null";
		case "void":
			return "void";
		case "collected":
			return "<collected>";
		case "prim":
			return n.type === "char" ? quote(n.v ?? "", "'") : String(n.v);
		case "box":
			return n.type === "java.lang.Character" ? quote(n.v ?? "", "'") : String(n.v);
		case "str": {
			const len = n.len ?? (n.v ?? "").length;
			const cut = (n.v ?? "").length < len;
			return quote(n.v ?? "", '"') + (cut ? ` …(${(n.v ?? "").length} of ${len} characters)` : "");
		}
		case "enum":
			return `${name}.${n.v}`;
		case "val":
			return `${name}(${n.v})`;
		case "wrap":
			return `${name}${at}(${valueText(n.value, depth, indent)})`;
		case "arr":
		case "coll":
		case "map": {
			const head = n.t === "arr" ? `${name.replace(/\[\]$/, "")}[${n.size}]${at}` : `${name}${at}(size ${n.size})`;
			if (n.cut || !n.items || depth <= 0) return n.size === 0 ? head : head;
			const inner = indent + "  ";
			const items = n.items.map((it) =>
				"k" in (it as object) && (it as { k: Node }).k !== undefined && n.t === "map"
					? `${valueText((it as { k: Node }).k, depth - 1, inner)} = ${valueText((it as { v: Node }).v, depth - 1, inner)}`
					: valueText(it as Node, depth - 1, inner),
			);
			const rest = (n.size ?? 0) > items.length ? `, …+${(n.size ?? 0) - items.length} more` : "";
			const flat = `${head} [${items.join(", ")}${rest}]`;
			if (flat.length <= 120 && !flat.includes("\n")) return flat;
			return `${head} [\n${items.map((s) => inner + s).join(",\n")}${rest ? `,\n${inner}${rest.slice(2)}` : ""}\n${indent}]`;
		}
		case "obj": {
			const head = `${name}${at}`;
			if (n.cut || !n.fields || depth <= 0) return head;
			const inner = indent + "  ";
			const fields = n.fields.map((f) => `${f.name}=${valueText(f.value, depth - 1, inner)}`);
			if (n.more) fields.push(`…+${n.more} more fields`);
			const flat = `${head} {${fields.join(", ")}}`;
			if (flat.length <= 120 && !flat.includes("\n")) return flat;
			return `${head} {\n${fields.map((s) => inner + s).join("\n")}\n${indent}}`;
		}
		default:
			return `${name}${at}`;
	}
}

/** Text of a value under ANSWER_CAP: the depth is lowered until it fits, then the text is cut. */
export function fitValue(n: Node | null | undefined, depth: number, cap = ANSWER_CAP): { text: string; depth: number } {
	for (let d = depth; d >= 0; d--) {
		const text = valueText(n, d);
		if (text.length <= cap) return { text, depth: d };
	}
	const text = valueText(n, 0);
	return { text: `${text.slice(0, cap)} …(cut at ${cap} characters)`, depth: 0 };
}

/** Text of locals; an entry with its own `depth` (the locals of a stop) is shown to that depth. */
export function localsText(locals: Array<{ name: string; value: Node | null; depth?: number }>, depth = 0): string {
	return locals.map((l) => `  ${l.name} = ${valueText(l.value, l.depth ?? depth, "  ")}`).join("\n");
}

/** Locals of a stop under `cap` characters: expanded entries fall back to Type@N, the last first. */
export function stopLocalsText(locals: Array<{ name: string; value: Node | null; depth?: number }>, cap: number): string {
	const shown = locals.map((l) => ({ ...l }));
	let t = localsText(shown, 0);
	for (let i = shown.length - 1; t.length > cap && i >= 0; i--) {
		if (shown[i].depth) {
			shown[i].depth = 0;
			t = localsText(shown, 0);
		}
	}
	return t;
}

/** Patterns that have no place in a debugger expression (no file system, processes, class loaders). */
export const OUTSIDE_EXPRESSION =
	/\bjava\s*\.\s*(?:io|nio)\b|\bProcess(?:Builder|Handle)?\b|\bRuntime\b|\b(?:get)?[Cc]lass[Ll]oader\b|\bucp\b|\b[Cc]lass[Pp]ath\b|\bjava\s*\.\s*class\s*\.\s*path\b|\buser\s*\.\s*(?:home|dir)\b|\bThread\b|\bcurrentThread\b|\bprotectionDomain\b|\bcodesource\b|\bCodeSource\b|\bClass\s*\.\s*forName\b|\bSystem\s*\.\s*(?:getenv|getProperty|exit)\b|\bFiles\b|\bPaths\b|\bsetAccessible\b|\bgetDeclared\w*\b/;
