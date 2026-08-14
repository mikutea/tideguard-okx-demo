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

export interface MLModelSummary {
  modelId: string;
  artifactSha256: string;
  state: "candidate" | "validated" | "champion" | "retired" | "rejected";
  trainer: string;
  createdAt: string;
  trainedThrough: string;
  validationRunId: string | null;
  reportSha256: string | null;
  metrics: null | {
    folds: number;
    oosRows: number;
    trades: number;
    accuracy: number;
    netReturn: number;
    maxDrawdown: number;
    worstFoldNetReturn: number;
    roundTripCostBps: number;
    evaluationMode: string;
  };
  gateFailures: string[];
}

export interface MLStatus {
  engine: {
    name: string;
    artifactFormat: string;
    featureContractSha256: string;
    trainingMode: string;
    profitGuarantee: boolean;
  };
  training: { running: boolean; publicDataOnly: boolean };
  models: MLModelSummary[];
  champion: null | {
    modelId: string;
    artifactSha256: string;
    generation: number;
    promotionId: string;
    reviewer: string;
    rationale: string;
    policySha256: string;
    validationRunId: string;
    reportSha256: string;
    approvedAt: string;
  };
  generation: number;
  promotionPolicy: Record<string, string | number> & { confirmation: string };
  automation: {
    confirmation: string;
    demoOnly: boolean;
    instrument: string;
    maxSessionSeconds: number;
    maxSessionOrders: number;
    maxSessionNotionalUsdt: string;
    entryOnly: boolean;
    automaticExit: boolean;
    permit: null | {
      permitId: string;
      modelId: string;
      expiresAt: string;
      maxOrders: number;
      maxTotalNotionalUsdt: string;
      usedOrders: number;
      usedNotionalUsdt: string;
      revokedAt: string | null;
      active: boolean;
      remainingSeconds: number;
    };
    manualReviews: Array<Record<string, string | null>>;
    recentExecutions: Array<Record<string, unknown>>;
    lastDecision: null | Record<string, unknown>;
  };
  freqai: {
    bundled: boolean;
    mode: string;
    directOkxDemoExecution: boolean;
    pinnedReferenceVersion: string;
  };
}
