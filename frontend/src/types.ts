export type SafetyMode = "observe" | "armed" | "killed";

export interface SystemStatus {
  app: string;
  version: string;
  environment: string;
  demoHeader: string;
  baseUrl: string;
  bind: string;
  credentialConfigured: boolean;
  credentialStore: string;
  safety: {
    mode: SafetyMode;
    armedRemainingSeconds: number;
    killActive: boolean;
    armedUntil: string | null;
  };
  auditChainValid: boolean;
  csrfToken: string;
  policy: {
    version: string;
    maxOrderNotionalUsdt: string;
    maxOrderEquityFraction: string;
    maxPriceDeviation: string;
    maxOpenOrders: number;
    staleMarketSeconds: number;
  };
}

export interface Candle {
  ts: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  confirmed: boolean;
}

export interface MarketData {
  source: string;
  environment: string;
  instrument: {
    instId: string;
    instType: string;
    state: string;
    tickSize: string;
    lotSize: string;
    minSize: string;
  };
  ticker: {
    last: string;
    open24h: string;
    high24h: string;
    low24h: string;
    volume24h: string;
    volumeCcy24h: string;
    bid: string;
    ask: string;
    ts: string;
  };
  candles: Candle[];
}

export interface AccountData {
  configured: boolean;
  source: string;
  equityUsdt: string | null;
  updatedAt?: string;
  balances: Array<{
    currency: string;
    available: string;
    cashBalance: string;
    equity: string;
  }>;
}

export interface DemoOrder {
  ordId: string;
  clOrdId: string;
  instId: string;
  side: "buy" | "sell";
  ordType: string;
  price: string;
  size: string;
  filledSize: string;
  state: string;
  createdAt: string;
  updatedAt: string;
  source: string;
}

export interface AuditEvent {
  id: number;
  utcTime: string;
  actor: string;
  eventType: string;
  correlationId: string | null;
  payload: Record<string, unknown>;
  eventHash: string;
}

export interface RiskCheck {
  key: string;
  label: string;
  passed: boolean;
  current: string;
  limit: string;
  reason: string;
}

export interface PreviewResult {
  intentId: string;
  digest: string;
  expiresAt: string;
  order: {
    instId: string;
    side: "buy" | "sell";
    ordType: "limit" | "post_only";
    price: string;
    size: string;
  };
  notionalUsdt: string;
  decision: {
    allowed: boolean;
    policyVersion: string;
    checks: RiskCheck[];
    reasonCodes: string[];
  };
}
