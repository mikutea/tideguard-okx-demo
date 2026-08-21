import type {
  AccountData,
  AuditEvent,
  DemoOrder,
  EnvironmentChallenge,
  EnvironmentAcknowledgements,
  EnvironmentTarget,
  EnvironmentPreflight,
  EnvironmentStatus,
  EnvironmentSwitchResult,
  MarketData,
  LongRunStatus,
  MLStatus,
  ResearchMonitorStatus,
  PreviewResult,
  SystemStatus
} from "./types";

let csrfToken = "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body) headers.set("Content-Type", "application/json");
  if (init?.method && init.method !== "GET") headers.set("X-Tideguard-CSRF", csrfToken);
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) message = body.detail;
    } catch {
      // Preserve the status text when a proxy or local guard returned plain text.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export async function getStatus(): Promise<SystemStatus> {
  const status = await request<SystemStatus>("/api/v1/system/status");
  csrfToken = status.csrfToken;
  return status;
}

export const api = {
  getMarket: () => request<MarketData>("/api/v1/market"),
  getAccount: () => request<AccountData>("/api/v1/account"),
  getOrders: () => request<DemoOrder[]>("/api/v1/orders"),
  getAudit: (limit = 50) =>
    request<{ chainValid: boolean; events: AuditEvent[] }>(`/api/v1/audit?limit=${limit}`),
  testConnection: () =>
    request<{ public: boolean; private: boolean; privateReachable: boolean; policyValid: boolean; policyReason: string | null; environment: string }>("/api/v1/connection/test", {
      method: "POST"
    }),
  arm: (confirmation: string) =>
    request<SystemStatus["safety"]>("/api/v1/safety/arm", {
      method: "POST",
      body: JSON.stringify({ confirmation })
    }),
  disarm: () =>
    request<SystemStatus["safety"]>("/api/v1/safety/disarm", { method: "POST" }),
  kill: () =>
    request<{ safety: SystemStatus["safety"]; acceptedCancelRequests: number; remainingAppOrders: number | null; failures: number }>(
      "/api/v1/safety/kill",
      { method: "POST" }
    ),
  resetKill: (confirmation: string) =>
    request<SystemStatus["safety"]>("/api/v1/safety/reset-kill", {
      method: "POST",
      body: JSON.stringify({ confirmation })
    }),
  preview: (order: {
    instId: string;
    side: "buy" | "sell";
    ordType: "limit" | "post_only";
    price: string;
    size: string;
  }) =>
    request<PreviewResult>("/api/v1/intents/preview", {
      method: "POST",
      body: JSON.stringify(order)
    }),
  commit: (preview: PreviewResult, idempotencyKey: string) =>
    request<{ intentId: string; status: string; ordId: string | null; replayed: boolean }>(
      `/api/v1/intents/${preview.intentId}/commit`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({ digest: preview.digest })
      }
    ),
  getMLStatus: () => request<MLStatus>("/api/v1/ml/status"),
  getResearchStatus: () => request<ResearchMonitorStatus>("/api/v1/research/status"),
  trainModel: (candleLimit = 2000) =>
    request<Record<string, unknown>>("/api/v1/ml/train", {
      method: "POST",
      body: JSON.stringify({ candleLimit })
    }),
  promoteModel: (body: {
    modelId: string;
    reviewer: string;
    rationale: string;
    confirmation: string;
    expectedGeneration: number;
  }) =>
    request<NonNullable<MLStatus["champion"]>>("/api/v1/ml/promote", {
      method: "POST",
      body: JSON.stringify(body)
    }),
  authorizeAutomation: (body: {
    issuedBy: string;
    confirmation: string;
    ttlSeconds: number;
    maxOrders: number;
    maxTotalNotionalUsdt: string;
  }) =>
    request<Record<string, unknown>>("/api/v1/ml/automation/authorize", {
      method: "POST",
      body: JSON.stringify(body)
    }),
  stopAutomation: () =>
    request<Record<string, unknown>>("/api/v1/ml/automation/stop", { method: "POST" }),
  getAutonomyStatus: () => request<LongRunStatus>("/api/v1/autonomy/status"),
  trainAutonomy: () =>
    request<Record<string, unknown>>("/api/v1/autonomy/train", { method: "POST" }),
  enableAutonomy: (confirmation: string) =>
    request<LongRunStatus["state"]>("/api/v1/autonomy/master/enable", {
      method: "POST",
      body: JSON.stringify({ mode: "demo", confirmation })
    }),
  disableAutonomy: (reason: string) =>
    request<LongRunStatus["state"]>("/api/v1/autonomy/master/disable", {
      method: "POST",
      body: JSON.stringify({ reason })
    }),
  getEnvironmentStatus: () =>
    request<EnvironmentStatus>("/api/v1/environment/status"),
  preflightEnvironmentSwitch: (target: EnvironmentTarget) =>
    request<EnvironmentPreflight>("/api/v1/environment/preflight", {
      method: "POST",
      body: JSON.stringify({ target })
    }),
  challengeEnvironmentSwitch: (target: EnvironmentTarget) =>
    request<EnvironmentChallenge>("/api/v1/environment/challenge", {
      method: "POST",
      body: JSON.stringify({ target })
    }),
  confirmEnvironmentSwitch: (
    target: EnvironmentTarget,
    nonce: string,
    confirmation: string,
    acknowledgements: EnvironmentAcknowledgements
  ) =>
    request<EnvironmentSwitchResult>("/api/v1/environment/confirm", {
      method: "POST",
      body: JSON.stringify({ target, nonce, confirmation, acknowledgements })
    })
};
