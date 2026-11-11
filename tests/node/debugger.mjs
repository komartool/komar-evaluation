// Checks of pi/debugger.ts, run by tests/test_debugger.py: node debugger.mjs <pi package dir> <repository root> [<debugger config>]
// Without a config: the pure helpers (line matching, frames, values, expression filter).
// With a config: the five tools on the sample project (tests/debugger/sample) through a stand-in of
// the Pi extension API. Prints one JSON object.
import { pathToFileURL } from "node:url";

const [piDir, root, configPath] = process.argv.slice(2);
const { createJiti } = await import(pathToFileURL(`${piDir}/node_modules/jiti/lib/jiti.mjs`).href);
const jiti = createJiti(import.meta.url, {
	alias: {
		"@earendil-works/pi-coding-agent": `${piDir}/dist/index.js`,
		typebox: `${piDir}/node_modules/typebox/build/index.mjs`,
	},
});
const f = await jiti.import(`${root}/pi/debugger/format.ts`);
const out = {};

if (!configPath) {
	const { mkdtempSync, mkdirSync, writeFileSync } = await import("node:fs");
	const { tmpdir } = await import("node:os");
	const ws = mkdtempSync(`${tmpdir()}/roots-`);
	const put = (p, pkg) => { mkdirSync(`${ws}/${p}`, { recursive: true }); writeFileSync(`${ws}/${p}/A.java`, `package ${pkg};\nclass A {}\n`); };
	put("core/src/main/java/a/b", "a.b");
	put("core/src/test/java/a/b/c", "a.b.c");
	put("jgit.test/tst/org/x", "org.x");
	put("jgit.test/tst/org/x/y", "org.x.y");
	const d = await jiti.import(`${root}/pi/debugger.ts`);
	out.roots = d.findSourceRoots(ws).map((r) => r.slice(ws.length + 1)).sort();
	const src = ["package a.b;", "", "class X {", "  int f() {", "    return 1;", "  }", "  int g() {", "    return 1;", "  }", "  // return 2;", "}"].join("\n");
	out.unique = f.matchLine(src, "X.java", "int g() {");
	out.trimmed = f.matchLine(src, "X.java", "   int   f()  {  ");
	out.duplicate = f.matchLine(src, "X.java", "return 1;");
	out.duplicateWithLine = f.matchLine(src, "X.java", "return 1;", 8);
	out.wrongLine = f.matchLine(src, "X.java", "return 1;", 4);
	out.missing = f.matchLine(src, "X.java", "return 3;");
	out.partial = f.matchLine(src, "X.java", "return 2");
	out.tooLong = f.matchLine(src, "X.java", "x".repeat(201));
	out.multiLine = f.matchLine(src, "X.java", "a\nb");
	out.className = f.classOfSource(src, "src/main/java/a/b/X.java");
	out.method = f.parseMethod("org.x.Foo#bar(int, java.lang.String)");
	out.methodAny = f.parseMethod("org.x.Foo#bar");
	out.ctor = f.parseMethod("org.x.Foo#Foo()");
	out.badMethod = f.parseMethod("Foo.bar");
	out.methodGeneric = f.parseMethod("org.x.Foo#bar(java.util.Map<String, java.util.List<Integer>>, int, String...)");
	const frames = [
		{ index: 0, class: "a.T", method: "m", line: 3, source: "a/T.java" },
		...Array.from({ length: 30 }, (_, i) => ({ index: i + 1, class: "java.lang.reflect.M", method: "invoke", line: i, source: "java/lang/reflect/M.java" })),
		...Array.from({ length: 7 }, (_, i) => ({ index: 31 + i, class: "a.U", method: "u" + i, line: i, source: "a/U.java" })),
	];
	const pf = f.projectFrames(frames, (s) => !!s && s.startsWith("a/"));
	out.frames = { shown: pf.shown.map(f.frameText), more: pf.more };
	out.values = {
		prim: f.valueText({ t: "prim", type: "int", v: "5" }),
		char: f.valueText({ t: "prim", type: "char", v: "(" }),
		str: f.valueText({ t: "str", type: "java.lang.String", ref: 7, v: "a\"b\n", len: 4 }),
		cutStr: f.valueText({ t: "str", type: "java.lang.String", ref: 7, v: "abc", len: 5000 }),
		list: f.valueText({ t: "coll", type: "java.util.ArrayList", ref: 9, size: 25, items: [{ t: "str", type: "java.lang.String", ref: 1, v: "x", len: 1 }] }, 1),
		listShallow: f.valueText({ t: "coll", type: "java.util.ArrayList", ref: 9, size: 25, cut: true }, 0),
		obj: f.valueText({ t: "obj", type: "a.T", ref: 3, fields: [{ name: "n", value: { t: "prim", type: "int", v: "1" } }, { name: "o", value: { t: "obj", type: "a.U", ref: 4, cut: true } }], more: 2 }, 1),
		null: f.valueText({ t: "null" }),
	};
	const big = { t: "obj", type: "a.T", ref: 3, fields: Array.from({ length: 40 }, (_, i) => ({ name: "f" + i, value: { t: "str", type: "java.lang.String", ref: 10 + i, v: "y".repeat(400), len: 400 } })) };
	out.fit = (() => { const r = f.fitValue(big, 1); return { depth: r.depth, length: r.text.length }; })();
	const self = { t: "obj", type: "a.T", ref: 3, fields: [{ name: "n", value: { t: "prim", type: "int", v: "1" } }] };
	const arg = { t: "obj", type: "a.U", ref: 4, fields: [{ name: "s", value: { t: "str", type: "java.lang.String", ref: 5, v: "z".repeat(300), len: 300 } }] };
	const locals = [{ name: "this", value: self, depth: 1 }, { name: "u", value: arg, depth: 1 }, { name: "i", value: { t: "prim", type: "int", v: "2" }, depth: 0 }];
	out.stopLocals = f.stopLocalsText(locals, 8000);
	out.stopLocalsCapped = f.stopLocalsText(locals, 200);
	out.outside = ["java.io.File.separator", "java.nio.file.Files", "Runtime.getRuntime()", "this.size", "x.getClass().getName()", "cl.getClass().getClassLoader()"].map((e) => f.OUTSIDE_EXPRESSION.test(e));
	process.stdout.write(JSON.stringify(out));
	process.exit(0);
}

process.env.DEBUGGER_CONFIG = configPath;
const tools = new Map();
const handlers = {};
const pi = {
	registerTool: (t) => tools.set(t.name, t),
	on: (name, h) => { (handlers[name] ??= []).push(h); },
};
const ext = (await jiti.import(`${root}/pi/debugger.ts`)).default;
ext(pi);
const call = async (name, args = {}) => (await tools.get(name).execute("id", args, undefined, undefined, {})).content[0].text;
const steps = [];
const step = async (name, args) => {
	const text = await call(name, args);
	steps.push({ name, args, text });
	return text;
};
out.tools = [...tools.keys()];
if (process.env.DEBUGGER_SHELF) {
	// the debugger must run no code of the program: Tag prints a marker from hashCode, equals, toString and
	// compareTo once the scenario has armed it (tests/debugger/sample, ShelfScenarioTest)
	out.bp = await step("set_breakpoint", { file: "src/main/java/demo/Shelf.java", code: "return n;" });
	out.start = await step("debug_start", {});
	out.stop = await step("resume", { timeout_s: 120 });
	out.inspectThis = await step("inspect", { expr: "this", depth: 2 });
	out.countsSize = await step("inspect", { expr: "this.counts.size()" });
	out.byNameGet = await step("inspect", { expr: "this.byName.get(\"x\")" });
	out.plainGet = await step("inspect", { expr: "this.plain.get(\"y\")" });
	out.plainHas = await step("inspect", { expr: "this.plain.containsKey(\"z\")" });
	out.orderGet = await step("inspect", { expr: "this.order.get(1)" });
	out.fixedGet = await step("inspect", { expr: "this.fixed.get(2)" });
	out.tagsSize = await step("inspect", { expr: "this.tags.size()" });
	out.sortedSize = await step("inspect", { expr: "this.sorted.size()" });
	out.tagsContains = await step("inspect", { expr: "this.tags.contains(this.order.get(0))" });
	out.countsGet = await step("inspect", { expr: "this.counts.get(this.order.get(0))" });
	out.tagToString = await step("inspect", { expr: "this.order.get(0).toString()" });
	out.tagEquals = await step("inspect", { expr: "\"x\".equals(this.order.get(0))" });
	out.concat = await step("inspect", { expr: "\"t=\" + this.order.get(0)" });
	out.last = await step("inspect", { expr: "this.last" });
	out.first = await step("inspect", { expr: "this.first", depth: 2 });
	out.loaderField = await step("inspect", { expr: "this.loader.ucp" });
	out.classLoader = await step("inspect", { expr: "this.getClass().classLoader" });
	out.threadName = await step("inspect", { expr: "this.owner.name" });
	out.ownerShown = await step("inspect", { expr: "this.owner", depth: 2 });
	out.outsideExisting = await step("set_breakpoint", { file: "/etc/hostname", code: "x" });
	out.outsideMissing = await step("set_breakpoint", { file: "/nonexistent/Nope.java", code: "x" });
	out.outsideRelative = await step("set_breakpoint", { file: "../../etc/hostname", code: "x" });
	out.end = await step("resume", {});
	for (const h of handlers.session_shutdown ?? []) await h({ reason: "quit" });
	process.stdout.write(JSON.stringify(out));
	process.exit(0);
}
out.noSession = await step("resume", {});
out.bpLine = await step("set_breakpoint", { file: "src/main/java/demo/Counter.java", code: "total += word.length();" });
out.bpDuplicate = await step("set_breakpoint", { file: "src/main/java/demo/Counter.java", code: "}" });
out.bpExit = await step("set_breakpoint", { method: "demo.Counter#add", on: "exit" });
out.bpEntrySafe = await step("set_breakpoint", { method: "demo.Counter#safeAdd(java.lang.String)", on: "entry", condition: "word == null" });
out.start = await step("debug_start", {});
out.stop1 = await step("resume", { timeout_s: 120 });
out.inspectThis = await step("inspect", { expr: "this", depth: 1 });
out.inspectSize = await step("inspect", { expr: "this.seen.size()" });
out.inspectProjectCall = await step("inspect", { expr: "this.safeAdd(\"x\")" });
out.inspectFrame1 = await step("inspect", { expr: "locals", frame: 1 });
out.stop2 = await step("resume", {});
out.stop3 = await step("resume", {});
out.stop4 = await step("resume", {});
out.removeLine = await step("remove_breakpoint", { id: 1 });
out.stop5 = await step("resume", {});
out.stop6 = await step("resume", {});
out.stop7 = await step("resume", {});
out.restart = await step("debug_start", {});
out.stopAfterRestart = await step("resume", {});
out.removeExit = await step("remove_breakpoint", { id: 2 });
out.removeEntry = await step("remove_breakpoint", { id: 3 });
out.bpStatic = await step("set_breakpoint", { method: "demo.Counter#twice", on: "entry" });
out.stopStatic = await step("resume", {});
out.inspectStaticThis = await step("inspect", { expr: "this" });
out.bpBridge = await step("set_breakpoint", { method: "demo.Square#copy", on: "exit" });
out.stopBridge = await step("resume", {});
for (const h of handlers.tool_call ?? []) await h({ toolName: "bash", input: { command: "mvn" } });
out.afterBash = await step("resume", {});
out.third = await step("debug_start", {});
out.refused = await step("debug_start", {});
out.description = tools.get("debug_start").description;
for (const h of handlers.session_shutdown ?? []) await h({ reason: "quit" });
process.stdout.write(JSON.stringify(out));
process.exit(0);
