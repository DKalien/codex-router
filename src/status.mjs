import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { credentialStatus } from "./provider-credentials.mjs";
import { readGptRoute } from "./gpt-route.mjs";
import { providerSelectionStatus } from "./provider-selection.mjs";
import { LISTED_MODELS, PROVIDERS } from "./model-registry.mjs";
import { readInstallManifest } from "./install-manifest.mjs";
import {
  CONFIG_PATH,
  MERGED_CATALOG_PATH,
  PORTS,
  STATE_DIR,
  loopback,
} from "./paths.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const EXPECTED_NATIVE_SLUGS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"];
const EXPECTED_CATALOG_SLUGS = new Set([
  ...EXPECTED_NATIVE_SLUGS,
  ...LISTED_MODELS.map((model) => model.slug),
]);
const argumentsList = process.argv.slice(2);
const jsonOutput = argumentsList.includes("--json");
const invalidArguments = argumentsList.some((argument) => argument !== "--json");

function childJson(script, args = []) {
  const result = spawnSync(process.execPath, [path.join(root, "src", script), ...args], {
    cwd: root,
    encoding: "utf8",
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: 128 * 1024,
    timeout: 10_000,
  });
  if (result.error || result.status !== 0) return undefined;
  try {
    return JSON.parse(result.stdout || "");
  } catch {
    return undefined;
  }
}

async function routerHealth() {
  try {
    const response = await fetch(loopback(PORTS.router, "/health"), {
      signal: AbortSignal.timeout(1_500),
    });
    const payload = await response.json().catch(() => ({}));
    const ready = response.ok && payload?.service === "codex-router";
    return {
      ok: ready,
      status: response.status,
      router: payload?.router || (ready ? "ready" : "unknown"),
      service: payload?.service || "unknown",
      version: payload?.version || undefined,
    };
  } catch {
    return { ok: false, status: 0, router: "unreachable", service: "unknown" };
  }
}

function catalogStatus() {
  try {
    const parsed = JSON.parse(readFileSync(MERGED_CATALOG_PATH, "utf8"));
    const models = Array.isArray(parsed.models) ? parsed.models : [];
    const slugs = models.map((model) => model?.slug).filter((slug) => typeof slug === "string");
    const uniqueSlugs = new Set(slugs);
    return {
      path: MERGED_CATALOG_PATH,
      total: models.length,
      routed: models.filter((model) =>
        typeof model?.slug === "string" &&
        [...PROVIDERS.keys()].some((provider) => model.slug.startsWith(`${provider}/`)),
      ).length,
      exact:
        models.length === EXPECTED_CATALOG_SLUGS.size &&
        slugs.length === models.length &&
        uniqueSlugs.size === EXPECTED_CATALOG_SLUGS.size &&
        [...EXPECTED_CATALOG_SLUGS].every((slug) => uniqueSlugs.has(slug)),
      readable: true,
    };
  } catch {
    return { path: MERGED_CATALOG_PATH, total: 0, routed: 0, exact: false, readable: false };
  }
}

function configStatus() {
  const snapshot = childJson("config-manager.mjs", ["status"]);
  if (!snapshot) {
    return {
      readable: existsSync(CONFIG_PATH),
      managed: false,
      mode: "unknown",
      catalogMode: "unknown",
    };
  }
  const catalogMode = !snapshot.model_catalog_json
    ? "official"
    : snapshot.model_catalog_json === MERGED_CATALOG_PATH
      ? "merged"
      : "custom";
  return {
    readable: true,
    managed: snapshot.mode === "router",
    mode: snapshot.mode || "unknown",
    catalogConfigured: snapshot.model_catalog_json === MERGED_CATALOG_PATH,
    catalogMode,
  };
}

function providersStatus() {
  const selection = providerSelectionStatus();
  const selected = new Set(selection.providers);
  return {
    selection: {
      explicit: selection.explicit,
      providers: selection.providers,
      degraded: selection.degraded,
    },
    providers: [...PROVIDERS.values()].map((provider) => ({
      id: provider.id,
      selected: selected.has(provider.id),
      ...credentialStatus(provider, { persistent: true }),
    })),
  };
}

function gptRouteStatus(providers) {
  try {
    const mode = readGptRoute();
    const wlb = providers.providers.find((provider) => provider.id === "wlb-relay");
    return {
      readable: true,
      mode,
      ready: mode === "official" || Boolean(wlb?.selected && wlb?.configured),
    };
  } catch (error) {
    return {
      readable: false,
      mode: "unknown",
      ready: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

function serviceStatus() {
  const status = childJson("service.mjs", ["status"]);
  return status
    ? { available: true, ...status }
    : { available: false, installed: false, loaded: false, state: "unknown" };
}

function manifestStatus() {
  const manifest = readInstallManifest();
  return manifest
    ? {
        installed: true,
        version: manifest.current?.packageVersion || null,
        installedAt: manifest.current?.installedAt || null,
        sourceRoot: manifest.current?.sourceRoot || null,
      }
    : { installed: false };
}

export async function collectStatus() {
  const health = await routerHealth();
  const config = configStatus();
  const catalog = catalogStatus();
  const providers = providersStatus();
  const gptRoute = gptRouteStatus(providers);
  const service = serviceStatus();
  const manifest = manifestStatus();
  const selectedProviders = providers.providers.filter((provider) => provider.selected);
  const routedProvidersConfigured =
    selectedProviders.length > 0 && selectedProviders.every((provider) => provider.configured);
  const status = {
    health,
    config,
    catalog,
    gptRoute,
    providers,
    manifest,
    service,
    paths: { stateDir: STATE_DIR },
  };
  return { ok: statusIsReady(status, routedProvidersConfigured), ...status };
}

export function statusIsReady(status, routedProvidersConfigured) {
  const officialCatalog = status.config.catalogMode === "official";
  const catalogReady =
    officialCatalog ||
    (status.config.catalogConfigured && status.catalog.readable && status.catalog.exact);
  const gptRouteReady =
    !status.gptRoute ||
    (status.gptRoute.readable === true && status.gptRoute.ready === true);
  return Boolean(
    status.health.ok &&
      status.config.managed &&
      catalogReady &&
      gptRouteReady &&
      !status.providers.selection.degraded &&
      (officialCatalog ? gptRouteReady : routedProvidersConfigured),
  );
}

function printText(status) {
  console.log(`Codex Router: ${status.ok ? "ready" : "needs attention"}`);
  console.log(
    `Router health: ${status.health.ok ? "ready" : "unreachable"}` +
      (status.health.version ? ` (${status.health.version})` : ""),
  );
  console.log(
    `Codex config: ${status.config.managed ? "managed by this router" : "not managed by this router"}`,
  );
  if (status.config.catalogMode === "official") {
    console.log("目录：官方 Codex 目录");
  } else {
    console.log(`Catalog: ${status.catalog.total} models, ${status.catalog.routed} routed`);
    if (!status.catalog.exact) console.log("  Catalog entries do not match the required 8-model set.");
  }
  if (status.gptRoute) {
    console.log(`GPT 路由：${status.gptRoute.mode}`);
    if (!status.gptRoute.readable) console.log(`  ${status.gptRoute.error}`);
    else if (!status.gptRoute.ready) console.log("  WLB 凭据或 provider selection 缺失。");
  }
  console.log(
    `Provider selection: ${status.providers.selection.explicit ? "explicit" : "default"} (${status.providers.selection.providers.join(", ") || "none"})`,
  );
  for (const provider of status.providers.providers) {
    console.log(
      `  ${provider.id}: ${provider.selected ? "selected" : "not selected"}, ` +
        `${provider.configured ? "credential configured" : "credential missing"}`,
    );
  }
  if (status.providers.selection.degraded) {
    console.log(`  Provider selection degraded: ${status.providers.selection.degraded}`);
  }
  console.log(
    `Install manifest: ${status.manifest.installed ? "present" : "missing"}; ` +
      `service: ${status.service.state || "unknown"}${status.service.installed ? " (installed)" : ""}`,
  );
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (invalidArguments) {
    console.error("Usage: status.mjs [--json]");
    process.exitCode = 2;
  } else {
    try {
      const status = await collectStatus();
      if (jsonOutput) process.stdout.write(`${JSON.stringify(status, null, 2)}\n`);
      else printText(status);
      process.exitCode = status.ok ? 0 : 1;
    } catch (error) {
      console.error(error instanceof Error ? error.message : String(error));
      process.exitCode = 1;
    }
  }
}
