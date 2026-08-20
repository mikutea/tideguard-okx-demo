export type SafetyMode = "observe" | "armed" | "killed";

export interface SystemStatus {
  app: string;
  version: string;
  environment: string;
  environmentProfile?: EnvironmentStatus;
  demoHeader: string | null;
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
    ordType: "limit" | "post_only" | "ioc";
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

export type EnvironmentMode = "demo" | "live" | "unknown";
export type EnvironmentTarget = Exclude<EnvironmentMode, "unknown">;

export interface EnvironmentStatus {
  activeEnvironment: EnvironmentTarget;
  activeDisplayName: string;
  configuredEnvironment: EnvironmentTarget;
  restartRequired: boolean;
  transitionPending?: boolean;
  transitionTarget?: EnvironmentTarget | null;
  selectorValid: boolean;
  selectorError: string | null;
  switchId: string | null;
  updatedAt: string | null;
  operatingMode: "observe" | "runtime" | "transition_locked";
  credentialConfigured?: boolean;
  credentialService?: string;
  orderTag?: string;
  simulatedTradingHeader?: boolean;
  liveManualTradingAvailable?: boolean;
  liveAutomationAvailable?: boolean;
  independentLiveAuthorizationRequired?: boolean;
}

export interface EnvironmentPreflightCheck {
  key: string;
  label?: string;
  passed: boolean;
  detail?: string | null;
}

export interface EnvironmentPreflight {
  source: EnvironmentTarget;
  target: EnvironmentTarget;
  allowed: boolean;
  checks: EnvironmentPreflightCheck[];
  preflightSha256: string;
  binding?: {
    currentCredentialFingerprint: string | null;
    currentAccountFingerprint: string | null;
    targetCredentialFingerprint: string | null;
    targetAccountFingerprint: string | null;
  };
}

export interface EnvironmentChallenge {
  nonce: string;
  source: EnvironmentTarget;
  target: EnvironmentTarget;
  confirmationPhrase: string;
  issuedAt: string;
  readyAt: string;
  expiresAt: string;
  requiredAcknowledgements: Array<keyof EnvironmentAcknowledgements>;
  preflight: EnvironmentPreflight;
}

export interface EnvironmentAcknowledgements {
  automationStopped: boolean;
  noOutstandingState: boolean;
  restartRequired: boolean;
  liveFundsAtRisk: boolean;
}

export interface EnvironmentSwitchResult extends EnvironmentStatus {
  source: EnvironmentTarget;
  target: EnvironmentTarget;
  killActive: boolean;
  targetKillActive: boolean;
}

export interface FoldMetric {
  fold: number;
  startAt: string | null;
  endAt: string | null;
  netReturn: number | null;
  maxDrawdown: number | null;
  trades: number | null;
  status: "pass" | "fail" | "unknown";
}

export interface ResearchStatus {
  dataset?: {
    source: string;
    instrument: string;
    bar: string;
    confirmedRows: number;
    targetRows: number | null;
    earliestAt: string | null;
    latestAt: string | null;
    gaps: number | null;
    conflicts: number | null;
    snapshotSha256: string | null;
    lastErrorType: string | null;
    syncState: "idle" | "running" | "complete" | "failed";
    updatedAt: string | null;
  };
  training?: {
    stage: "idle" | "syncing" | "snapshotting" | "feature_build" | "walk_forward" | "final_fit" | "registering" | "completed" | "failed";
    progress: number | null;
    cpuPercent: number | null;
    memoryMb: number | null;
    elapsedSeconds: number | null;
    nextRunAt: string | null;
  };
  walkForward?: Record<string, FoldMetric[]>;
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
  longRun: LongRunStatus;
}

export interface LongRunPosition {
  positionId: string;
  modelId: string;
  championGeneration: number;
  status: "entry_submitted" | "entry_unfilled" | "long" | "exit_submitted" | "closed" | "closed_dust" | "manual_review";
  requestedSize: string;
  filledSize: string;
  remainingSize: string;
  entryAvgPrice: string | null;
  entryCandleAt: string;
  exitDueAt: string;
  hardExitAt: string;
  stopPrice: string | null;
  takeProfitPrice: string | null;
  exitAttempts: number;
  realizedReturn: number | null;
  failureReason: string | null;
  createdAt: string;
  updatedAt: string;
  closedAt: string | null;
}

export interface SupervisorModelReview {
  modelId: string;
  artifactSha256: string;
  trainingConfigSha256: string | null;
  comparisonBaselineModelId: string | null;
  comparisonBaselineCohort: string | null;
  state: MLModelSummary["state"];
  trainer: string;
  createdAt: string;
  trainedThrough: string;
  deterministicFailures: string[];
  shadowFailures: string[];
  comparisonFailures: string[];
  metrics: null | {
    aggregateAccuracy: number;
    benchmarkCohortId: string | null;
    evaluationDatasetSha256: string;
    evaluationMode: string;
    folds: number;
    maxDrawdown: number;
    marketSnapshotSha256: string | null;
    netReturn: number;
    oosRows: number;
    reportSha256: string;
    roundTripCostBps: number;
    splitProtocolSha256: string | null;
    testFrom: string;
    testThrough: string;
    trades: number;
    worstFoldNetReturn: number;
  };
  shadow: {
    settledSignals: number;
    settledBuys: number;
    netReturn: number;
    maxDrawdown: number;
    durationDays: number;
    firstSignalAt: string | null;
    lastSettledAt: string | null;
  };
}

export interface LongRunStatus {
  schemaVersion: string;
  state: {
    desiredMode: "disabled" | "shadow" | "demo";
    runtimeStatus: "disabled" | "shadow" | "waiting_supervisor" | "waiting_champion" | "running" | "exit_only" | "suspended" | "manual_review";
    identityBound: boolean;
    suspendedReason: string | null;
    stateVersion: number;
    enabledAt: string | null;
    updatedAt: string;
  };
  activePosition: LongRunPosition | null;
  latestTraining: null | {
    runId: string;
    startedAt: string;
    completedAt: string | null;
    status: "running" | "completed" | "failed";
    modelId: string | null;
    errorType: string | null;
    phase: "syncing" | "snapshotting" | "feature_build" | "walk_forward" | "final_fit" | "registering" | "completed" | "failed";
    progressCurrent: number;
    progressTotal: number | null;
    snapshotId: string | null;
    dataRows: number | null;
    result: Record<string, unknown> | null;
  };
  dataWarehouse: {
    backfillComplete: boolean;
    bar: string;
    coverageDays: number;
    cursorTsMs: number | null;
    diskBytes: number;
    expectedRows: number;
    firstOpenAt: string | null;
    firstOpenTsMs: number | null;
    instrument: string;
    lastErrorType: string | null;
    lastClosedAt: string | null;
    lastOpenAt: string | null;
    lastOpenTsMs: number | null;
    lastSyncAt: string | null;
    latestSnapshot: null | {
      schemaVersion: string;
      source: string;
      instrument: string;
      bar: string;
      firstOpenAt: string;
      lastOpenAt: string;
      rowCount: number;
      snapshotId: string;
      contentSha256: string;
      featureContractSha256: string;
      createdAt: string;
    };
    missingBars: number;
    pagesFetched: number;
    rowsInserted: number;
    schemaVersion: string;
    source: string;
    storedRows: number;
    syncStatus: "idle" | "syncing" | "partial" | "error";
    unresolvedConflicts: number;
  };
  recentDecisions: Array<{
    decisionId: string;
    kind: "promote" | "lease" | "reject" | "rollback" | "suspend";
    modelId: string | null;
    generation: number;
    issuedAt: string;
    expiresAt: string;
    appliedAt: string | null;
  }>;
  recentPositions: LongRunPosition[];
  demoPerformance: {
    closedPositions: number;
    netReturn: number;
    maxDrawdown: number;
    lastClosedAt: string | null;
  };
  policy: {
    policySha256: string;
    fixed_notional_usdt: string;
    max_daily_entries: number;
    hold_bars: number;
    max_holding_bars: number;
    stop_loss_fraction: string;
    take_profit_fraction: string;
    ioc_slippage_fraction: string;
    round_trip_cost_bps: string;
    max_exit_attempts: number;
    train_interval_hours: number;
    training_retry_hours: number;
    supervisor_lease_hours: number;
    shadow_min_settled: number;
    shadow_min_days: number;
    max_demo_drawdown: string;
    min_challenger_oos_improvement: string;
    max_challenger_drawdown_regression: string;
  };
  champion: MLStatus["champion"];
  activeSupervisorLease: null | {
    decisionId: string;
    modelId: string;
    generation: number;
    issuedAt: string;
    expiresAt: string;
    evidenceSha256: string;
    appliedAt: string;
  };
  review: {
    schemaVersion: string;
    evidenceSha256: string;
    generatedAt: string;
    auditChainValid: boolean;
    championSupervisorApproved: boolean;
    demoPerformance: LongRunStatus["demoPerformance"];
    generation: number;
    models: SupervisorModelReview[];
  };
  lastError: null | {
    at: string;
    errorType: string;
    failClosed: boolean;
  };
}
