import { ArrowRight, Check, CircleDashed, LockKeyhole, ShieldCheck, X } from "lucide-react";
import { useMemo } from "react";
import { buildPerformanceSeries, formatPercent, formatTime, shortId } from "../lib/format";
import { failureLabel, modelFailures } from "../lib/research";
import type { LongRunStatus, MarketData, ResearchStatus, SupervisorModelReview } from "../types";

function pathFor(values: number[], width: number, height: number, min: number, max: number): string {
  if (values.length < 2) return "";
  const span = max - min || 1;
  return values.map((value, index) => {
    const x = (index / (values.length - 1)) * width;
    const y = height - ((value - min) / span) * height;
    return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
}

export function PerformanceChart({ market, longRun }: { market: MarketData | null; longRun: LongRunStatus | null }) {
  const series = useMemo(
    () => buildPerformanceSeries(market, longRun?.recentPositions ?? []),
    [market, longRun?.recentPositions]
  );
  const geometry = useMemo(() => {
    if (series.length < 2) return null;
    const values = series.flatMap((point) => [point.strategy, point.benchmark]);
    const min = Math.min(...values, 0);
    const max = Math.max(...values, 0);
    return {
      min,
      max,
      strategy: pathFor(series.map((point) => point.strategy), 760, 230, min, max),
      benchmark: pathFor(series.map((point) => point.benchmark), 760, 230, min, max),
      zeroY: 230 - ((0 - min) / (max - min || 1)) * 230
    };
  }, [series]);

  const latest = series.at(-1);
  return (
    <section className="workspace-panel performance-panel" aria-labelledby="performance-title">
      <div className="panel-heading">
        <div>
          <h2 id="performance-title">模型净值与市场基准</h2>
          <p>可见 5m 行情窗口；模型线仅累计本程序已闭环仓位的扣费结果</p>
        </div>
        <div className="chart-direct-labels" aria-label="图例">
          <span className="legend-strategy">模型 {latest ? `${latest.strategy >= 0 ? "+" : ""}${latest.strategy.toFixed(2)}%` : "—"}</span>
          <span className="legend-benchmark">BTC 基准 {latest ? `${latest.benchmark >= 0 ? "+" : ""}${latest.benchmark.toFixed(2)}%` : "—"}</span>
        </div>
      </div>
      {geometry ? (
        <div className="performance-canvas">
          <svg viewBox="0 0 760 270" role="img" aria-label={`模型净值 ${latest?.strategy.toFixed(2)}%，BTC 同窗基准 ${latest?.benchmark.toFixed(2)}%`}>
            {[0, 57.5, 115, 172.5, 230].map((y) => <line key={y} x1="0" x2="760" y1={y} y2={y} className="chart-grid" />)}
            <line x1="0" x2="760" y1={geometry.zeroY} y2={geometry.zeroY} className="chart-zero" />
            <path d={geometry.benchmark} className="line-benchmark" />
            <path d={geometry.strategy} className="line-strategy" />
            <text x="0" y="260" className="chart-axis-label">{formatTime(series[0].ts)}</text>
            <text x="760" y="260" textAnchor="end" className="chart-axis-label">{formatTime(series.at(-1)?.ts)}</text>
            <text x="756" y="16" textAnchor="end" className="chart-axis-label">{geometry.max.toFixed(2)}%</text>
            <text x="756" y="226" textAnchor="end" className="chart-axis-label">{geometry.min.toFixed(2)}%</text>
          </svg>
          {longRun?.demoPerformance.closedPositions === 0 ? (
            <div className="chart-empty-note"><CircleDashed size={18} />尚无模型闭环；零线不是收益预测</div>
          ) : null}
        </div>
      ) : <div className="visual-empty"><CircleDashed /><span>等待公共行情形成可比较窗口</span></div>}
      <div className="performance-summary">
        <div><span>闭环仓位</span><strong>{longRun?.demoPerformance.closedPositions ?? 0}</strong></div>
        <div><span>累计净收益</span><strong className={(longRun?.demoPerformance.netReturn ?? 0) < 0 ? "negative" : "positive"}>{formatPercent(longRun?.demoPerformance.netReturn)}</strong></div>
        <div><span>最大回撤</span><strong>{formatPercent(longRun?.demoPerformance.maxDrawdown)}</strong></div>
        <div><span>统计口径</span><strong>实际成交 · 含费用</strong></div>
      </div>
    </section>
  );
}

export function DrawdownBand({ longRun }: { longRun: LongRunStatus | null }) {
  const positions = (longRun?.recentPositions ?? []).filter((item) => item.realizedReturn !== null);
  let equity = 1;
  let peak = 1;
  const values = positions.map((position) => {
    equity *= 1 + (position.realizedReturn ?? 0);
    peak = Math.max(peak, equity);
    return ((equity / peak) - 1) * 100;
  });
  const bars = values.length ? values : [0, 0, 0, 0, 0, 0, 0, 0];
  const floor = Math.min(...bars, -0.01);
  return (
    <div className="drawdown-band" role="img" aria-label={`最近闭环最大回撤 ${formatPercent(longRun?.demoPerformance.maxDrawdown)}`}>
      <div className="drawdown-label"><span>闭环回撤路径</span><strong>{formatPercent(longRun?.demoPerformance.maxDrawdown)}</strong></div>
      <div className="drawdown-bars">{bars.map((value, index) => (
        <span key={index} style={{ height: `${Math.max(5, (Math.abs(value) / Math.abs(floor)) * 100)}%` }} />
      ))}</div>
    </div>
  );
}

type PipelineStatus = "done" | "active" | "blocked" | "waiting";

export function PipelineMap({ longRun, research }: { longRun: LongRunStatus | null; research: ResearchStatus }) {
  const models = longRun?.review.models ?? [];
  const hasValidated = models.some((model) => model.state === "validated" || model.state === "champion");
  const stages: Array<{ label: string; detail: string; status: PipelineStatus }> = [
    { label: "历史回填", detail: `${research.dataset?.confirmedRows.toLocaleString("zh-CN") ?? "—"} 根已确认`, status: research.dataset?.syncState === "running" ? "active" : research.dataset?.confirmedRows ? "done" : "waiting" },
    { label: "特征与训练", detail: `${models.length} 个候选`, status: longRun?.latestTraining?.status === "running" ? "active" : models.length ? "done" : "waiting" },
    { label: "Walk-forward", detail: "时间隔离 · 扣成本", status: hasValidated ? "done" : models.length ? "blocked" : "waiting" },
    { label: "未来 Shadow", detail: `${Math.max(0, ...models.map((model) => model.shadow.settledBuys), 0)} 次已结算 BUY`, status: hasValidated ? "active" : models.length ? "blocked" : "waiting" },
    { label: "监督与执行", detail: longRun?.activeSupervisorLease ? "lease 有效" : "等待证据通过", status: longRun?.activeSupervisorLease ? "done" : longRun?.champion ? "active" : "blocked" }
  ];
  return (
    <section className="workspace-panel pipeline-panel" aria-labelledby="pipeline-title">
      <div className="panel-heading"><div><h2 id="pipeline-title">AI 决策流水线</h2><p>每一步都有证据门，任何失败都会阻止后续执行</p></div></div>
      <div className="pipeline-map">
        {stages.map((stage, index) => (
          <div className={`pipeline-stage ${stage.status}`} key={stage.label}>
            <div className="pipeline-index">{stage.status === "done" ? <Check size={16} /> : stage.status === "blocked" ? <LockKeyhole size={15} /> : String(index + 1).padStart(2, "0")}</div>
            <div><strong>{stage.label}</strong><span>{stage.detail}</span></div>
            {index < stages.length - 1 ? <ArrowRight className="pipeline-arrow" size={18} aria-hidden="true" /> : null}
          </div>
        ))}
      </div>
    </section>
  );
}

export function CoverageTimeline({ research }: { research: ResearchStatus }) {
  const dataset = research.dataset;
  const hasConfirmedHistory = Boolean(
    dataset
    && dataset.confirmedRows > 0
    && dataset.syncState === "complete"
    && dataset.snapshotSha256
  );
  const progress = dataset?.targetRows && dataset.targetRows > 0
    ? Math.min(1, dataset.confirmedRows / dataset.targetRows)
    : null;
  return (
    <section className="workspace-panel coverage-panel" aria-labelledby="coverage-title">
      <div className="panel-heading"><div><h2 id="coverage-title">历史覆盖与回填</h2><p>完整历史先经过连续性检查，再生成不可变训练快照</p></div><span className={`source-state ${dataset?.syncState ?? "idle"}`}>{!dataset ? "待遥测" : dataset.syncState === "running" ? "回填中" : dataset.syncState === "failed" ? "失败" : hasConfirmedHistory ? "已确认" : "等待首次回填"}</span></div>
      <div className={`coverage-track ${progress === null ? "indeterminate" : ""}`} role="progressbar" {...(progress === null ? {} : { "aria-valuenow": Math.round(progress * 100) })} aria-valuemin={0} aria-valuemax={100} aria-label={progress === null ? "历史回填进行中，交易所最早边界尚未知" : "历史回填进度"}>
        <span style={progress === null ? undefined : { width: `${Math.max(2, progress * 100)}%` }} />
      </div>
      <div className="coverage-axis"><span>{formatTime(dataset?.earliestAt)}</span><strong>{dataset ? `${dataset.confirmedRows.toLocaleString("zh-CN")} 根完成 K 线` : "等待仓库遥测"}</strong><span>{formatTime(dataset?.latestAt)}</span></div>
      <div className="coverage-facts">
        <div><span>来源</span><strong>{dataset?.source ?? "—"}</strong></div>
        <div><span>品种 / 周期</span><strong>{dataset?.instrument ?? "—"} / {dataset?.bar ?? "—"}</strong></div>
        <div><span>缺口</span><strong>{dataset?.gaps ?? "未上报"}</strong></div>
        <div><span>未解决冲突</span><strong>{dataset?.conflicts ?? "未上报"}</strong></div>
      </div>
    </section>
  );
}

export function DataLineage({ research }: { research: ResearchStatus }) {
  const dataset = research.dataset;
  const nodes = [
    { title: "OKX 公共历史", detail: `${dataset?.instrument ?? "BTC-USDT"} · ${dataset?.bar ?? "5m"}`, status: "source" },
    { title: "连续性与确认", detail: dataset?.gaps === null || dataset?.gaps === undefined ? "等待缺口遥测" : `${dataset.gaps} 个缺口`, status: "guard" },
    { title: "不可变数据快照", detail: dataset?.snapshotSha256 ? shortId(dataset.snapshotSha256) : "哈希待上报", status: "artifact" },
    { title: "训练 / OOS", detail: "时间隔离 · 非重叠", status: "model" },
    { title: "未来 Shadow", detail: "不下单 · 扣成本", status: "shadow" }
  ];
  return (
    <section className="workspace-panel lineage-panel" aria-labelledby="lineage-title">
      <div className="panel-heading"><div><h2 id="lineage-title">数据谱系</h2><p>从公共 K 线到监督证据，不跨越凭证与交易边界</p></div></div>
      <ol className="data-lineage">
        {nodes.map((node, index) => <li key={node.title} className={node.status}><span>{index + 1}</span><div><strong>{node.title}</strong><small>{node.detail}</small></div>{index < nodes.length - 1 ? <ArrowRight aria-hidden="true" /> : null}</li>)}
      </ol>
    </section>
  );
}

function matrixTone(model: SupervisorModelReview): "pass" | "fail" | "unknown" {
  if (!model.metrics) return "unknown";
  return modelFailures(model).length === 0 ? "pass" : "fail";
}

export function WalkForwardMatrix({ models, research }: { models: SupervisorModelReview[]; research: ResearchStatus }) {
  const shown = models.slice(0, 8);
  const foldCount = Math.max(0, ...shown.map((model) => model.metrics?.folds ?? 0));
  const folds = Array.from({ length: foldCount }, (_, index) => index + 1);
  const gridStyle = foldCount > 0 ? { gridTemplateColumns: `minmax(160px, 1.5fr) repeat(${foldCount}, 74px) 94px 74px` } : undefined;
  return (
    <section className="workspace-panel matrix-panel" aria-labelledby="matrix-title">
      <div className="panel-heading"><div><h2 id="matrix-title">Walk-forward 时间折矩阵</h2><p>逐折数据缺失时保持灰色；不会用聚合值伪造单折结果</p></div><div className="matrix-key"><span className="pass">通过</span><span className="fail">未通过</span><span className="unknown">未上报</span></div></div>
      <div className="matrix-scroll">
        {foldCount > 0 ? <div className="matrix-grid matrix-header" style={gridStyle}><span>候选</span>{folds.map((fold) => <span key={fold}>F{fold}</span>)}<span>聚合</span><span>交易</span></div> : null}
        {shown.map((model) => {
          const foldData = research.walkForward?.[model.modelId] ?? [];
          const tone = matrixTone(model);
          return <button className="matrix-grid matrix-row" style={gridStyle} key={model.modelId} type="button" aria-label={`${model.modelId} walk-forward 结果`}>
            <strong>{shortId(model.modelId, 10)}</strong>
            {folds.map((fold) => {
              const metric = foldData.find((item) => item.fold === fold);
              return <span className={`matrix-cell ${metric?.status ?? "unknown"}`} key={fold} title={metric?.netReturn === null || metric?.netReturn === undefined ? "逐折值未上报" : `净收益 ${formatPercent(metric.netReturn)}`}>{metric?.netReturn === null || metric?.netReturn === undefined ? "·" : `${(metric.netReturn * 100).toFixed(1)}`}</span>;
            })}
            <span className={`matrix-cell ${tone}`}>{formatPercent(model.metrics?.netReturn, 1)}</span>
            <span>{model.metrics?.trades ?? "—"}</span>
          </button>;
        })}
        {shown.length === 0 ? <div className="visual-empty"><CircleDashed /><span>尚无 walk-forward 报告</span></div> : null}
      </div>
    </section>
  );
}

export function ModelLineage({ longRun }: { longRun: LongRunStatus | null }) {
  const models = longRun?.review.models ?? [];
  const recent = models.slice(0, 6).reverse();
  return (
    <section className="workspace-panel model-lineage-panel" aria-labelledby="model-lineage-title">
      <div className="panel-heading"><div><h2 id="model-lineage-title">模型谱系与监督状态</h2><p>内容寻址模型只能沿证据链晋级；拒绝不会覆盖历史</p></div></div>
      <div className="model-lineage">
        <div className="lineage-root"><ShieldCheck size={19} /><span>代次 {longRun?.review.generation ?? 0}</span><strong>{longRun?.champion ? shortId(longRun.champion.modelId, 12) : "尚无 champion"}</strong></div>
        <div className="lineage-branches">
          {recent.map((model) => <div className={`lineage-node ${model.state}`} key={model.modelId}><span>{model.state === "rejected" ? <X size={14} /> : <Check size={14} />}{model.state}</span><strong>{shortId(model.modelId, 10)}</strong><small>{model.metrics?.trades ?? 0} 笔 · {formatPercent(model.metrics?.netReturn, 1)}</small></div>)}
          {recent.length === 0 ? <div className="visual-empty compact"><CircleDashed /><span>等待首批候选</span></div> : null}
        </div>
      </div>
    </section>
  );
}

export function FailureSummary({ model }: { model: SupervisorModelReview }) {
  const failures = modelFailures(model);
  return failures.length ? <ul className="failure-summary">{failures.map((failure) => <li key={failure}>{failureLabel(failure)}</li>)}</ul> : <div className="gate-success"><Check size={16} />全部确定性门槛已通过</div>;
}
