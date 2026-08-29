import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, statSync } from "node:fs";

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
    "  [System.IO.File]::SetAccessControl($env:CODEX_ROUTER_PRIVATE_FILE, $acl)",
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
      },
    ).trim().toLowerCase() === "true";
  } catch {
    return false;
  }
}
