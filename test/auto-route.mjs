import assert from "node:assert/strict";
import test from "node:test";
import { createAutoRouteController, parseGptUsage, parseWlbUsage } from "../src/auto-route.mjs";

const headers = { authorization: "Bearer official-test", "chatgpt-account-id": "account-a", "x-secret": "never-forward" };
const wlb = (daily = 10, weekly = 100) => ({ rate_limits: [{ window: "1d", remaining: daily }, { window: "7d", remaining: weekly }] });
const gpt = (five = 50, weekly = 50) => ({ rate_limits: { ...(five === undefined ? {} : { primary: { used_percent: five } }), ...(weekly === undefined ? {} : { secondary: { used_percent: weekly } }) } });
const json = (value) => new Response(JSON.stringify(value));
function fixture(options = {}) {
  let time = 0;
  let wlbValue = wlb();
  let gptValue = gpt();
  let failure = "";
  const calls = [];
  const controller = createAutoRouteController({
    getWlbConnection: () => ({ baseUrl: options.baseUrl ?? "https://wlb.test/v1/", credential: "wlb-test" }),
    now: () => time,
    ttlMs: 100,
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      const name = url.includes("wlb.test") ? "wlb" : "gpt";
      if (failure === name || failure === "both") throw new Error("sensitive-response-token");
      return json(name === "wlb" ? wlbValue : gptValue);
    },
    ...options,
  });
  return { controller, calls, set: (w, g) => { wlbValue = w; gptValue = g; }, tick: () => { time += 100; }, fail: (name) => { failure = name; } };
}

test("GPT 5h阈值等号和原始浮点边界，周实际0仍禁用", () => {
  for (const [used, expected] of [[95, false], [94.999999, true], [95.000001, false]]) assert.equal(parseGptUsage(gpt(used)).available, expected);
  assert.equal(parseGptUsage(gpt(1, 100)).available, false);
  assert.equal(parseGptUsage(gpt(1, 97.9)).available, true);
});

test("无5h时使用周2%阈值，5h-only有效", () => {
  for (const [used, expected] of [[98, false], [97.999999, true], [98.000001, false]]) {
    assert.equal(parseGptUsage({ rate_limits: { secondary: { used_percent: used } } }).available, expected);
  }
  assert.equal(parseGptUsage({ rate_limits: { primary: { used_percent: 94 } } }).available, true);
});

test("GPT对象/列表/旧协议/data封装，primary周时长归类", () => {
  const cases = [
    { rate_limits: [{ window: "7d", usedPercent: 97 }] },
    { rate_limit: { primary_window: { limit_window_seconds: 604800, used_percent: 97 } } },
    { rate_limits: { primary: { window_minutes: 10080, used_percent: 97 } } },
    { data: { rate_limits: [{ type: "weekly", used_percent: 97 }] } },
  ];
  for (const value of cases) {
    const parsed = parseGptUsage(value);
    assert.equal(parsed.available, true);
    assert.equal(parsed.fiveHour.status, "missing");
    assert.equal(parsed.weekly.remainingPercent, 3);
  }
});

test("WLB原始日/周剩余额度独立耗尽，不采用UI百分比", () => {
  assert.equal(parseWlbUsage(wlb(0, 10)).available, false);
  assert.equal(parseWlbUsage(wlb(10, 0)).available, false);
  assert.equal(parseWlbUsage(wlb(0.000001, 1)).available, true);
  const parsed = parseWlbUsage({ data: { rate_limits: { "1d": { used: 999, limit: 1000 }, "7d": { remaining: 2, remaining_percent: 0 } } } });
  assert.equal(parsed.daily.remaining, 1);
  assert.equal(parsed.available, true);
  assert.equal(parseWlbUsage({ rate_limits: [{ window: "7d", remaining: 2 }] }).available, true);
});

test("null、NaN、空串和非法百分比不误当耗尽或放行", () => {
  for (const value of [null, NaN, Infinity, "", "abc", true, -1, 101]) {
    assert.equal(parseGptUsage({ rate_limits: { primary: { used_percent: value }, secondary: { used_percent: 99 } } }).available, false);
  }
  for (const value of [null, NaN, "", true]) assert.equal(parseWlbUsage(wlb(value)).available, null);
  assert.equal(parseGptUsage({}).available, null);
  assert.equal(parseWlbUsage({}).available, null);
  assert.equal(parseGptUsage({ rate_limits: { primary: null, secondary: { used_percent: 99 } } }).fiveHour.status, "missing");
});

test("WLB→官方→SuperGPT，恢复时升回最高档", async () => {
  const f = fixture();
  assert.equal((await f.controller.resolve(headers)).route, "wlb");
  f.set(wlb(0), gpt()); f.tick();
  assert.equal((await f.controller.resolve(headers)).route, "official");
  f.set(wlb(0), gpt(95)); f.tick();
  assert.equal((await f.controller.resolve(headers)).route, "supergpt");
  f.set(wlb(1), gpt()); f.tick();
  assert.equal((await f.controller.resolve(headers)).route, "wlb");
});

test("TTL缓存、刷新并发复用和官方账户/auth隔离", async () => {
  const f = fixture();
  await Promise.all(Array.from({ length: 8 }, () => f.controller.resolve(headers)));
  assert.equal(f.calls.length, 2);
  await f.controller.resolve(headers); assert.equal(f.calls.length, 2);
  f.tick(); await f.controller.resolve(headers); assert.equal(f.calls.length, 4);
  await f.controller.resolve({ ...headers, "chatgpt-account-id": "account-b" }); assert.equal(f.calls.length, 6);
  await f.controller.resolve({ ...headers, authorization: "Bearer another" }); assert.equal(f.calls.length, 8);
});

test("缓存最多保留16个身份", async () => {
  const f = fixture();
  for (let i = 0; i < 17; i++) await f.controller.resolve({ authorization: `Bearer user-${i}` });
  await f.controller.resolve({ authorization: "Bearer user-0" });
  assert.equal(f.calls.length, 36);
});

test("已确认WLB可用不受GPT查询失败影响，错误不含原响应", async () => {
  const f = fixture(); f.fail("gpt");
  const result = await f.controller.resolve(headers);
  assert.equal(result.route, "wlb"); assert.equal(result.degraded, true);
  assert.ok(!JSON.stringify(result).includes("sensitive"));
});

test("未知额度首次不切付费；暂时失败保留已有route", async () => {
  const f = fixture(); f.fail("wlb");
  await assert.rejects(f.controller.resolve(headers), { status: 503, type: "auto_route_unavailable" });
  f.fail(""); f.tick(); await f.controller.resolve(headers);
  f.fail("both"); f.tick();
  assert.equal((await f.controller.resolve(headers)).route, "wlb");
  const invalid = fixture(); invalid.set(wlb(0), {});
  await assert.rejects(invalid.controller.resolve(headers), { status: 503 });
});

test("明确排除缓存防止滞后额度立即切回，下次成功刷新解除", async () => {
  const f = fixture();
  await f.controller.resolve(headers);
  assert.equal((await f.controller.resolve(headers, { exclude: ["wlb"] })).route, "official");
  assert.equal((await f.controller.resolve(headers)).route, "official");
  f.fail("wlb"); f.tick();
  assert.equal((await f.controller.resolve(headers)).route, "official");
  f.fail(""); f.tick();
  assert.equal((await f.controller.resolve(headers)).route, "wlb");
  await assert.rejects(f.controller.resolve(headers, { exclude: ["wlb", "official", "supergpt"] }), { status: 503 });
});

test("首次exclude和刷新中exclude不被同次滞后结果覆盖", async () => {
  const f = fixture();
  assert.equal((await f.controller.resolve(headers, { exclude: ["wlb"] })).route, "official");
  assert.equal((await f.controller.resolve(headers)).route, "official");
});

test("消费者取消及时返回且不取消其他消费者共享刷新", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const signals = [];
  const f = fixture({ fetchImpl: async (url, init) => { signals.push(init.signal); await gate; return json(url.includes("wlb") ? wlb() : gpt()); } });
  const cancel = new AbortController();
  const one = f.controller.resolve(headers, { signal: cancel.signal });
  const two = f.controller.resolve(headers);
  cancel.abort();
  await assert.rejects(one, { name: "AbortError" });
  assert.ok(signals.every((signal) => !signal.aborted));
  release(); assert.equal((await two).route, "wlb");
  await assert.rejects(f.controller.resolve(headers, { signal: cancel.signal }), { name: "AbortError" });
});

test("GET端点规范化及身份隔离，禁止redirect", async () => {
  for (const baseUrl of ["https://wlb.test", "https://wlb.test/v1", "https://wlb.test/v1/usage/"]) {
    const f = fixture({ baseUrl }); await f.controller.resolve(headers);
    assert.equal(f.calls[0].url, "https://wlb.test/v1/usage");
    assert.deepEqual(f.calls[0].init.headers, { authorization: "Bearer wlb-test" });
    assert.deepEqual(f.calls[1].init.headers, { authorization: headers.authorization, "chatgpt-account-id": "account-a", "User-Agent": "Mozilla/5.0 Codex-Usage/1.0", Originator: "codex-cli" });
    assert.equal(f.calls[1].url, "https://chatgpt.com/backend-api/wham/usage");
    for (const { init } of f.calls) { assert.equal(init.method, "GET"); assert.equal(init.redirect, "error"); }
  }
});

test("HTTP失败、超大响应、超时均停止可靠判定且不泄露内容", async () => {
  for (const fetchImpl of [
    async () => new Response("secret", { status: 302 }),
    async () => new Response(" ".repeat(65536) + "{}"),
    async (_url, { signal }) => new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("secret")), { once: true })),
  ]) {
    const f = fixture({ fetchImpl, timeoutMs: 15 });
    await assert.rejects(f.controller.resolve(headers), (error) => error.status === 503 && !error.message.includes("secret"));
  }
});

test("有界读取取消超限stream且不接受截断边界有效JSON", async () => {
  let cancelled = 0;
  const f = fixture({ fetchImpl: async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode(JSON.stringify(wlb()) + " ".repeat(70000))); },
    cancel() { cancelled++; },
  })) });
  await assert.rejects(f.controller.resolve(headers), { status: 503 });
  assert.equal(cancelled, 2);
});

test("三档排除持续至下一次成功刷新，不永久屏蔽终点", async () => {
  const f = fixture(); await f.controller.resolve(headers);
  await assert.rejects(f.controller.resolve(headers, { exclude: ["wlb", "official", "supergpt"] }), { status: 503 });
  await assert.rejects(f.controller.resolve(headers), { status: 503 });
  f.fail("both"); f.tick();
  await assert.rejects(f.controller.resolve(headers), { status: 503 });
  f.fail(""); f.set(wlb(0), gpt(99)); f.tick();
  assert.equal((await f.controller.resolve(headers)).route, "supergpt");
});

test("刷新进行期间新报告的排除不会被较早发出的刷新清除", async () => {
  let release;
  let delayed = false;
  const f = fixture({ fetchImpl: async (url) => {
    if (delayed) await new Promise((resolve) => { const previous = release; release = () => { previous?.(); resolve(); }; });
    return json(url.includes("wlb") ? wlb() : gpt());
  } });
  await f.controller.resolve(headers);
  delayed = true; f.tick();
  const first = f.controller.resolve(headers);
  const second = f.controller.resolve(headers, { exclude: ["wlb"] });
  release();
  assert.equal((await first).route, "official");
  assert.equal((await second).route, "official");
  assert.equal((await f.controller.resolve(headers)).route, "official");
});

test("null窗口表示不存在，null百分比仍属非法，primary周窗口可搭配null secondary", () => {
  const weekly = parseGptUsage({ rate_limit: { primary_window: { limit_window_seconds: 604800, used_percent: 97 }, secondary_window: null } });
  assert.equal(weekly.weekly.remainingPercent, 3);
  assert.equal(weekly.fiveHour.status, "missing");
  assert.equal(weekly.available, true);
  assert.equal(parseGptUsage({ rate_limits: { primary: null, secondary: { used_percent: 97 } } }).available, true);
  assert.equal(parseGptUsage({ rate_limits: { primary: { used_percent: null }, secondary: { used_percent: 97 } } }).available, null);
});

test("双窗口同时监控，任一窗口低于阈值都跳过，必须同时恢复", () => {
  assert.equal(parseGptUsage(gpt(50, 98)).available, false);
  assert.equal(parseGptUsage(gpt(50, 97.999999)).available, true);
  assert.equal(parseGptUsage(gpt(50, 98.000001)).available, false);
  assert.equal(parseGptUsage(gpt(95, 50)).available, false);
  assert.equal(parseGptUsage(gpt(94.999999, 50)).available, true);
  assert.equal(parseGptUsage(gpt(95, 98)).available, false);
  assert.equal(parseGptUsage(gpt(94, 98)).available, false);
  assert.equal(parseGptUsage(gpt(94, 1)).available, true);
  assert.equal(parseGptUsage(gpt(94, 3)).available, true);
});
