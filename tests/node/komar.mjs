// Checks of the komar-mode command forms of pi/guard.ts, run by tests/test_komar.py with node and the jiti
// loader of the installed Pi package: node komar.mjs <pi package dir> <repository root>, with
// {"scenario": [...], "commands": [...]} on stdin. Prints one JSON object with the results.
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [piDir, root] = process.argv.slice(2);
const { createJiti } = await import(pathToFileURL(`${piDir}/node_modules/jiti/lib/jiti.mjs`).href);
const jiti = createJiti(import.meta.url, {
	alias: { "@earendil-works/pi-coding-agent": `${piDir}/dist/index.js` },
});
const guard = await jiti.import(`${root}/pi/guard.ts`);
const input = JSON.parse(readFileSync(0, "utf-8"));

const out = {
	kinds: input.commands.map((c) => guard.komarCommandKind(c, input.scenario) ?? null),
	compactKinds: input.commands.map((c) => guard.komarCommandKind(c, input.scenario, true) ?? null),
	viewKinds: (input.views ?? []).map((c) => guard.komarCommandKind(c, input.scenario) ?? null),
	compactViewKinds: (input.views ?? []).map((c) => guard.komarCommandKind(c, input.scenario, true) ?? null),
	compactReads: (input.reads ?? []).map(([tool, rel, glob]) => guard.compactReadDenied(tool, rel, glob ?? undefined) !== undefined),
	spec: ["komar/a.yaml", "komar/run-2.v1.yaml", "komar/a.jsonl", "komar/../a.yaml", "src/a.yaml"].map((p) =>
		guard.KOMAR_SPEC.test(p),
	),
};

// the bash timeout the agent gives a komar run is dropped (the wrapper bounds the run); other commands keep it
const dir = mkdtempSync(join(tmpdir(), "komar-guard-"));
writeFileSync(join(dir, "guard.json"), JSON.stringify({
	workspace: dir, output_test_file: "T.java", commands: { test: ["mvn", "test"] }, command_env: [],
	komar: { scenario: input.scenario },
}));
process.env.GUARD_CONFIG = join(dir, "guard.json");
for (const k of ["GUARD_DENIED_LOG", "GUARD_COMMAND_LOG", "GUARD_OUTPUT_VERSION_LOG"]) process.env[k] = join(dir, `${k}.jsonl`);
const handlers = {};
guard.default({ on: (name, h) => { (handlers[name] ??= []).push(h); }, registerTool: () => {} });
out.timeouts = [];
for (const command of [input.commands[0], "mvn test"]) {
	const event = { toolName: "bash", toolCallId: command, input: { command, timeout: 120 } };
	let blocked = false;
	for (const h of handlers.tool_call ?? []) blocked = blocked || Boolean((await h(event))?.block);
	out.timeouts.push(blocked ? "blocked" : event.input.timeout ?? null);
}
process.stdout.write(JSON.stringify(out));
