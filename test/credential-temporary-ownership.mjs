import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import assert from "node:assert/strict";
import test from "node:test";

const driver = String.raw`
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";
const [root, state, mode] = process.argv.slice(1);
process.env.MODEL_ROUTER_STATE_DIR = state;
process.env.CODEX_HOME = path.join(state, "home");
const originalOpen = fs.openSync, originalRename = fs.renameSync;
let foreign;
let attempted;
fs.openSync = (file, flags, ...rest) => {
  if (mode === "collision" && flags === "wx" && String(file).includes(".tmp.")) {
    foreign = file;
    fs.writeFileSync(file, "foreign");
  }
  return originalOpen(file, flags, ...rest);
};
fs.renameSync = (source, target) => {
  if (mode === "ordinary" && String(source).includes(".tmp.")) {
    attempted = source;
    throw new Error("injected rename failure");
  }
  if (mode === "replaced" && String(source).includes(".tmp.")) {
    originalRename(source, source + ".owned");
    fs.writeFileSync(source, "foreign");
    foreign = source;
    throw new Error("injected rename failure");
  }
  return originalRename(source, target);
};
syncBuiltinESMExports();
const { writeProviderCredential } = await import(pathToFileURL(path.join(root, "src/provider-credentials.mjs")));
let failure;
if (mode === "ordinary") {
  const { primaryCredentialPath } = await import(pathToFileURL(path.join(root, "src/provider-credentials.mjs")));
  const target = primaryCredentialPath((await import(pathToFileURL(path.join(root, "src/model-registry.mjs")))).PROVIDERS.get("wlb-relay"));
  fs.writeFileSync(target, "old");
}
try { writeProviderCredential("wlb-relay", "synthetic"); } catch (error) { failure = error; }
assert.ok(failure);
if (mode === "ordinary") {
  const { primaryCredentialPath } = await import(pathToFileURL(path.join(root, "src/provider-credentials.mjs")));
  const target = primaryCredentialPath((await import(pathToFileURL(path.join(root, "src/model-registry.mjs")))).PROVIDERS.get("wlb-relay"));
  assert.equal(fs.readFileSync(target, "utf8"), "old");
  assert.equal(fs.existsSync(attempted), false);
} else assert.equal(fs.readFileSync(foreign, "utf8"), "foreign");
if (mode === "collision") assert.equal(failure.code, "EEXIST");
else if (mode === "replaced") assert.equal(failure.cleanupError?.code, "ESTALE");
`;
for (const mode of ["collision", "replaced", "ordinary"]) test(`credential temporary ${mode} preserves foreign file`, () => {
  const state = mkdtempSync(path.join(os.tmpdir(), "credential-owned-"));
  try {
    execFileSync(process.execPath, ["--input-type=module", "-e", driver, process.cwd(), state, mode], { windowsHide: true, timeout: 30000 });
  } finally { rmSync(state, { recursive: true, force: true }); }
});
