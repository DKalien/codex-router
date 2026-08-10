import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { protectPrivateFile } from "./file-security.mjs";
import {
  ANNOUNCED_MODELS_PATH,
  CONFIG_PATH,
  MERGED_CATALOG_PATH,
  NATIVE_CATALOG_PATH,
} from "./paths.mjs";
import { codexAuthStatus, codexVersion, runCodex } from "./codex-binary.mjs";
import { MODEL_BY_SLUG } from "./model-registry.mjs";
import { selectedConfiguredListedModels } from "./provider-selection.mjs";
import { assertStateOwnership } from "./state-owner.mjs";




const refresh = process.argv.includes("--refresh-native");
const bundled = process.argv.includes("--bundled-native");

function atomicJson(target, value) {
  mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  const temporary = `${target}.tmp.${process.pid}`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  protectPrivateFile(temporary);
  renameSync(temporary, target);
  protectPrivateFile(target);
}

function captureNative() {
  const args = ["debug", "models"];
  if (bundled) args.push("--bundled");
  let output;
  try {
    output = runCodex(args, {
      encoding: "utf8",
      timeout: 30_000,
      maxBuffer: 32 * 1024 * 1024,
    });
  } catch (error) {
    if (bundled) throw error;
    output = runCodex(["debug", "models", "--bundled"], {
      encoding: "utf8",
      timeout: 30_000,
      maxBuffer: 32 * 1024 * 1024,
    });
  }
  const parsed = JSON.parse(output);
  if (!parsed || !Array.isArray(parsed.models) || parsed.models.length === 0) {
    throw new Error("Codex returned an empty or invalid model catalog.");
  }
  if (parsed.models.some((model) => MODEL_BY_SLUG.has(String(model.slug)))) {
    throw new Error(
      "Refusing to capture an already-merged catalog. Disable the router before refreshing native models.",
    );
  }
  const capturedWith = codexVersion();
  atomicJson(NATIVE_CATALOG_PATH, {
    ...(capturedWith ? { captured_with: capturedWith } : {}),
    models: parsed.models,
  });
  return parsed;
}

// A native capture is only trustworthy for the Codex build that produced it:
// newer builds can require catalog fields the older build never emitted, or
// carry different capability values for the same slug. An unknown current
// version keeps the cache — with no binary to re-ask, stale is the best we
// have.
export function nativeCatalogIsReusable(parsed, currentVersion) {
  if (!parsed || !Array.isArray(parsed.models) || parsed.models.length === 0) {
    return false;
  }
  return !currentVersion || parsed.captured_with === currentVersion;
}

function nativeCatalog() {
  if (!existsSync(NATIVE_CATALOG_PATH) || refresh) return captureNative();
  const parsed = JSON.parse(readFileSync(NATIVE_CATALOG_PATH, "utf8"));
  if (nativeCatalogIsReusable(parsed, codexVersion())) return parsed;
  try {
    return captureNative();
  } catch (error) {
    // Version-mismatched is still better than empty: serve the stale capture
    // when the re-capture fails, but say so instead of hiding it.
    if (parsed && Array.isArray(parsed.models) && parsed.models.length > 0) {
      console.error(
        `Could not refresh the native model catalog (${error.message}); reusing the cached capture.`,
      );
      return parsed;
    }
    throw error;
  }
}

// Codex's picker deserializes reasoning efforts into a fixed enum and
// silently drops any level it does not recognize, so a curated "max" level
// simply vanishes from the effort menu on builds whose enum ends at xhigh
// (issue #57). No runtime probe can see this: config parsing accepts unknown
// effort strings, and `debug models` passes catalog levels through as plain
// strings even on builds whose picker cannot offer them. The enum history is
// the only reliable signal — max and ultra joined in 0.143.0 (verified
// against the published binaries: 0.142.5 lacks the serde variants, 0.143.0
// carries them), and the baseline predates this router. An unknown version
// clamps: a wrongly clamped Max still routes at full effort under the xhigh
// label, while a wrongly emitted max is exactly the missing-picker-entry bug.
const BASELINE_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"];
const EFFORT_LADDER = [...BASELINE_EFFORTS, "max", "ultra"];
const MAX_EFFORT_SINCE = [0, 143, 0];
const ALLOWED_NATIVE_SLUGS = new Set([
  "gpt-5.6-sol",
  "gpt-5.6-terra",
  "gpt-5.6-luna",
]);

export function codexEffortVocabulary(version) {
  const match = /(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.]+)?/.exec(String(version || ""));
  if (!match) return new Set(BASELINE_EFFORTS);
  const installed = [Number(match[1]), Number(match[2]), Number(match[3])];
  for (let index = 0; index < 3; index += 1) {
    if (installed[index] > MAX_EFFORT_SINCE[index]) return new Set(EFFORT_LADDER);
    if (installed[index] < MAX_EFFORT_SINCE[index]) return new Set(BASELINE_EFFORTS);
  }
  // Exactly the boundary release: prereleases of it may predate the variants.
  return match[4] ? new Set(BASELINE_EFFORTS) : new Set(EFFORT_LADDER);
}

function clampEffort(effort, vocabulary) {
  if (vocabulary.has(effort)) return effort;
  const start = EFFORT_LADDER.indexOf(effort);
  // Off-ladder values cannot be ranked, so pass them through unchanged.
  if (start === -1) return effort;
  for (let index = start - 1; index >= 0; index -= 1) {
    if (vocabulary.has(EFFORT_LADDER[index])) return EFFORT_LADDER[index];
  }
  return effort;
}

// Registry levels are ordered lightest-first, so when a clamped level lands on
// an effort the model already offers (xhigh + max both become xhigh), the
// genuine entry keeps its slot and the clamped duplicate is dropped.
export function clampModelEfforts(models, vocabulary) {
  return models.map((model) => {
    if (!Array.isArray(model.reasoningLevels)) return model;
    const levels = [];
    const seen = new Set();
    for (const level of model.reasoningLevels) {
      const effort = clampEffort(level.effort, vocabulary);
      if (seen.has(effort)) continue;
      seen.add(effort);
      levels.push(effort === level.effort ? level : { ...level, effort });
    }
    const defaultEffort = clampEffort(model.defaultEffort, vocabulary);
    if (
      defaultEffort === model.defaultEffort &&
      levels.length === model.reasoningLevels.length &&
      levels.every((level, index) => level === model.reasoningLevels[index])
    ) {
      return model;
    }
    return { ...model, reasoningLevels: levels, defaultEffort };
  });
}

function selectedModel() {
  if (!existsSync(CONFIG_PATH)) return undefined;
  const config = readFileSync(CONFIG_PATH, "utf8");
  const firstTable = config.search(/^\s*\[/m);
  const root = firstTable === -1 ? config : config.slice(0, firstTable);
  return root.match(/^\s*model\s*=\s*["\']([^"\']+)["\']/m)?.[1];
}

function normalizeNativeModel(model) {
  return {
    ...model,
    supports_reasoning_summaries:
      typeof model.supports_reasoning_summaries === "boolean"
        ? model.supports_reasoning_summaries
        : false,
  };
}

export function routedModel(template, model) {
  if (model.provider === "wlb-relay") {
    if (!template || template.slug !== model.upstreamModel) {
      throw new Error(
        `Native catalog is missing exact upstream model ${model.upstreamModel} required by ${model.slug}.`,
      );
    }
    return {
      ...template,
      slug: model.slug,
      display_name: model.displayName ?? template.display_name,
    };
  }
  if (model.provider !== "mimo-token-plan") {
    throw new Error(`Unsupported routed model provider ${model.provider}.`);
  }
  return {
    slug: model.slug,
    display_name: model.displayName,
    description: model.description,
    default_reasoning_level: model.defaultEffort,
    supported_reasoning_levels: model.reasoningLevels,
    shell_type: "shell_command",
    visibility: "list",
    supported_in_api: true,
    priority: model.priority,
    base_instructions: model.baseInstructions,
    supports_reasoning_summaries: model.supportsReasoningSummaries,
    default_reasoning_summary: model.defaultReasoningSummary,
    support_verbosity: model.supportVerbosity,
    truncation_policy: model.truncationPolicy,
    supports_parallel_tool_calls: model.supportsParallelToolCalls,
    supports_image_detail_original: model.supportsImageDetailOriginal === true,
    context_window: model.contextWindow,
    max_context_window: model.contextWindow,
    effective_context_window_percent: 95,
    experimental_supported_tools: [],
    input_modalities: model.inputModalities,
    supports_search_tool: false,
  };
}

export const AUTO_ANNOUNCE_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;

function formatTokenCount(tokens) {
  if (tokens >= 995_000) {
    const millions = Math.round((tokens / 1_000_000) * 10) / 10;
    return `${millions % 1 === 0 ? Math.round(millions) : millions}M`;
  }
  return `${Math.round(tokens / 1000)}K`;
}

function joinNaturally(parts) {
  if (parts.length <= 1) return parts.join("");
  if (parts.length === 2) return `${parts[0]} and ${parts[1]}`;
  return `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
}

// Announcement copy is assembled from verified registry capabilities only, so
// it can never claim more than the picker metadata already does.
function autoAnnouncementCopy(model) {
  const details = [];
  if (Number.isInteger(model.contextWindow)) {
    details.push(`a ${formatTokenCount(model.contextWindow)}-token context window`);
  }
  const efforts = Array.isArray(model.reasoningLevels)
    ? model.reasoningLevels.map((level) => level.effort)
    : [];
  if (efforts.length > 1) {
    details.push(`reasoning efforts from ${efforts[0]} to ${efforts[efforts.length - 1]}`);
  }
  if ((model.inputModalities || []).includes("image")) {
    details.push("image input");
  }
  const capabilities = details.length ? ` It comes with ${joinNaturally(details)}.` : "";
  return `${model.displayName} just landed in your model picker.${capabilities}`;
}

// A new checked-in model announces itself for a window of rebuilds rather
// than a single one, because catalogs rebuild on updates and provider toggles
// and the operator may not launch Codex in between; Codex itself stops the
// card after four showings per slug. The first capture seeds silently so an
// install never announces the entire catalog, and locally curated models are
// excluded because the operator added those deliberately. Only models whose
// provider is selected and credentialed ever reach this list, so a model the
// operator cannot use never announces.
export function annotateNewModelAnnouncements(routedModelsList, announcedAt, userSlugs, now) {
  const firstRun = announcedAt === null;
  const nextAnnouncedAt = new Map(firstRun ? [] : announcedAt);
  const models = routedModelsList.map((model) => {
    if (!nextAnnouncedAt.has(model.slug)) {
      nextAnnouncedAt.set(model.slug, firstRun ? 0 : now);
    }
    if (model.availabilityNux || userSlugs.has(model.slug)) return model;
    const since = nextAnnouncedAt.get(model.slug);
    if (since === 0 || now - since >= AUTO_ANNOUNCE_WINDOW_MS) return model;
    return { ...model, availabilityNux: autoAnnouncementCopy(model) };
  });
  return { models, announcedAt: nextAnnouncedAt };
}

function readAnnouncedAt() {
  if (!existsSync(ANNOUNCED_MODELS_PATH)) return null;
  try {
    const parsed = JSON.parse(readFileSync(ANNOUNCED_MODELS_PATH, "utf8"));
    if (!parsed || typeof parsed.models !== "object" || Array.isArray(parsed.models)) {
      return null;
    }
    return new Map(
      Object.entries(parsed.models).filter(([, value]) => Number.isFinite(value)),
    );
  } catch {
    // Corrupt state must reseed silently, not announce the whole catalog.
    return null;
  }
}

function writeAnnouncedAt(announcedAt) {
  atomicJson(ANNOUNCED_MODELS_PATH, {
    version: 1,
    models: Object.fromEntries([...announcedAt.entries()].sort()),
  });
}

function sortCatalogModels(models) {
  const group = (model) => {
    const slug = String(model.slug);
    if (!slug.includes("/")) return 0;
    return slug.startsWith("mimo-token-plan/") ? 1 : 2;
  };
  return [...models].sort((left, right) => {
    const providerGroup = group(left) - group(right);
    const priority = Number(left.priority ?? 999) - Number(right.priority ?? 999);
    return providerGroup || priority || String(left.slug).localeCompare(String(right.slug));
  });
}

export function buildMergedCatalog(native, routedModelsList, { includeNative = true } = {}) {
  if (!native || !Array.isArray(native.models) || native.models.length === 0) {
    throw new Error("Native model catalog is empty.");
  }
  const models = new Map(
    includeNative
      ? native.models
          .filter((model) => ALLOWED_NATIVE_SLUGS.has(model.slug))
          .map((model) => [model.slug, normalizeNativeModel(model)])
      : [],
  );
  for (const model of routedModelsList) {
    const template =
      model.provider === "wlb-relay"
        ? native.models.find((nativeModel) => nativeModel.slug === model.upstreamModel)
        : undefined;
    models.set(model.slug, routedModel(template, model));
  }
  return sortCatalogModels(models.values());
}

function main() {
  // The catalog is what Codex offers in its picker. Writing it from a checkout
  // that does not own this state directory is how the picker ends up
  // advertising models the running gateway has no route for.
  assertStateOwnership("write the Codex model catalog");
  const selectedModels = selectedConfiguredListedModels();
  // Clamp before announcements so every surface Codex reads — picker levels,
  // defaults, and announcement copy — stays inside the effort vocabulary the
  // installed build can actually deserialize.
  const { models: routedModels, announcedAt } = annotateNewModelAnnouncements(
    clampModelEfforts(selectedModels, codexEffortVocabulary(codexVersion())),
    readAnnouncedAt(),
    new Set(),
    Date.now(),
  );
  const native = nativeCatalog();
  // Dropping every native model is destructive, so only do it when Codex
  // actually answered that the session is signed out. If the probe could not
  // run at all we do not know, and guessing "signed out" is what silently
  // emptied the picker for Windows npm installs.
  const auth = codexAuthStatus();
  if (auth.reason === "probe-failed") {
    throw new Error(
      `Could not ask Codex whether it is signed in (${auth.code || "spawn failed"} running ${auth.binary}). ` +
        "Refusing to rebuild the catalog, because assuming a signed-out session would remove every native model. " +
        "Set CODEX_BIN to a runnable Codex CLI and try again.",
    );
  }
  const openaiAuthenticated = auth.authenticated;
  // Native models join the catalog only when the auth probe says the session
  // can actually spend them; a signed-out session keeps only routed models.
  const merged = buildMergedCatalog(native, routedModels, {
    includeNative: openaiAuthenticated,
  });
  atomicJson(MERGED_CATALOG_PATH, { models: merged });
  writeAnnouncedAt(announcedAt);
  process.stdout.write(
    `${JSON.stringify({
      path: MERGED_CATALOG_PATH,
      models: merged.length,
      routed_models: routedModels.length,
      native_models: openaiAuthenticated
        ? merged.filter((model) => !MODEL_BY_SLUG.has(String(model.slug))).length
        : 0,
      openai_authenticated: openaiAuthenticated,
      openai_auth_reason: auth.reason,
      selected_model: selectedModel() || null,
    })}\n`,
  );
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main();
  } catch (error) {
    // Ownership conflicts are an operator mistake with a specific remedy, so
    // print the guidance rather than a stack trace.
    if (error?.code === "foreign_state_owner") {
      console.error(error.message);
      process.exit(1);
    }
    throw error;
  }
}
