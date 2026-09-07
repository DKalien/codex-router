import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";

import {
  MERGED_CATALOG_PATH,
  NATIVE_CATALOG_PATH,
  SOURCE_ROOT,
} from "./paths.mjs";

function run(script, args = []) {
  execFileSync(process.execPath, [path.join(SOURCE_ROOT, "src", script), ...args], {
    cwd: SOURCE_ROOT,
    env: process.env,
    stdio: ["ignore", "ignore", "inherit"],
  });
}

export function targetCli(command) {
  return `./bin/${command}`;
}

export function targetPickerName() {
  return "Codex";
}

function usesManagedCatalog() {
  try {
    const output = execFileSync(
      process.execPath,
      [path.join(SOURCE_ROOT, "src", "config-manager.mjs"), "status"],
      { cwd: SOURCE_ROOT, env: process.env, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], timeout: 10_000 },
    );
    return JSON.parse(output).model_catalog_json === MERGED_CATALOG_PATH;
  } catch {
    return false;
  }
}

export function refreshTargetPickerIfInstalled() {
  if (
    !existsSync(NATIVE_CATALOG_PATH) ||
    !usesManagedCatalog()
  ) {
    return false;
  }
  run("catalog.mjs");
  return true;
}
