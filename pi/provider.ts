/**
 * Pi extension: an OpenAI-compatible model provider configured from the environment.
 *
 * The runner sets these variables from config.yaml:
 *   KEVAL_PROVIDER_NAME   provider name used with `pi --provider` (default "custom")
 *   KEVAL_BASE_URL        base URL of the OpenAI-compatible API, e.g. https://api.example.com/v1
 *   KEVAL_API_KEY         API key (the runner copies it from the variable named in the config). The
 *                           extension reads it once and removes it from the process environment, so
 *                           that no command or test started by the agent inherits it.
 *   KEVAL_MODELS          optional JSON list of models; when empty, the list is fetched from
 *                           <base URL>/models. An entry is a model id or an object with the fields of
 *                           RemoteModel below.
 */

import type { ExtensionAPI, ProviderModelConfig } from "@earendil-works/pi-coding-agent";

interface RemoteModel {
	id: string;
	display_name?: string;
	context_window?: number;
	context_length?: number;
	max_output_tokens?: number;
	supports_vision?: boolean;
	supports_images?: boolean;
	supports_reasoning?: boolean;
	thinking?: { supported?: boolean; levels?: string[] } | null;
	pricing?: { input?: number; output?: number; cache_read?: number; cache_write?: number };
}

const THINKING_LEVELS = ["minimal", "low", "medium", "high", "xhigh"] as const;

function toModelConfig(m: RemoteModel): ProviderModelConfig {
	// Pi thinking levels mapped to the provider's level names; null = unsupported.
	const levels = new Set(m.thinking?.levels ?? (m.supports_reasoning ? THINKING_LEVELS : []));
	const thinkingLevelMap: ProviderModelConfig["thinkingLevelMap"] = {
		off: levels.has("none") ? "none" : null,
		minimal: levels.has("minimal") ? "minimal" : null,
		low: levels.has("low") ? "low" : null,
		medium: levels.has("medium") ? "medium" : null,
		high: levels.has("high") ? "high" : null,
		xhigh: levels.has("xhigh") ? "xhigh" : null,
	};
	const vision = m.supports_vision ?? m.supports_images ?? false;
	return {
		id: m.id,
		name: m.display_name ?? m.id,
		reasoning: m.supports_reasoning ?? m.thinking?.supported ?? false,
		thinkingLevelMap,
		input: vision ? ["text", "image"] : ["text"],
		cost: {
			input: m.pricing?.input ?? 0,
			output: m.pricing?.output ?? 0,
			cacheRead: m.pricing?.cache_read ?? 0,
			cacheWrite: m.pricing?.cache_write ?? 0,
		},
		contextWindow: m.context_window ?? m.context_length ?? 128000,
		maxTokens: m.max_output_tokens ?? 16384,
	};
}

/** Pauses before the retries of the model list request, milliseconds. */
export const MODEL_LIST_RETRY_DELAYS = [2000, 5000, 10000];

/** The model list of the provider. A failed or empty answer is retried after each delay; when every
 * attempt fails, the error says so (otherwise Pi would only report an unknown provider). */
export async function fetchModels(
	baseUrl: string,
	apiKey: string,
	delays: number[] = MODEL_LIST_RETRY_DELAYS,
	fetchImpl: typeof fetch = fetch,
): Promise<RemoteModel[]> {
	let last = "";
	for (let attempt = 0; attempt <= delays.length; attempt++) {
		if (attempt > 0) {
			await new Promise((resolve) => setTimeout(resolve, delays[attempt - 1]));
		}
		try {
			const response = await fetchImpl(`${baseUrl}/models`, { headers: { Authorization: `Bearer ${apiKey}` } });
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}`);
			}
			const payload = (await response.json()) as { data?: RemoteModel[] };
			if (payload.data?.length) {
				return payload.data;
			}
			last = "empty model list";
		} catch (error) {
			last = error instanceof Error ? error.message : String(error);
		}
		console.error(`[provider] failed to fetch models (attempt ${attempt + 1}/${delays.length + 1}): ${last}`);
	}
	throw new Error(`[provider] model list unavailable: ${baseUrl}/models failed ${delays.length + 1} times (${last})`);
}

/** Pi reads `$NAME` in an API key as an environment reference and a leading `!` as a command:
 * escape the key so that it is taken literally. */
export function literalConfigValue(value: string): string {
	const escaped = value.replace(/\$/g, "$$$$");
	return escaped.startsWith("!") ? `$!${escaped.slice(1)}` : escaped;
}

export default async function (pi: ExtensionAPI) {
	const apiKey = process.env.KEVAL_API_KEY ?? "";
	delete process.env.KEVAL_API_KEY;
	const baseUrl = (process.env.KEVAL_BASE_URL ?? "").replace(/\/+$/, "");
	if (!baseUrl) {
		return;
	}
	const name = process.env.KEVAL_PROVIDER_NAME || "custom";
	const configured = JSON.parse(process.env.KEVAL_MODELS || "[]") as (string | RemoteModel)[];
	let models: RemoteModel[];
	try {
		models = configured.length
			? configured.map((m) => (typeof m === "string" ? { id: m, supports_reasoning: true } : m))
			: await fetchModels(baseUrl, apiKey);
	} catch (error) {
		console.error(error instanceof Error ? error.message : String(error));
		throw error;
	}
	pi.registerProvider(name, {
		name,
		baseUrl,
		apiKey: literalConfigValue(apiKey),
		api: "openai-completions",
		models: models.map(toModelConfig),
	});
}
