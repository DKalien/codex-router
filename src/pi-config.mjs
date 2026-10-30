import { existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  assertCallerSecret,
  callerPiBaseUrl,
  isManagedCallerPiBaseUrl,
  redactCallerUrl,
} from "./caller-auth.mjs";
import { protectPrivateFile } from "./file-security.mjs";
import { CALLER_SECRET_PATH, PORTS } from "./paths.mjs";

// Pi 的内置 OpenAI provider 只需要把 baseUrl 指向本地路由器的 /pi/v1
// 命名空间；模型列表、登录凭据和 thinking 设置全部保持 Pi 自己的实现。
// 这里不写 Pi 的 auth.json、不做 OAuth，也绝不打印任何凭据。

export function piAgentDir() {
  return (
    process.env.PI_CODING_AGENT_DIR?.trim() || path.join(os.homedir(), ".pi", "agent")
  );
}

export function piModelsPath() {
  return path.join(piAgentDir(), "models.json");
}

function managedPiBaseUrl() {
  if (!existsSync(CALLER_SECRET_PATH)) {
    throw new Error(
      "The local router caller key is missing; run the status command, then reinstall to repair it.",
    );
  }
  const secret = assertCallerSecret(readFileSync(CALLER_SECRET_PATH, "utf8").trim());
  return callerPiBaseUrl(PORTS.router, secret);
}

function readModelsFile() {
  const file = piModelsPath();
  if (!existsSync(file)) return {};
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(file, "utf8"));
  } catch (error) {
    throw new Error(
      `无法解析 ${file}：${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`无法解析 ${file}：顶层必须是 JSON 对象。`);
  }
  return parsed;
}

function writeModelsFile(value) {
  const file = piModelsPath();
  mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = `${file}.tmp.${process.pid}`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  try {
    // models.json 同时承载用户自己的 provider 密钥与带 secret 的本地
    // router URL，写入后立即收紧到仅当前用户可读。
    protectPrivateFile(temporary);
    renameSync(temporary, file);
    protectPrivateFile(file);
  } catch (error) {
    if (existsSync(temporary)) unlinkSync(temporary);
    throw error;
  }
}

function deleteModelsFileIfEmpty(value) {
  if (Object.keys(value).length > 0) return false;
  try {
    unlinkSync(piModelsPath());
    return true;
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    return false;
  }
}

export function enablePiConfig() {
  const baseUrl = managedPiBaseUrl();
  const models = readModelsFile();
  const providers =
    models.providers === undefined ? {} : models.providers;
  if (!providers || typeof providers !== "object" || Array.isArray(providers)) {
    throw new Error("models.json 的 providers 必须是 JSON 对象。");
  }
  const openai = providers.openai === undefined ? {} : providers.openai;
  if (!openai || typeof openai !== "object" || Array.isArray(openai)) {
    throw new Error("models.json 的 providers.openai 必须是 JSON 对象。");
  }
  const existing = openai.baseUrl;
  if (
    typeof existing === "string" &&
    existing &&
    !isManagedCallerPiBaseUrl(existing, PORTS.router)
  ) {
    throw new Error(
      `Refusing to replace user-owned openai baseUrl: ${redactCallerUrl(existing)}`,
    );
  }
  openai.baseUrl = baseUrl;
  providers.openai = openai;
  models.providers = providers;
  writeModelsFile(models);
  return { path: piModelsPath(), baseUrl: redactCallerUrl(baseUrl), changed: existing !== baseUrl };
}

export function disablePiConfig() {
  const file = piModelsPath();
  if (!existsSync(file)) return { path: file, changed: false, managed: false };
  const models = readModelsFile();
  const openai = models.providers?.openai;
  const existing = openai?.baseUrl;
  if (typeof existing !== "string" || !isManagedCallerPiBaseUrl(existing, PORTS.router)) {
    return { path: file, changed: false, managed: false };
  }
  delete openai.baseUrl;
  if (Object.keys(openai).length === 0) {
    delete models.providers.openai;
    if (Object.keys(models.providers).length === 0) delete models.providers;
  }
  if (deleteModelsFileIfEmpty(models)) return { path: file, changed: true, managed: true };
  writeModelsFile(models);
  return { path: file, changed: true, managed: true };
}

export function piConfigStatus() {
  const file = piModelsPath();
  const base = { configured: false, managed: false, path: file, agentDir: piAgentDir(), baseUrl: null };
  if (!existsSync(file)) return base;
  let models;
  try {
    models = readModelsFile();
  } catch (error) {
    return { ...base, configured: true, error: error instanceof Error ? error.message : String(error) };
  }
  const existing = models.providers?.openai?.baseUrl;
  if (typeof existing !== "string" || !existing) return base;
  return {
    ...base,
    configured: true,
    managed: isManagedCallerPiBaseUrl(existing, PORTS.router),
    baseUrl: redactCallerUrl(existing),
  };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2).filter((argument) => argument !== "--json");
  const command = args[0] || "status";
  const jsonOutput = process.argv.slice(2).includes("--json");
  const invalidArguments =
    args.length > 1 || !new Set(["enable", "disable", "status"]).has(command);
  if (invalidArguments) {
    console.error("用法：pi-config.mjs enable|disable|status [--json]");
    process.exitCode = 2;
  } else {
    try {
      if (command === "enable") {
        const result = enablePiConfig();
        if (jsonOutput) process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        else console.log(`Pi models.json 已指向路由器：${result.baseUrl}`);
      } else if (command === "disable") {
        const result = disablePiConfig();
        if (jsonOutput) process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        else console.log(result.changed ? "Pi models.json 已恢复默认 OpenAI 端点。" : "Pi models.json 未由本路由器管理，保持不变。");
      } else {
        const status = piConfigStatus();
        if (jsonOutput) process.stdout.write(`${JSON.stringify(status, null, 2)}\n`);
        else {
          console.log(
            `Pi models.json：${status.managed ? "由本路由器管理" : status.configured ? "存在用户自有配置" : "未配置"}` +
              (status.baseUrl ? ` (${status.baseUrl})` : ""),
          );
          if (status.error) console.log(`  ${status.error}`);
        }
      }
    } catch (error) {
      console.error(error instanceof Error ? error.message : String(error));
      process.exitCode = 1;
    }
  }
}
