import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import { HistoricalReplayConsole } from "../src/components/HistoricalReplayConsole";
import type { HistoricalReplayStatus } from "../src/types";

const V6_SCHEMA_VERSION = "moheng.historical-replay-report.v4";
const V5_SCHEMA_VERSION = "moheng.historical-replay-report.v3";

const checkpoints = [
  {
    at: "2026-01-01T00:00:00.000Z",
    cash: 10_000,
    drawdown: 0,
    equity: 10_000,
    positionInstrument: null,
    positionMarketValue: 0,
  },
  {
    at: "2026-01-01T00:05:00.000Z",
    cash: 5_000,
    drawdown: 0,
    equity: 10_100,
    positionInstrument: "BTC-USDT",
    positionMarketValue: 5_100,
  },
  {
    at: "2026-01-01T00:10:00.000Z",
    cash: 10_200,
    drawdown: 0,
    equity: 10_200,
    positionInstrument: null,
    positionMarketValue: 0,
  },
];

const episodes = [
  {
    assetRows: 1_000,
    availableAt: "2025-12-31T23:55:00.000Z",
    calibratedBrier: 0.18,
    calibrationRows: 100,
    calibrationStartAt: "2025-12-20T00:00:00.000Z",
    calibrationStopAt: "2025-12-30T23:55:00.000Z",
    episode: 0,
    episodeId: "episode-v6-000000000001",
    fitRows: 900,
    fitStartAt: "2025-01-01T00:00:00.000Z",
    fitStopAt: "2025-12-19T23:55:00.000Z",
    labelCompleteAt: "2025-12-31T23:55:00.000Z",
    rawBrier: 0.2,
    replayRows: 1,
    replayStartAt: checkpoints[0].at,
    replayStopAt: checkpoints[1].at,
    trainingSeconds: 1.2,
  },
  {
    assetRows: 1_100,
    availableAt: "2026-01-01T00:05:00.000Z",
    calibratedBrier: 0.17,
    calibrationRows: 110,
    calibrationStartAt: "2025-12-21T00:00:00.000Z",
    calibrationStopAt: "2025-12-31T23:55:00.000Z",
    episode: 1,
    episodeId: "episode-v6-000000000002",
    fitRows: 990,
    fitStartAt: "2025-01-02T00:00:00.000Z",
    fitStopAt: "2025-12-20T23:55:00.000Z",
    labelCompleteAt: "2026-01-01T00:05:00.000Z",
    rawBrier: 0.19,
    replayRows: 1,
    replayStartAt: checkpoints[2].at,
    replayStopAt: checkpoints[2].at,
    trainingSeconds: 1.1,
  },
];

const v6Replay = {
  blockers: ["shadow_evidence_missing"],
  calibrationImproved: true,
  capacityHandling: "clip",
  cashBarRate: 0.1,
  checkpoints,
  chosenPolicy: {
    edgeBufferBps: 12,
    minEntrySpacingBars: 6,
    requiredGrossReturnBps: 36,
  },
  cohortId: "cohort-v6",
  completedAt: "2026-01-01T00:10:00.000Z",
  compressionMultiple: 2,
  decision: "research_only",
  developmentGatePassed: true,
  developmentHistoryAlreadyObserved: true,
  episodeCount: 2,
  episodes,
  executionSlice: {
    developmentGatePassed: false,
    failures: ["execution_slice_trades_insufficient"],
    instrument: "BTC-USDT",
    maxDrawdown: 0.01,
    netReturn: 0.017,
    stressNetReturn: 0.003,
    trades: 14,
  },
  executionSemantics: "corrected_next_open_boundary",
  family: "logistic_regression",
  finalCash: 10_200,
  firstReplayAt: checkpoints[0].at,
  lastReplayAt: checkpoints[2].at,
  maxDrawdown: 0.01,
  netReturn: 0.02,
  independentVerificationRequired: true,
  monitorContractValid: true,
  ordersClipped: 2,
  ordersRejected: 0,
  ordinaryCostBps: 24,
  promotable: false,
  replayId: "replay-v6-browser-contract",
  reportSha256: "f".repeat(64),
  retiredSemanticMismatch: false,
  retrainEveryDays: 30,
  schemaVersion: V6_SCHEMA_VERSION,
  shadowDaysCredited: 0,
  selectionBiasWarning: true,
  simulatedDays: 365,
  startingCash: 10_000,
  stressNetReturn: 0.01,
  totalEstimatedSlippageCost: 12,
  targetExecutionAligned: true,
  totalFees: 24,
  totalWallSeconds: 2.3,
  tradeCount: 24,
  trades: [],
  tradesPerDay: 0.07,
  turnoverMultiple: 1.2,
  valid: true,
} as HistoricalReplayStatus;

function requireElement<T extends Element>(selector: string, root: ParentNode = document): T {
  const element = root.querySelector<T>(selector);
  if (!element) throw new Error(`Missing rendered element: ${selector}`);
  return element;
}

function requireButton(label: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find((candidate) => candidate.textContent?.includes(label));
  if (!button) throw new Error(`Missing rendered button: ${label}`);
  return button;
}

function expectText(value: string): void {
  if (!document.body.textContent?.includes(value)) {
    throw new Error(`Missing rendered text: ${value}`);
  }
}

function rejectText(value: string): void {
  if (document.body.textContent?.includes(value)) {
    throw new Error(`Unexpected rendered text: ${value}`);
  }
}

function renderReplay(root: ReturnType<typeof createRoot>, replay: HistoricalReplayStatus): void {
  flushSync(() => {
    root.render(<HistoricalReplayConsole replay={replay} />);
  });
}

async function click(button: HTMLButtonElement): Promise<void> {
  button.click();
  await Promise.resolve();
  await Promise.resolve();
}

async function run(): Promise<void> {
  const mount = requireElement<HTMLElement>("#root");
  const result = requireElement<HTMLOutputElement>("#browser-test-result");
  const root = createRoot(mount);

  try {
    renderReplay(root, v6Replay);

    expectText("CAUSAL REPLAY LAB · V6");
    expectText("共同时间边界 · 零额外延迟");
    expectText("V6 契约通过");
    expectText("必需 · 尚不能据此上线");
    expectText("否 · 当前为 V6");
    expectText("历史结果仍不是前瞻收益或成交证明");
    rejectText("V5 · 已退役");
    result.dataset.v6Mapped = "true";
    result.dataset.semanticFields = "true";

    const range = requireElement<HTMLInputElement>('input[aria-label="历史回放进度"]');
    const rangeValue = () => String(range.value);
    if (rangeValue() !== "0") throw new Error(`Expected initial cursor 0, got ${rangeValue()}`);
    expectText("$10,000.00");

    await click(requireButton("开始回放"));
    expectText("暂停回放");
    await click(requireButton("暂停回放"));
    expectText("开始回放");

    await click(requireElement<HTMLButtonElement>('button[aria-label="前进一步"]'));
    if (rangeValue() !== "1") throw new Error(`Expected stepped cursor 1, got ${rangeValue()}`);
    expectText("$10,100.00");
    expectText("50%");

    const fastest = requireButton("32×");
    await click(fastest);
    if (fastest.getAttribute("aria-pressed") !== "true") {
      throw new Error("32× playback speed did not become pressed");
    }

    await click(requireElement<HTMLButtonElement>('button[aria-label="跳转到第 2 代"]'));
    if (rangeValue() !== "2") throw new Error(`Expected episode cursor 2, got ${rangeValue()}`);
    expectText("第 2 代");
    expectText("$10,200.00");
    expectText("重新回放");
    result.dataset.playbackControls = "true";

    const v5Replay = {
      ...v6Replay,
      executionSemantics: "legacy_or_pre_v6",
      monitorContractValid: false,
      replayId: "replay-v5-retired-browser-contract",
      retiredSemanticMismatch: true,
      schemaVersion: V5_SCHEMA_VERSION,
      valid: false,
    } as HistoricalReplayStatus;
    renderReplay(root, v5Replay);

    expectText("回放证据完整性校验失败");
    expectText("该报告采用已退役的 V5 或更早执行语义");
    if (document.querySelector(".replay-workbench")) {
      throw new Error("Invalid retired V5 report rendered the replay workbench");
    }
    rejectText("开始回放");
    result.dataset.v5RetiredInvalid = "true";

    result.dataset.status = "passed";
    result.value = "HistoricalReplayConsole real-browser contract passed";
  } catch (error) {
    result.dataset.status = "failed";
    result.value = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    throw error;
  } finally {
    flushSync(() => {
      root.unmount();
    });
  }
}

await run();
