import assert from "node:assert/strict";

import { buildMergedCatalog } from "../src/catalog.mjs";
import { LISTED_MODELS } from "../src/model-registry.mjs";

const nativeModels = ["sol", "terra", "luna"].map((name, index) => ({
  slug: `gpt-5.6-${name}`,
  display_name: `native-${name}`,
  priority: index + 1,
  base_instructions: `native-${name}-instructions`,
  model_messages: { instructions_template: `native-${name}-messages` },
})).concat([
  { slug: "gpt-5.5", display_name: "native-legacy", priority: 10 },
  { slug: "codex-auto-review", display_name: "native-review", priority: 11 },
]);

const merged = buildMergedCatalog({ models: nativeModels }, LISTED_MODELS);
const expectedSlugs = [
  "gpt-5.6-sol",
  "gpt-5.6-terra",
  "gpt-5.6-luna",
  "mimo-token-plan/mimo-v2.5-pro",
  "mimo-token-plan/mimo-v2.5",
  "wlb-relay/gpt-5.6-sol",
  "wlb-relay/gpt-5.6-terra",
  "wlb-relay/gpt-5.6-luna",
];
assert.equal(merged.length, expectedSlugs.length);
assert.deepEqual(
  merged.map((model) => model.slug),
  expectedSlugs,
);
const wlbModels = LISTED_MODELS.filter((item) => item.provider === "wlb-relay");
assert.deepEqual(
  wlbModels.map((model) => model.slug),
  ["wlb-relay/gpt-5.6-sol", "wlb-relay/gpt-5.6-terra", "wlb-relay/gpt-5.6-luna"],
);
for (const model of wlbModels) {
  const native = nativeModels.find((item) => item.slug === model.upstreamModel);
  const routed = merged.find((item) => item.slug === model.slug);
  assert.deepEqual(routed, { ...native, slug: model.slug, display_name: model.displayName });
}
assert.equal(
  new Set(
    merged
      .filter((model) => model.slug.startsWith("wlb-relay/"))
      .map((model) => model.base_instructions),
  ).size,
  3,
);

assert.throws(
  () =>
    buildMergedCatalog(
      { models: nativeModels.filter((model) => model.slug !== "gpt-5.6-luna") },
      LISTED_MODELS,
    ),
  /missing exact upstream model gpt-5\.6-luna/,
);

const mimoKeys = [
  "slug",
  "display_name",
  "description",
  "default_reasoning_level",
  "supported_reasoning_levels",
  "shell_type",
  "visibility",
  "supported_in_api",
  "priority",
  "base_instructions",
  "supports_reasoning_summaries",
  "default_reasoning_summary",
  "support_verbosity",
  "truncation_policy",
  "supports_parallel_tool_calls",
  "supports_image_detail_original",
  "context_window",
  "max_context_window",
  "effective_context_window_percent",
  "experimental_supported_tools",
  "input_modalities",
  "supports_search_tool",
];
for (const model of merged.filter((item) => item.slug.startsWith("mimo-token-plan/"))) {
  const config = LISTED_MODELS.find((item) => item.slug === model.slug);
  assert.deepEqual(Object.keys(model).sort(), [...mimoKeys].sort());
  assert.equal(model.supports_reasoning_summaries, config.supportsReasoningSummaries);
  assert.equal(model.default_reasoning_summary, config.defaultReasoningSummary);
  assert.equal(model.support_verbosity, config.supportVerbosity);
  assert.deepEqual(model.truncation_policy, config.truncationPolicy);
  assert.equal(model.supports_parallel_tool_calls, config.supportsParallelToolCalls);
  assert.equal(model.context_window, config.contextWindow);
  assert.equal(model.max_context_window, config.contextWindow);
  for (const leaked of [
    "model_messages",
    "comp_hash",
    "service_tiers",
    "apply_patch_tool_type",
    "auto_compact_token_limit",
    "multi_agent_version",
  ]) {
    assert.equal(model[leaked], undefined, `${model.slug} leaked ${leaked}`);
  }
}

console.log("catalog metadata assertions passed");
