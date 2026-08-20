import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { canConfirmEnvironmentChallenge, environmentFromSystemStatus, environmentUiPolicy, resolveEnvironmentMode } from "../src/lib/environment.js";

const liveSystem = {
  environment: "OKX 实盘",
  environmentProfile: { activeEnvironment: "live" }
};

test("structured system status resolves LIVE before the child endpoint returns", () => {
  assert.equal(resolveEnvironmentMode({ lastKnown: "demo", systemStatus: liveSystem, environmentStatus: null }), "live");
});

test("child endpoint failure cannot downgrade a last-known LIVE state", () => {
  assert.equal(resolveEnvironmentMode({ lastKnown: "live", systemStatus: null, environmentStatus: null }), "live");
});

test("a delayed Demo child response cannot hide structured LIVE status", () => {
  assert.equal(resolveEnvironmentMode({
    lastKnown: "demo",
    systemStatus: liveSystem,
    environmentStatus: { activeEnvironment: "demo" }
  }), "live");
});

test("Chinese live display name remains a safe compatibility fallback", () => {
  assert.equal(environmentFromSystemStatus({ environment: "OKX 实盘" }), "live");
});

test("fully unknown state is locked instead of defaulting to Demo", () => {
  const mode = resolveEnvironmentMode({ lastKnown: "unknown", systemStatus: null, environmentStatus: null });
  assert.equal(mode, "unknown");
  assert.equal(environmentUiPolicy(mode).executionLocked, true);
  assert.equal(environmentUiPolicy(mode).manualArmPhrase, "");
});

test("LIVE policy keeps the permanent banner and real-funds phrase", () => {
  const policy = environmentUiPolicy("live");
  assert.equal(policy.showLiveBanner, true);
  assert.equal(policy.manualArmPhrase, "我确认使用真实资金");
  assert.equal(policy.liveAutomationAvailable, false);
});

test("server countdown and exact phrase gate final environment confirmation", () => {
  const base = {
    readyAt: "2026-08-21T00:00:10.000Z",
    expiresAt: "2026-08-21T00:05:00.000Z",
    phrase: "切换到 OKX 实盘",
    expectedPhrase: "切换到 OKX 实盘",
    allAcknowledged: true,
    transitionLocked: false
  };
  assert.equal(canConfirmEnvironmentChallenge({ ...base, now: Date.parse("2026-08-21T00:00:09.999Z") }), false);
  assert.equal(canConfirmEnvironmentChallenge({ ...base, now: Date.parse("2026-08-21T00:00:10.000Z") }), true);
  assert.equal(canConfirmEnvironmentChallenge({ ...base, now: Date.parse("2026-08-21T00:00:11.000Z"), phrase: "wrong" }), false);
  assert.equal(canConfirmEnvironmentChallenge({ ...base, now: Date.parse("2026-08-21T00:00:11.000Z"), transitionLocked: true }), false);
});

test("final switch UI keeps the high-risk modal DOM contract", async () => {
  const source = await readFile(new URL("../src/components/EnvironmentSwitch.tsx", import.meta.url), "utf8");
  assert.match(source, /role="dialog"/);
  assert.match(source, /aria-modal="true"/);
  assert.match(source, /LIVE 实际资金/);
  assert.match(source, /server-countdown/);
  assert.match(source, /canConfirmEnvironmentChallenge/);
  assert.match(source, /onMouseDown=.*cancelIfIdle/);
});

test("kill reset is environment-specific and never auto-arms", async () => {
  const source = await readFile(new URL("../src/pages/ExecutionPage.tsx", import.meta.url), "utf8");
  assert.match(source, /api\.resetKill\(phrase\)/);
  assert.match(source, /解除实盘急停/);
  assert.match(source, /解除模拟盘急停/);
  assert.match(source, /当前仍为 observe，未自动 arm/);
  const resetFlow = source.match(/const resetKill = async[\s\S]*?\n  return /)?.[0] ?? "";
  assert.doesNotMatch(resetFlow, /api\.arm/);
});

test("confirmed environment switch uses a manual two-step restart", async () => {
  const source = await readFile(new URL("../src/components/EnvironmentSwitch.tsx", import.meta.url), "utf8");
  assert.match(source, /开始菜单 → 停止墨衡后台服务/);
  assert.match(source, /重新打开墨衡 MOHENG/);
  assert.match(source, /不会通过 HTTP 自行终止后台/);
});

test("in-flight high-risk requests cannot be dismissed", async () => {
  const environmentSource = await readFile(new URL("../src/components/EnvironmentSwitch.tsx", import.meta.url), "utf8");
  const executionSource = await readFile(new URL("../src/pages/ExecutionPage.tsx", import.meta.url), "utf8");
  for (const source of [environmentSource, executionSource]) {
    assert.match(source, /event\.key === "Escape" && !busy/);
    assert.match(source, /disabled=\{busy\}/);
    assert.match(source, /请求已发送，结果返回前不可撤回/);
    assert.match(source, /cancelIfIdle/);
  }
});

test("connection reachability is never presented as live policy approval", async () => {
  const apiSource = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
  const pageSource = await readFile(new URL("../src/pages/AuditSettingsPage.tsx", import.meta.url), "utf8");
  assert.match(apiSource, /privateReachable: boolean/);
  assert.match(apiSource, /policyValid: boolean/);
  assert.match(pageSource, /账户 API/);
  assert.match(pageSource, /权限策略/);
  assert.match(pageSource, /result\.privateReachable && !result\.policyValid \? "error"/);
});

test("empty research state is not labeled as confirmed history", async () => {
  const chartSource = await readFile(new URL("../src/components/Charts.tsx", import.meta.url), "utf8");
  const dataSource = await readFile(new URL("../src/pages/DataPage.tsx", import.meta.url), "utf8");
  assert.match(chartSource, /dataset\.confirmedRows > 0/);
  assert.match(chartSource, /等待首次回填/);
  assert.match(dataSource, /等待首次全量回填/);
});
