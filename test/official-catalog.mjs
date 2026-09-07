import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { statusIsReady } from "../src/status.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const callerSecret = "test-router-caller-capability-with-sufficient-length";

function runConfig(command, codexHome, stateDir, env = {}) {
  return spawnSync(
    process.execPath,
    [path.join(root, "src", "config-manager.mjs"), command],
    {
      cwd: root,
      encoding: "utf8",
      env: {
        ...process.env,
        CODEX_HOME: codexHome,
        CODEX_ROUTER_STATE_DIR: stateDir,
        MODEL_ROUTER_STATE_DIR: stateDir,
        ...env,
      },
    },
  );
}

function statusFixture(overrides = {}) {
  return {
    health: { ok: true },
    config: { managed: true, catalogConfigured: false, catalogMode: "official" },
    catalog: { readable: false, exact: false },
    gptRoute: { readable: true, ready: true, mode: "official" },
    providers: { selection: { degraded: undefined } },
    ...overrides,
  };
}

test("official catalog 不依赖旧的 8 模型文件或第三方凭据", () => {
  assert.equal(statusIsReady(statusFixture(), false), true);
  assert.equal(
    statusIsReady(
      statusFixture({ gptRoute: { readable: true, ready: false, mode: "wlb" } }),
      false,
    ),
    false,
  );
  assert.equal(
    statusIsReady(
      statusFixture({ gptRoute: { readable: false, ready: false, mode: "unknown" } }),
      false,
    ),
    false,
  );
});

test("merged catalog 模式仍要求精确目录，未知目录不被当作官方目录", () => {
  const catalog = { readable: true, exact: true };
  assert.equal(
    statusIsReady(
      statusFixture({
        config: { managed: true, catalogConfigured: true, catalogMode: "merged" },
        catalog,
      }),
      true,
    ),
    true,
  );
  assert.equal(
    statusIsReady(
      statusFixture({
        config: { managed: true, catalogConfigured: false, catalogMode: "custom" },
        catalog,
      }),
      true,
    ),
    false,
  );
  assert.equal(
    statusIsReady(
      statusFixture({
        config: { managed: true, catalogConfigured: false, catalogMode: "unknown" },
        catalog,
      }),
      true,
    ),
    false,
  );
});

test("enable 删除自己管理的旧目录，不写回 model_catalog_json", () => {
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), "codex-router-official-catalog-"));
  const codexHome = path.join(tempRoot, "codex");
  const stateDir = path.join(codexHome, "codex-router");
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(path.join(stateDir, "caller-secret"), `${callerSecret}\n`);
  const oldCatalog = path.join(stateDir, "merged-models.json");
  writeFileSync(
    path.join(codexHome, "config.toml"),
    [
      "# BEGIN codex-router-managed",
      'openai_base_url = "http://127.0.0.1:4102/v1"',
      `model_catalog_json = ${JSON.stringify(oldCatalog)}`,
      "# END codex-router-managed",
      "",
      "[features]",
      "multi_agent_v2 = { enabled = true }",
      "",
    ].join("\n"),
  );

  try {
    const result = runConfig("enable", codexHome, stateDir);
    assert.equal(result.status, 0, result.stderr);
    const config = readFileSync(path.join(codexHome, "config.toml"), "utf8");
    assert.equal(config.includes("model_catalog_json"), false);
    const snapshot = JSON.parse(result.stdout);
    assert.equal(snapshot.mode, "router");
    assert.equal(snapshot.model_catalog_json, null);
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});

test("enable 保留已有 multi_agent_v2 子表，重复启用与关闭不产生重复定义", () => {
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), "codex-router-multi-agent-table-"));
  const stateDir = path.join(tempRoot, "codex-router");
  mkdirSync(stateDir);
  writeFileSync(path.join(stateDir, "caller-secret"), `${callerSecret}\n`);
  // 固定兼容探测结果，确保回归检查不依赖机器上是否安装 Codex。
  const codexBin = path.join(tempRoot, process.platform === "win32" ? "codex.cmd" : "codex");
  writeFileSync(codexBin, process.platform === "win32"
    ? "@echo Not logged in 1>&2\r\n@exit /b 1\r\n"
    : "#!/bin/sh\necho 'Not logged in' >&2\nexit 1\n", { mode: 0o700 });
  const configPath = path.join(tempRoot, "config.toml");
  try {
    for (const header of ["[features.multi_agent_v2]", '[ "features" . "multi_agent_v2" ] # 用户配置']) {
      const original = `[features]\nshell_tool = true\n[features.context_management]\nenabled = true\n${header}\nenabled = true\nmax_concurrent_threads_per_session = 4\n`;
      writeFileSync(configPath, original);
      for (const command of ["enable", "enable", "disable"]) {
        const result = runConfig(command, tempRoot, stateDir, { CODEX_BIN: codexBin });
        assert.equal(result.status, 0, result.stderr);
        const config = readFileSync(configPath, "utf8");
        assert.equal(config.includes("# BEGIN codex-router-multi-agent-v2-managed"), false);
        assert.equal(config.includes("# BEGIN codex-router-agent-concurrency-managed"), false);
        assert.equal(config.includes(original.trimEnd()), true);
        assert.equal((config.match(/multi_agent_v2/g) || []).length, 1);
      }
    }
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});

test("enable 拒绝覆盖用户自有 model_catalog_json", () => {
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), "codex-router-user-catalog-"));
  const codexHome = path.join(tempRoot, "codex");
  const stateDir = path.join(codexHome, "codex-router");
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(path.join(stateDir, "caller-secret"), `${callerSecret}\n`);
  const configPath = path.join(codexHome, "config.toml");
  const original = [
    `openai_base_url = "http://127.0.0.1:4102/_codex-router/${callerSecret}/v1"`,
    'model_catalog_json = "C:/user-owned/models.json"',
    "",
    "[features]",
    "multi_agent_v2 = { enabled = true }",
    "",
  ].join("\n");
  writeFileSync(configPath, original);

  try {
    const result = runConfig("enable", codexHome, stateDir);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /model_catalog_json/);
    assert.equal(readFileSync(configPath, "utf8"), original);
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});

test("provider 刷新在官方目录模式跳过旧 catalog 重建", () => {
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), "codex-router-target-picker-"));
  const codexHome = path.join(tempRoot, "codex");
  const stateDir = path.join(codexHome, "codex-router");
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(path.join(stateDir, "native-models.json"), "{}\n");
  writeFileSync(path.join(codexHome, "config.toml"), "openai_base_url = \"https://chatgpt.com/backend-api\"\n");
  const moduleUrl = pathToFileURL(path.join(root, "src", "target-integration.mjs")).href;
  const script = `import { refreshTargetPickerIfInstalled } from ${JSON.stringify(moduleUrl)}; console.log(refreshTargetPickerIfInstalled());`;

  try {
    const result = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
      cwd: root,
      encoding: "utf8",
      env: {
        ...process.env,
        CODEX_HOME: codexHome,
        CODEX_ROUTER_STATE_DIR: stateDir,
        MODEL_ROUTER_STATE_DIR: stateDir,
      },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout.trim(), "false");
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});
