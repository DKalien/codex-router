import { execFile, execFileSync } from "node:child_process";
import { chmodSync, existsSync, statSync } from "node:fs";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const ACL_TIMEOUT_MS = 10_000;

function powershellPrivateScript() {
  return [
    "$ErrorActionPreference = 'Stop'",
    "try {",
    "  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User",
    "  $fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl",
    "  $none = [System.Security.AccessControl.InheritanceFlags]::None",
    "  $propagationNone = [System.Security.AccessControl.PropagationFlags]::None",
    "  $allow = [System.Security.AccessControl.AccessControlType]::Allow",
    "  $acl = [System.Security.AccessControl.FileSecurity]::new()",
    "  [void]$acl.SetAccessRuleProtection($true, $false)",
    "  $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, $fullControl, $none, $propagationNone, $allow)",
    "  [void]$acl.AddAccessRule($rule)",
    "  if ($PSVersionTable.PSEdition -eq 'Core') {",
    "    [System.IO.FileSystemAclExtensions]::SetAccessControl([System.IO.FileInfo]::new($env:CODEX_ROUTER_PRIVATE_FILE), $acl)",
    "  } else {",
    "    [System.IO.File]::SetAccessControl($env:CODEX_ROUTER_PRIVATE_FILE, $acl)",
    "  }",
    "} catch {",
    "  [Console]::Error.WriteLine($_.Exception.Message)",
    "  exit 1",
    "}",
  ].join("\n");
}

export function protectPrivateFile(target) {
  chmodSync(target, 0o600);
  if (process.platform !== "win32") return target;
  try {
    execFileSync(
      "powershell.exe",
      ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", powershellPrivateScript()],
      {
        encoding: "utf8",
        env: { ...process.env, CODEX_ROUTER_PRIVATE_FILE: target },
        stdio: ["ignore", "ignore", "pipe"],
        windowsHide: true,
        timeout: ACL_TIMEOUT_MS,
      },
    );
  } catch (error) {
    const detail = String(error?.stderr?.trim?.() || error?.message || "").trim();
    throw new Error(
      detail
        ? `Failed to protect private file ACL: ${detail}`
        : "Failed to protect private file ACL.",
    );
  }
  return target;
}

export async function protectPrivateFileAsync(target, { signal } = {}) {
  signal?.throwIfAborted();
  chmodSync(target, 0o600);
  if (process.platform !== "win32") return target;
  const deadline = performance.now() + ACL_TIMEOUT_MS;
  let lastError;
  for (const shell of ["pwsh.exe", "powershell.exe"]) {
    signal?.throwIfAborted();
    const timeout = Math.ceil(deadline - performance.now());
    if (timeout <= 0) break;
    try {
      await execFileAsync(
        shell,
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", powershellPrivateScript()],
        {
          encoding: "utf8",
          env: { ...process.env, CODEX_ROUTER_PRIVATE_FILE: target },
          windowsHide: true,
          timeout,
          signal,
        },
      );
      signal?.throwIfAborted();
      return target;
    } catch (error) {
      signal?.throwIfAborted();
      lastError = error;
    }
  }
  const detail = String(lastError?.stderr?.trim?.() || lastError?.message || "").trim();
  throw new Error(detail ? `Failed to protect private file ACL: ${detail}` : "Failed to protect private file ACL.");
}

export function privateFileIsProtected(target) {
  if (!existsSync(target)) return false;
  if (process.platform !== "win32") return (statSync(target).mode & 0o777) === 0o600;
  const script = [
    // Get-Acl lazy-loads Microsoft.PowerShell.Security, which can fail under
    // concurrent Windows processes. The .NET API returns the same FileSecurity
    // object without importing a PowerShell module.
    "$acl = [System.IO.File]::GetAccessControl($env:CODEX_ROUTER_PRIVATE_FILE)",
    "$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
    "$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))",
    "$fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl",
    "$hasFullControl = $false",
    "$hasForeignAllow = $false",
    "$hasInheritedRule = $false",
    "foreach ($rule in $rules) { if ($rule.IsInherited) { $hasInheritedRule = $true }; if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow) { if ($rule.IdentityReference.Value -eq $sid) { if (($rule.FileSystemRights -band $fullControl) -eq $fullControl) { $hasFullControl = $true } } else { $hasForeignAllow = $true } } }",
    "[Console]::Out.Write(($acl.AreAccessRulesProtected -and -not $hasInheritedRule -and $hasFullControl -and -not $hasForeignAllow).ToString())",
  ].join("; ");
  try {
    return execFileSync(
      "powershell.exe",
      ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
      {
        encoding: "utf8",
        env: { ...process.env, CODEX_ROUTER_PRIVATE_FILE: target },
        stdio: ["ignore", "pipe", "ignore"],
        windowsHide: true,
        timeout: ACL_TIMEOUT_MS,
      },
    ).trim().toLowerCase() === "true";
  } catch {
    return false;
  }
}
