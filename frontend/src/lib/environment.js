export function environmentFromSystemStatus(status) {
  const structured = status?.environmentProfile?.activeEnvironment;
  if (structured === "demo" || structured === "live") return structured;
  const display = String(status?.environment ?? "").trim().toLowerCase();
  if (display.includes("实盘") || display.includes("live")) return "live";
  if (display.includes("模拟") || display.includes("demo")) return "demo";
  return null;
}

export function resolveEnvironmentMode({ lastKnown, systemStatus, environmentStatus }) {
  const child = environmentStatus?.activeEnvironment;
  const system = environmentFromSystemStatus(systemStatus);
  if (child === "live" || system === "live") return "live";
  if (system === "demo") return "demo";
  if (child === "demo") return "demo";
  if (system) return system;
  if (lastKnown === "demo" || lastKnown === "live") return lastKnown;
  return "unknown";
}

export function environmentUiPolicy(mode) {
  return {
    showLiveBanner: mode === "live",
    executionLocked: mode === "unknown",
    manualArmPhrase: mode === "live" ? "我确认使用真实资金" : mode === "demo" ? "DEMO" : "",
    liveAutomationAvailable: false
  };
}

export function canConfirmEnvironmentChallenge({ now, readyAt, expiresAt, phrase, expectedPhrase, allAcknowledged, transitionLocked }) {
  const current = Number(now);
  const ready = new Date(readyAt).getTime();
  const expires = new Date(expiresAt).getTime();
  return Number.isFinite(current)
    && Number.isFinite(ready)
    && Number.isFinite(expires)
    && current >= ready
    && current < expires
    && phrase === expectedPhrase
    && allAcknowledged === true
    && transitionLocked !== true;
}
