import { BrainCircuit, CheckCircle2, CircleDashed, FileSearch, ShieldCheck, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";
import { FailureSummary, ModelLineage } from "../components/Charts";
import { EvidenceDrawer, PageHeader, StatusMark } from "../components/Primitives";
import type { ExplanationMode } from "../components/Shell";
import { formatPercent, formatTime, shortId } from "../lib/format";
import { failureLabel, modelFailures } from "../lib/research";
import type { MLStatus, SupervisorModelReview } from "../types";

function ModelState({ model }: { model: SupervisorModelReview }) {
  const failures = modelFailures(model);
  const tone = model.state === "champion" || (model.state === "validated" && failures.length === 0) ? "healthy" : model.state === "rejected" ? "danger" : "warning";
  const label = model.state === "champion" ? "Champion" : model.state === "validated" && failures.length === 0 ? "等待监督" : model.state === "rejected" ? "已拒绝" : model.state;
  return <StatusMark tone={tone}>{label}</StatusMark>;
}

export function ModelsPage({ ml, explanationMode }: { ml: MLStatus | null; explanationMode: ExplanationMode }) {
  const models = ml?.longRun.review.models ?? [];
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = models.find((model) => model.modelId === selectedId) ?? null;
  const comparison = useMemo(() => {
    const cohortKey = (model: SupervisorModelReview) => {
      const metrics = model.metrics;
      if (!metrics?.benchmarkCohortId || !metrics.evaluationDatasetSha256 || !metrics.splitProtocolSha256) return null;
      return `${metrics.benchmarkCohortId}|${metrics.evaluationDatasetSha256}|${metrics.splitProtocolSha256}`;
    };
    const anchor = models.find((model) => cohortKey(model));
    const anchorKey = anchor ? cohortKey(anchor) : null;
    const comparable = anchorKey ? models.filter((model) => cohortKey(model) === anchorKey) : [];
    const ranked = [...comparable].sort((a, b) => (b.metrics?.netReturn ?? -Infinity) - (a.metrics?.netReturn ?? -Infinity));
    return { anchor, comparable, best: ranked[0] ?? null, excluded: models.filter((model) => model.metrics && cohortKey(model) !== anchorKey).length };
  }, [models]);
  const maxAbs = Math.max(0.01, ...comparison.comparable.map((model) => Math.abs(model.metrics?.netReturn ?? 0)));
  const baselineLabel = selected?.comparisonBaselineModelId
    ? selected.comparisonBaselineCohort === selected.metrics?.benchmarkCohortId
      ? "相同 cohort 的旧 champion"
      : "在新 cohort 重训的 champion 配方 baseline"
    : "未建立 paired baseline";
  return <div className="page-stack models-page">
    <PageHeader title="模型评估" description="用同口径成本、时间隔离与未来 Shadow 比较候选；准确率不等于可执行收益。" meta={<><StatusMark tone={ml?.longRun.champion ? "healthy" : "warning"}>{ml?.longRun.champion ? "Champion 有效" : "无 Champion"}</StatusMark><span>代次 {ml?.longRun.review.generation ?? 0}</span></>} />
    <div className="model-summary-grid">
      <section className="workspace-panel champion-panel" aria-labelledby="champion-title">
        <div className="panel-heading"><div><h2 id="champion-title">当前 Champion</h2><p>只有应用完成的 Codex 晋级决策才可执行</p></div><ShieldCheck size={21} /></div>
        {ml?.longRun.champion ? <div className="champion-live"><Sparkles size={30} /><div><strong>{shortId(ml.longRun.champion.modelId, 18)}</strong><span>第 {ml.longRun.champion.generation} 代 · {ml.longRun.champion.reviewer}</span><p>{ml.longRun.champion.rationale}</p></div></div> : <div className="visual-empty tall"><CircleDashed /><div><strong>尚未晋级</strong><span>候选必须同时通过 OOS、Shadow、比较门与审计链。</span></div></div>}
        <dl className="evidence-list"><div><dt>证据哈希</dt><dd><code>{shortId(ml?.longRun.review.evidenceSha256, 20)}</code></dd></div><div><dt>监督批准</dt><dd>{ml?.longRun.review.championSupervisorApproved ? "已完整落盘" : "无可执行批准"}</dd></div><div><dt>执行 Lease</dt><dd>{ml?.longRun.activeSupervisorLease ? `至 ${formatTime(ml.longRun.activeSupervisorLease.expiresAt)}` : "无"}</dd></div></dl>
      </section>
      <section className="workspace-panel comparison-panel" aria-labelledby="comparison-title">
        <div className="panel-heading"><div><h2 id="comparison-title">候选扣成本 OOS 比较</h2><p>中心线为 0；只显示报告中的实际聚合净收益</p></div><BrainCircuit size={21} /></div>
        <div className="comparison-chart" role="img" aria-label="候选模型样本外扣成本净收益比较">
          {comparison.comparable.slice(0, 8).map((model) => {
            const value = model.metrics?.netReturn ?? 0;
            const width = Math.max(1, (Math.abs(value) / maxAbs) * 46);
            return <button key={model.modelId} onClick={() => setSelectedId(model.modelId)}><span>{shortId(model.modelId, 8)}</span><div className="comparison-lane"><i className={value >= 0 ? "positive" : "negative"} style={{ width: `${width}%`, left: value >= 0 ? "50%" : `${50 - width}%` }} /></div><strong className={value >= 0 ? "positive" : "negative"}>{formatPercent(value, 2)}</strong></button>;
          })}
          <div className="comparison-axis"><span>亏损</span><i /><span>收益</span></div>
          {comparison.comparable.length === 0 ? <div className="visual-empty"><CircleDashed /><span>缺少同 cohort / 数据快照 / split 协议的 v4 报告，不进行跨口径排名</span></div> : null}
        </div>
        {comparison.best ? <div className="best-candidate"><CheckCircle2 size={17} /><span>同一 cohort 内聚合净收益最高</span><strong>{shortId(comparison.best.modelId, 12)} · {formatPercent(comparison.best.metrics?.netReturn)}</strong></div> : null}
        {comparison.excluded > 0 ? <div className="comparison-caveat">{comparison.excluded} 个不同快照或协议的原始净收益未直接排名；跨 cohort 只使用后端 paired baseline</div> : null}
      </section>
    </div>
    <ModelLineage longRun={ml?.longRun ?? null} />
    <section className="workspace-panel model-ledger" aria-labelledby="model-ledger-title">
      <div className="panel-heading"><div><h2 id="model-ledger-title">候选模型账本</h2><p>{explanationMode === "summary" ? "先看可执行结论；技术证据点开查看" : "显示模型 ID、指标、Shadow 与门槛状态"}</p></div><span>{models.length} 个候选</span></div>
      <div className="table-scroll"><table><thead><tr><th>模型</th><th>状态</th><th className="numeric">OOS 行</th><th className="numeric">交易</th><th className="numeric">OOS 净收益</th><th className="numeric">回撤</th><th className="numeric">Shadow BUY</th><th>首要阻塞</th><th>证据</th></tr></thead><tbody>{models.map((model) => { const failures = modelFailures(model); return <tr key={model.modelId}><td><strong>{shortId(model.modelId, explanationMode === "evidence" ? 16 : 10)}</strong><small>{formatTime(model.createdAt)}</small></td><td><ModelState model={model} /></td><td className="numeric">{model.metrics?.oosRows ?? "—"}</td><td className="numeric">{model.metrics?.trades ?? "—"}</td><td className={`numeric ${(model.metrics?.netReturn ?? 0) < 0 ? "negative" : "positive"}`}>{formatPercent(model.metrics?.netReturn)}</td><td className="numeric">{formatPercent(model.metrics?.maxDrawdown)}</td><td className="numeric">{model.shadow.settledBuys}</td><td>{failures[0] ? failureLabel(failures[0]) : "无"}</td><td><button className="table-action" onClick={() => setSelectedId(model.modelId)}><FileSearch size={15} />查看</button></td></tr>;})}</tbody></table>{models.length === 0 ? <div className="visual-empty"><CircleDashed /><span>尚无模型记录</span></div> : null}</div>
    </section>
    <EvidenceDrawer open={Boolean(selected)} title={selected ? shortId(selected.modelId, 22) : "模型证据"} subtitle={selected ? `${selected.trainer} · ${formatTime(selected.createdAt)}` : undefined} onClose={() => setSelectedId(null)}>
      {selected ? <>
        <section className="drawer-verdict"><ModelState model={selected} /><h3>{modelFailures(selected).length ? "该候选当前不可晋级" : "确定性与 Shadow 门已通过，等待监督审查"}</h3><p>{modelFailures(selected)[0] ? `首要原因：${failureLabel(modelFailures(selected)[0])}。` : "通过门槛不代表保证盈利；Codex 仍需校验证据与当前系统状态。"}</p></section>
        <section className="drawer-section"><h3>指标证据</h3><dl className="metric-definition-grid"><div><dt>OOS 行</dt><dd>{selected.metrics?.oosRows ?? "—"}</dd></div><div><dt>非重叠交易</dt><dd>{selected.metrics?.trades ?? "—"}</dd></div><div><dt>准确率</dt><dd>{formatPercent(selected.metrics?.aggregateAccuracy)}</dd></div><div><dt>净收益</dt><dd>{formatPercent(selected.metrics?.netReturn)}</dd></div><div><dt>最大回撤</dt><dd>{formatPercent(selected.metrics?.maxDrawdown)}</dd></div><div><dt>最弱折净收益</dt><dd>{formatPercent(selected.metrics?.worstFoldNetReturn)}</dd></div><div><dt>Shadow BUY</dt><dd>{selected.shadow.settledBuys}</dd></div><div><dt>Shadow 天数</dt><dd>{selected.shadow.durationDays.toFixed(1)}</dd></div></dl></section>
        <section className="drawer-section"><h3>门槛解释</h3><FailureSummary model={selected} /></section>
        <section className="drawer-section technical-evidence"><h3>可复核标识与比较口径</h3><dl className="evidence-list"><div><dt>Artifact SHA-256</dt><dd><code>{selected.artifactSha256}</code></dd></div><div><dt>Training config</dt><dd><code>{selected.trainingConfigSha256 ?? "—"}</code></dd></div><div><dt>Report SHA-256</dt><dd><code>{selected.metrics?.reportSha256 ?? "—"}</code></dd></div><div><dt>Benchmark cohort</dt><dd><code>{selected.metrics?.benchmarkCohortId ?? "—"}</code></dd></div><div><dt>Evaluation dataset</dt><dd><code>{selected.metrics?.evaluationDatasetSha256 ?? "—"}</code></dd></div><div><dt>Split protocol</dt><dd><code>{selected.metrics?.splitProtocolSha256 ?? "—"}</code></dd></div><div><dt>Paired baseline</dt><dd>{baselineLabel}</dd></div><div><dt>Baseline model</dt><dd><code>{selected.comparisonBaselineModelId ?? "—"}</code></dd></div><div><dt>Baseline cohort</dt><dd><code>{selected.comparisonBaselineCohort ?? "—"}</code></dd></div><div><dt>评估模式</dt><dd>{selected.metrics?.evaluationMode ?? "—"}</dd></div><div><dt>往返成本</dt><dd>{selected.metrics?.roundTripCostBps ?? "—"} bps</dd></div></dl><p>跨 cohort 比较使用同一新快照重训的 champion 配方 baseline，不会把静态旧模型放到其训练前历史上回测。</p></section>
      </> : null}
    </EvidenceDrawer>
  </div>;
}
