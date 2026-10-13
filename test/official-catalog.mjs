import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

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

test("enable 支持 features 表与 multi_agent_v2 键的 bare/basic/literal 写法", () => {
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), "codex-router-quoted-features-"));
  const stateDir = path.join(tempRoot, "codex-router");
  mkdirSync(stateDir);
  writeFileSync(path.join(stateDir, "caller-secret"), `${callerSecret}\n`);
  const codexBin = path.join(tempRoot, process.platform === "win32" ? "codex.cmd" : "codex");
  writeFileSync(codexBin, process.platform === "win32"
    ? "@echo Not logged in 1>&2\r\n@exit /b 1\r\n"
    : "#!/bin/sh\necho 'Not logged in' >&2\nexit 1\n", { mode: 0o700 });
  const configPath = path.join(tempRoot, "config.toml");
  try {
    for (const header of ["[features]", '[ "features" ] # 用户配置', "[ 'features' ]"]) {
      for (const key of [null, "multi_agent_v2", '"multi_agent_v2"', "'multi_agent_v2'"]) {
        const original = `${header}\nshell_tool = true\n${key ? `${key} = { enabled = true }\n` : ""}`;
        writeFileSync(configPath, original);
        for (const command of ["enable", "enable", "disable"]) {
          const result = runConfig(command, tempRoot, stateDir, { CODEX_BIN: codexBin });
          assert.equal(result.status, 0, result.stderr);
          const config = readFileSync(configPath, "utf8");
          assert.equal(config.includes(original.trimEnd()), true);
          assert.equal((config.match(/^\s*\[\s*(?:features|"features"|'features')\s*\]/gm) || []).length, 1, `${header}, ${key}, ${command}`);
          assert.equal((config.match(/^\s*(?:multi_agent_v2|"multi_agent_v2"|'multi_agent_v2')\s*=/gm) || []).length,
            key || command !== "disable" ? 1 : 0, `${header}, ${key}, ${command}`);
          assert.equal(config.includes("# BEGIN codex-router-multi-agent-v2-managed"), !key && command !== "disable");
          assert.equal(config.includes("# BEGIN codex-router-agent-concurrency-managed"), false);
        }
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

test("移除 provider 密钥不再重建或修改已有模型目录", () => {
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), "codex-router-retired-catalog-"));
  const stateDir = path.join(tempRoot, "codex-router");
  mkdirSync(stateDir);
  const nativePath = path.join(stateDir, "native-models.json");
  const mergedPath = path.join(stateDir, "merged-models.json");
  const configPath = path.join(tempRoot, "config.toml");
  const keyPath = path.join(stateDir, "wlb-api-key.secret");
  const config = `model_catalog_json = ${JSON.stringify(mergedPath)}\n`;
  // 损坏的旧捕获会让原来的自动重建失败；密钥操作现在必须与目录无关。
  writeFileSync(nativePath, "invalid legacy capture\n");
  writeFileSync(mergedPath, "existing legacy catalog\n");
  writeFileSync(configPath, config);
  writeFileSync(keyPath, "test-retired-catalog-key\n");
  writeFileSync(path.join(stateDir, "enabled-providers.json"), JSON.stringify({
    version: 1, providers: ["mimo-token-plan", "wlb-relay"],
  }));

  try {
    const result = spawnSync(process.execPath, ["src/provider-key.mjs", "wlb-relay", "remove"], {
      cwd: root,
      encoding: "utf8",
      env: {
        ...process.env,
        CODEX_HOME: tempRoot,
        CODEX_ROUTER_STATE_DIR: stateDir,
        MODEL_ROUTER_STATE_DIR: stateDir,
        WLB_API_KEY: "",
      },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Removed 1 managed/);
    assert.doesNotMatch(result.stdout, /refresh the model picker/);
    assert.equal(existsSync(keyPath), false);
    const selection = JSON.parse(readFileSync(path.join(stateDir, "enabled-providers.json"), "utf8"));
    assert.deepEqual(selection.providers, ["mimo-token-plan"]);
    assert.equal(readFileSync(nativePath, "utf8"), "invalid legacy capture\n");
    assert.equal(readFileSync(mergedPath, "utf8"), "existing legacy catalog\n");
    assert.equal(readFileSync(configPath, "utf8"), config);
    assert.equal(existsSync(path.join(stateDir, "announced-models.json")), false);
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});


test("SuperGPT 状态只检查自身选择和持久化凭据，不误用 WLB 状态", () => {
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), "codex-router-supergpt-status-"));
  const script = `
    import { writeFileSync, unlinkSync } from 'node:fs';
    import path from 'node:path';
    import { collectStatus } from './src/status.mjs';
    import { writeProviderCredential, primaryCredentialPath, apiProvider } from './src/provider-credentials.mjs';
    import { STATE_DIR } from './src/paths.mjs';
    writeProviderCredential('supergpt-relay', 'test-supergpt-status-key');
    writeProviderCredential('wlb-relay', 'test-wlb-status-key');
    writeFileSync(path.join(STATE_DIR, 'gpt-route.json'), JSON.stringify({ version: 1, route: 'supergpt' }));
    const routes = [];
    const select = (providers) => writeFileSync(path.join(STATE_DIR, 'enabled-providers.json'), JSON.stringify({ version: 1, providers }));
    select(['supergpt-relay']);
    routes.push((await collectStatus()).gptRoute);
    select(['wlb-relay']);
    routes.push((await collectStatus()).gptRoute);
    select(['wlb-relay', 'supergpt-relay']);
    unlinkSync(primaryCredentialPath(apiProvider('supergpt-relay')));
    routes.push((await collectStatus()).gptRoute);
    console.log(JSON.stringify(routes));
  `;
  try {
    const result = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
      cwd: root, encoding: "utf8", timeout: 30_000,
      env: {
        ...process.env, CODEX_HOME: tempRoot, CODEX_ROUTER_STATE_DIR: tempRoot, MODEL_ROUTER_STATE_DIR: tempRoot,
        SUPERGPT_API_KEY: "", WLB_API_KEY: "", MODEL_ROUTER_PORT: "65534", CODEX_ROUTER_PORT: "65534",
        NODE_USE_ENV_PROXY: "1", NO_PROXY: "127.0.0.1,localhost",
      },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), [
      { readable: true, mode: "supergpt", ready: true },
      { readable: true, mode: "supergpt", ready: false },
      { readable: true, mode: "supergpt", ready: false },
    ]);
  } finally {
    rmSync(tempRoot, { recursive: true, force: true });
  }
});
