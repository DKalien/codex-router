import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const sandbox = mkdtempSync(path.join(os.tmpdir(), "codex-router-credential-security-"));
const stateDir = path.join(sandbox, "state");
const codexHome = path.join(sandbox, "codex-home");
mkdirSync(stateDir, { recursive: true });
mkdirSync(codexHome, { recursive: true });
process.env.MODEL_ROUTER_STATE_DIR = stateDir;
process.env.CODEX_HOME = codexHome;

const [{ protectPrivateFile, privateFileIsProtected }, { PROVIDERS }, credentials] =
  await Promise.all([
    import("../src/file-security.mjs"),
    import("../src/model-registry.mjs"),
    import("../src/provider-credentials.mjs"),
  ]);

const provider = PROVIDERS.get("mimo-token-plan");

test.after(() => rmSync(sandbox, { recursive: true, force: true }));

test("credential resolution ignores symlinked and non-file candidates", (t) => {
  const candidate = path.join(stateDir, provider.credential.file);
  const outside = path.join(sandbox, "outside-secret");
  writeFileSync(outside, "TEST_SYMLINK_SECRET\n", { mode: 0o600 });
  try {
    symlinkSync(outside, candidate);
  } catch (error) {
    if (process.platform === "win32" && ["EACCES", "EPERM"].includes(error?.code)) {
      t.skip("Windows symlink creation is unavailable");
      return;
    }
    throw error;
  }
  try {
    assert.equal(credentials.resolveProviderCredential(provider, { persistent: true }), undefined);
    unlinkSync(candidate);
    mkdirSync(candidate);
    assert.equal(credentials.resolveProviderCredential(provider, { persistent: true }), undefined);
  } finally {
    rmSync(candidate, { recursive: true, force: true });
  }
});

test(
  "Windows private ACL rejects and removes a foreign explicit Allow rule",
  { skip: process.platform !== "win32" },
  () => {
    const target = path.join(stateDir, "acl-secret");
    writeFileSync(target, "TEST_ACL_SECRET\n", { mode: 0o600 });
    try {
      protectPrivateFile(target);
      assert.equal(privateFileIsProtected(target), true);
      execFileSync(
        "powershell.exe",
        [
          "-NoLogo",
          "-NoProfile",
          "-NonInteractive",
          "-Command",
          [
            "$acl = [System.IO.File]::GetAccessControl($env:CODEX_ROUTER_PRIVATE_FILE)",
            "$sid = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')",
            "$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, [System.Security.AccessControl.FileSystemRights]::Read, [System.Security.AccessControl.InheritanceFlags]::None, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)",
            "[void]$acl.AddAccessRule($rule)",
            "[System.IO.File]::SetAccessControl($env:CODEX_ROUTER_PRIVATE_FILE, $acl)",
          ].join("; "),
        ],
        { env: { ...process.env, CODEX_ROUTER_PRIVATE_FILE: target }, stdio: "ignore" },
      );
      assert.equal(privateFileIsProtected(target), false);
      protectPrivateFile(target);
      assert.equal(privateFileIsProtected(target), true);
    } finally {
      rmSync(target, { force: true });
    }
  },
);
