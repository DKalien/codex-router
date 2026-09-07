import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// 非交互后台调用应隐藏窗口；provider-key.mjs 的密钥输入继承操作员控制台。
const backgroundCalls = [
  ["src/file-security.mjs", "protectPrivateFile"],
  ["src/file-security.mjs", "privateFileIsProtected"],
  ["src/service-windows.mjs", "schtasks"],
  ["src/service-windows.mjs", "installTask"],
  ["src/service-windows.mjs", "findServiceProcess"],
  ["src/service-windows.mjs", "stopServiceProcess"],
  ["src/service-windows.mjs", "taskState"],
];

function functionBody(source, name) {
  const start = source.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `source is missing ${name}`);
  const rest = source.slice(start + 1);
  const next = rest.search(/\n(?:export )?function /);
  return source.slice(start, next === -1 ? source.length : start + 1 + next);
}

test("Windows 后台子进程均隐藏控制台", () => {
  for (const [relativePath, name] of backgroundCalls) {
    const source = readFileSync(path.join(root, relativePath), "utf8");
    assert.match(
      functionBody(source, name),
      /windowsHide:\s*true/,
      `${relativePath} ${name} must pass windowsHide: true`,
    );
  }
});

test("交互式 API key 输入保持可见", () => {
  const source = readFileSync(path.join(root, "src/provider-key.mjs"), "utf8");
  assert.match(source, /stdio:\s*\["inherit",\s*"pipe",\s*"inherit"\]/);
  assert.doesNotMatch(source, /windowsHide:\s*true/);
});
