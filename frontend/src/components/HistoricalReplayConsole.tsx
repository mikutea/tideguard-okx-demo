import {
  BrainCircuit,
  ChevronRight,
  CircleDashed,
  FastForward,
  LockKeyhole,
  Pause,
  Play,
  RotateCcw,
  ShieldCheck,
  SkipForward,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { formatNumber, formatPercent, formatTime, shortId } from "../lib/format";
import type { HistoricalReplayStatus } from "../types";

const SPEEDS = [
  { delay: 650, label: "1×" },
  { delay: 150, label: "8×" },
  { delay: 42, label: "32×" },
] as const;

const executionSliceFailureLabels: Record<string, string> = {
  execution_slice_trades_insufficient: "BTC 闭环样本少于 20 笔",
  execution_slice_net_return_not_positive: "BTC 常规成本净收益未转正",
  execution_slice_stress_return_not_positive: "BTC 压力成本净收益未转正",
  execution_slice_drawdown_above_gate: "BTC 最大回撤超过 10%",
};

function pathFor(values: number[], width: number, height: number, min: number, max: number): string {
  if (values.length < 2) return "";
  const span = max - min || 1;
  return values.map((value, index) => {
    const x = (index / (values.length - 1)) * width;
    const y = height - ((value - min) / span) * height;
    return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
}

function money(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : `${value < 0 ? "−" : ""}$${formatNumber(Math.abs(value), 2)}`;
}

function finite(value: number | null | undefined, fallback = 0): number {
  return value === null || value === undefined || !Number.isFinite(value) ? fallback : value;
}

function replayDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function drawdownPercent(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : `${Math.abs(value * 100).toFixed(2)}%`;
}

function ReplayEmpty({ invalid = false }: { invalid?: boolean }) {
  return <section className={`workspace-panel replay-console replay-empty ${invalid ? "invalid" : ""}`} aria-labelledby="replay-title">
    <div className="panel-heading"><div><h2 id="replay-title">历史高速回放训练场</h2><p>把冻结历史按时间顺序喂给周期模型，观察成本后的虚拟资金轨迹</p></div><LockKeyhole size={20} /></div>
    <div className="visual-empty tall"><CircleDashed /><div><strong>{invalid ? "回放证据完整性校验失败" : "等待第一份历史回放证据"}</strong><span>{invalid ? "哈希或安全边界不符合契约，界面拒绝展示结果。" : "运行研究回放后，这里会出现训练周期、虚拟资金和逐日播放控件。"}</span></div></div>
  </section>;
}

export function HistoricalReplayConsole({ replay }: { replay: HistoricalReplayStatus | null | undefined }) {
  const checkpoints = replay?.checkpoints ?? [];
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speedIndex, setSpeedIndex] = useState(1);

  useEffect(() => {
    setCursor(0);
    setPlaying(false);
  }, [replay?.replayId]);

  useEffect(() => {
    if (!playing || checkpoints.length < 2) return;
    const timer = window.setInterval(() => {
      setCursor((current) => Math.min(current + 1, checkpoints.length - 1));
    }, SPEEDS[speedIndex].delay);
    return () => window.clearInterval(timer);
  }, [checkpoints.length, playing, speedIndex]);

  useEffect(() => {
    if (playing && cursor >= checkpoints.length - 1) setPlaying(false);
  }, [checkpoints.length, cursor, playing]);

  const geometry = useMemo(() => {
    if (checkpoints.length < 2) return null;
    const values = checkpoints.map((item) => finite(item.equity));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const full = pathFor(values, 860, 240, min, max);
    const progressValues = values.slice(0, Math.min(cursor + 1, values.length));
    const progressWidth = values.length > 1 ? (Math.max(0, progressValues.length - 1) / (values.length - 1)) * 860 : 0;
    const progress = progressValues.length > 1
      ? pathFor(progressValues, progressWidth, 240, min, max)
      : "";
    const current = values[Math.min(cursor, values.length - 1)];
    return {
      current,
      currentX: progressWidth,
      currentY: 240 - ((current - min) / (max - min || 1)) * 240,
      full,
      max,
      min,
      progress,
    };
  }, [checkpoints, cursor]);

  if (!replay) return <ReplayEmpty />;
  if (!replay.valid) return <ReplayEmpty invalid />;

  const checkpoint = checkpoints[Math.min(cursor, Math.max(0, checkpoints.length - 1))];
  const checkpointTime = checkpoint ? new Date(checkpoint.at).getTime() : Number.NaN;
  const activeEpisode = replay.episodes.find((episode) => {
    const start = new Date(episode.replayStartAt ?? "").getTime();
    const stop = new Date(episode.replayStopAt ?? "").getTime();
    return Number.isFinite(checkpointTime) && checkpointTime >= start && checkpointTime <= stop;
  }) ?? replay.episodes.at(-1);
  const progress = checkpoints.length > 1 ? cursor / (checkpoints.length - 1) : 0;
  const currentEquity = checkpoint?.equity ?? replay.startingCash;
  const currentReturn = replay.startingCash && currentEquity !== null && currentEquity !== undefined
    ? currentEquity / replay.startingCash - 1
    : null;
  const playbackComplete = cursor >= checkpoints.length - 1;
  const calibratedDelta = activeEpisode?.rawBrier !== null
    && activeEpisode?.rawBrier !== undefined
    && activeEpisode.calibratedBrier !== null
    && activeEpisode.calibratedBrier !== undefined
    ? activeEpisode.rawBrier - activeEpisode.calibratedBrier
    : null;
  const executionSlice = replay.executionSlice;

  const selectEpisode = (startAt: string | null) => {
    if (!startAt) return;
    const target = new Date(startAt).getTime();
    const index = checkpoints.findIndex((item) => new Date(item.at).getTime() >= target);
    setCursor(index >= 0 ? index : Math.max(0, checkpoints.length - 1));
    setPlaying(false);
  };

  return <section className="workspace-panel replay-console" aria-labelledby="replay-title">
    <div className="replay-titlebar">
      <div className="replay-title-copy"><span className="replay-kicker"><BrainCircuit size={15} />CAUSAL REPLAY LAB · {replay.schemaVersion?.endsWith(".v3") ? "V5" : "V4"}</span><h2 id="replay-title">执行对齐历史回放训练场</h2><p>365 天滚动训练、末 30 天隔离校准；标签严格对应下一根开盘成交与 12 根后退出。V5 额外隔离显示 BTC-USDT 可执行切片，播放器不触发训练、私有 API 或订单。</p></div>
      <div className="replay-safety-stamp" aria-label="历史回放安全边界"><ShieldCheck size={19} /><div><strong>研究隔离</strong><span>0 Shadow 天 · 0 下单能力</span></div></div>
    </div>

    <div className="replay-kpi-strip">
      <div><span>模拟跨度</span><strong>{formatNumber(replay.simulatedDays, 0)} 天</strong><small>{replayDate(replay.firstReplayAt)} — {replayDate(replay.lastReplayAt)}</small></div>
      <div><span>周期更迭</span><strong>{replay.episodeCount} 代</strong><small>每 {formatNumber(replay.retrainEveryDays, 0)} 天重训</small></div>
      <div><span>多币组合历史收益</span><strong className={finite(replay.netReturn) < 0 ? "negative" : "positive"}>{formatPercent(replay.netReturn)}</strong><small>{formatNumber(replay.ordinaryCostBps, 0)} bps 往返 · 研究口径</small></div>
      <div><span>BTC 可执行切片</span><strong className={finite(executionSlice?.netReturn) < 0 ? "negative" : "positive"}>{formatPercent(executionSlice?.netReturn)}</strong><small>{executionSlice ? `${executionSlice.trades} 笔 · 压力 ${formatPercent(executionSlice.stressNetReturn)}` : "等待 V5 证据"}</small></div>
      <div><span>组合压力成本收益</span><strong className={finite(replay.stressNetReturn) < 0 ? "negative" : "positive"}>{formatPercent(replay.stressNetReturn)}</strong><small>48 bps 往返</small></div>
      <div><span>组合最大回撤</span><strong>{drawdownPercent(replay.maxDrawdown)}</strong><small>{replay.tradeCount} 笔闭环 · 非未来承诺</small></div>
    </div>

    <div className="replay-workbench">
      <div className="replay-chart-column">
        <div className="replay-live-readout">
          <div><span>回放时钟</span><strong>{formatTime(checkpoint?.at)}</strong></div>
          <div><span>虚拟权益</span><strong>{money(currentEquity)}</strong></div>
          <div><span>截至此刻</span><strong className={finite(currentReturn) < 0 ? "negative" : "positive"}>{formatPercent(currentReturn)}</strong></div>
          <div><span>当前仓位</span><strong>{checkpoint?.positionInstrument ?? "现金"}</strong></div>
        </div>
        <div className="replay-chart" role="img" aria-label={`历史回放虚拟权益，当前 ${money(currentEquity)}，进度 ${(progress * 100).toFixed(0)}%`}>
          {geometry ? <svg viewBox="0 0 860 286">
            {[0, 60, 120, 180, 240].map((y) => <line key={y} x1="0" x2="860" y1={y} y2={y} className="chart-grid" />)}
            <path d={geometry.full} className="replay-line-muted" />
            <path d={geometry.progress} className="replay-line-progress" />
            <line x1={geometry.currentX} x2={geometry.currentX} y1="0" y2="240" className="replay-cursor-line" />
            <circle cx={geometry.currentX} cy={geometry.currentY} r="5" className="replay-cursor-dot" />
            <text x="0" y="275" className="chart-axis-label">{formatTime(checkpoints[0]?.at)}</text>
            <text x="860" y="275" textAnchor="end" className="chart-axis-label">{formatTime(checkpoints.at(-1)?.at)}</text>
            <text x="855" y="16" textAnchor="end" className="chart-axis-label">{money(geometry.max)}</text>
            <text x="855" y="235" textAnchor="end" className="chart-axis-label">{money(geometry.min)}</text>
          </svg> : <div className="visual-empty"><CircleDashed /><span>至少需要两个逐日权益检查点</span></div>}
        </div>
        <div className="replay-scrubber">
          <input aria-label="历史回放进度" type="range" min={0} max={Math.max(0, checkpoints.length - 1)} value={Math.min(cursor, Math.max(0, checkpoints.length - 1))} disabled={checkpoints.length < 2} onChange={(event) => { setCursor(Number(event.target.value)); setPlaying(false); }} />
          <span>{Math.round(progress * 100)}%</span>
        </div>
        <div className="replay-controls" aria-label="历史回放控制">
          <button className="icon-button" aria-label="回到开头" onClick={() => { setCursor(0); setPlaying(false); }} disabled={cursor === 0}><RotateCcw size={17} /></button>
          <button className="button primary replay-play" onClick={() => { if (playbackComplete) setCursor(0); setPlaying((value) => !value); }} disabled={checkpoints.length < 2}>{playing ? <Pause size={17} /> : <Play size={17} />}{playing ? "暂停回放" : playbackComplete ? "重新回放" : "开始回放"}</button>
          <button className="icon-button" aria-label="前进一步" onClick={() => { setCursor((value) => Math.min(value + 1, checkpoints.length - 1)); setPlaying(false); }} disabled={playbackComplete}><SkipForward size={17} /></button>
          <div className="replay-speed"><FastForward size={16} /><span>速度</span>{SPEEDS.map((speed, index) => <button className={speedIndex === index ? "active" : ""} key={speed.label} onClick={() => setSpeedIndex(index)} aria-pressed={speedIndex === index}>{speed.label}</button>)}</div>
        </div>
      </div>

      <aside className="replay-inspector" aria-label="当前回放证据">
        <div className="replay-inspector-head"><span>当前模型周期</span><strong>{activeEpisode ? `第 ${activeEpisode.episode + 1} 代` : "—"}</strong></div>
        <div className="replay-cycle-diagram">
          <div className="fit"><span>01</span><strong>滚动拟合</strong><small>{formatNumber(activeEpisode?.fitRows, 0)} 行</small></div><ChevronRight size={16} />
          <div className="cal"><span>02</span><strong>隔离校准</strong><small>{formatNumber(activeEpisode?.calibrationRows, 0)} 行</small></div><ChevronRight size={16} />
          <div className="play"><span>03</span><strong>执行对齐回放</strong><small>{formatNumber(activeEpisode?.replayRows, 0)} 行</small></div>
        </div>
        <dl className="replay-evidence-list">
          <div><dt>模型族</dt><dd>{replay.family ?? "—"}</dd></div>
          <div><dt>周期 ID</dt><dd><code>{shortId(activeEpisode?.episodeId, 14)}</code></dd></div>
          <div><dt>模型可用</dt><dd>{formatTime(activeEpisode?.availableAt)}</dd></div>
          <div><dt>标签与成交对齐</dt><dd className={replay.targetExecutionAligned ? "positive" : "negative"}>{replay.targetExecutionAligned ? "已验证" : "未验证"}</dd></div>
          <div><dt>容量处理</dt><dd>{replay.capacityHandling === "clip" ? `缩量成交 · ${replay.ordersClipped} 笔` : replay.capacityHandling ?? "—"}</dd></div>
          <div><dt>本代训练耗时</dt><dd>{formatNumber(activeEpisode?.trainingSeconds, 2)} 秒</dd></div>
          <div><dt>校准改善</dt><dd className={calibratedDelta === null ? undefined : calibratedDelta >= 0 ? "positive" : "negative"}>{calibratedDelta === null ? "—" : `${calibratedDelta >= 0 ? "+" : ""}${calibratedDelta.toFixed(4)}`}</dd></div>
          <div><dt>此刻回撤</dt><dd>{drawdownPercent(checkpoint?.drawdown)}</dd></div>
        </dl>
        <div className={`replay-gate ${replay.developmentGatePassed ? "passed" : "blocked"}`}><LockKeyhole size={18} /><div><strong>{replay.developmentGatePassed ? "历史开发门槛通过，仍不可晋级" : "历史开发门槛未通过"}</strong><span>{replay.selectionBiasWarning ? "本段历史已用于诊断，正收益可能偏乐观；" : ""}历史回放永远不累计 Shadow 天数，也不修改 BTC-USDT 执行白名单。</span></div></div>
        {executionSlice ? <div className={`replay-gate ${executionSlice.developmentGatePassed ? "passed" : "blocked"}`}><LockKeyhole size={18} /><div><strong>{executionSlice.developmentGatePassed ? "BTC 可执行切片通过开发门" : "BTC 可执行切片仍未达门"}</strong><span>{executionSlice.developmentGatePassed ? "仍需前瞻 Shadow 与 Demo 闭环。" : executionSlice.failures.map((failure) => executionSliceFailureLabels[failure] ?? failure).join("；")}</span></div></div> : null}
      </aside>
    </div>

    <div className="replay-generation-rail" aria-label="周期模型时间轴">
      <div className="replay-rail-label"><span>模型更迭轨</span><strong>{replay.episodes.length} 个冻结周期</strong></div>
      <div className="replay-rail-scroll">{replay.episodes.map((episode) => <button key={episode.episodeId ?? episode.episode} className={episode.episode === activeEpisode?.episode ? "active" : ""} onClick={() => selectEpisode(episode.replayStartAt)} aria-label={`跳转到第 ${episode.episode + 1} 代`}><span>{String(episode.episode + 1).padStart(2, "0")}</span><i /></button>)}</div>
    </div>
  </section>;
}
