import { AlertTriangle, ArrowRight, Bot, CheckCircle2, Clock3, Database, Shield, WalletCards } from "lucide-react";
import { DrawdownBand, PerformanceChart, PipelineMap } from "../components/Charts";
import { DetailLink, PageHeader, StatusMark } from "../components/Primitives";
import type { ExplanationMode } from "../components/Shell";
import { formatNumber, formatPercent, formatTime, shortId } from "../lib/format";
import { deriveResearchStatus, runtimeNarrative } from "../lib/research";
import type { AccountData, AuditEvent, MarketData, MLStatus, SystemStatus } from "../types";

const eventLabels: Record<string, string> = {
  "system.started": "本地服务已启动",
  "autonomy.cycle_failed": "自动周期未完成",
  "autonomy.training_completed": "训练批次已完成",
  "autonomy.model_rejected": "候选模型已拒绝",
  "autonomy.model_promoted": "新冠军已晋级",
  "order.accepted": "订单已被交易所接受",
  "order.reconciled": "订单终态已核对",
  "safety.kill_engaged": "急停已触发",
  "safety.deadman_failed": "失联保护心跳失败"
};

export function RuntimePage({ status, market, account, ml, events, explanationMode, onNavigate }: { status: SystemStatus | null; market: MarketData | null; account: AccountData | null; ml: MLStatus | null; events: AuditEvent[]; explanationMode: ExplanationMode; onNavigate: (view: "data" | "training" | "models" | "execution" | "audit") => void }) {
  const longRun = ml?.longRun ?? null;
  const narrative = status
    ? runtimeNarrative(longRun, status.credentialConfigured)
    : { now: "运行界面已就绪，交易执行保持锁定", why: "本地服务尚未返回系统与凭证状态", next: "保留最后有效证据并自动重连", tone: "warning" as const };
  const research = deriveResearchStatus(ml);
  const position = longRun?.activePosition ?? null;
  const alerts = [
    ...(longRun?.lastError ? [{ label: "长期运行错误", detail: longRun.lastError.errorType, time: longRun.lastError.at, tone: longRun.lastError.failClosed ? "danger" : "warning" }] : []),
    ...events.filter((event) => event.eventType.includes("failed") || event.eventType.includes("kill") || event.eventType.includes("uncertain")).slice(0, 3).map((event) => ({ label: eventLabels[event.eventType] ?? event.eventType, detail: event.actor, time: event.utcTime, tone: event.eventType.includes("kill") ? "danger" : "warning" }))
  ];
  const runtimeLabel = longRun?.state.runtimeStatus === "running" ? "闭环运行" : longRun?.state.runtimeStatus === "manual_review" ? "人工核对" : longRun?.state.runtimeStatus === "suspended" ? "已暂停" : longRun?.state.runtimeStatus === "waiting_champion" ? "等待冠军" : longRun?.state.runtimeStatus === "waiting_supervisor" ? "等待监督" : "研究阶段";

  return <div className="page-stack runtime-page">
    <PageHeader title="AI 量化运行中心" description="先看当前状态、阻塞原因与下一步，再进入证据细节。" meta={<><StatusMark tone={narrative.tone === "neutral" ? "info" : narrative.tone}>{runtimeLabel}</StatusMark><span>BTC-USDT · cash SPOT</span></>} />

    <section className={`now-why-next ${narrative.tone}`} aria-label="当前运行结论">
      <article><span>NOW</span><div><Bot size={19} /><strong>{narrative.now}</strong></div>{explanationMode === "evidence" ? <code>state v{longRun?.state.stateVersion ?? "—"} · {longRun?.state.runtimeStatus ?? "loading"}</code> : null}</article>
      <article><span>WHY</span><div><Shield size={19} /><strong>{narrative.why}</strong></div>{explanationMode === "evidence" ? <code>{shortId(longRun?.review.evidenceSha256, 16)}</code> : null}</article>
      <article><span>NEXT</span><div><ArrowRight size={19} /><strong>{narrative.next}</strong></div>{explanationMode === "evidence" ? <code>next train {formatTime(research.training?.nextRunAt)}</code> : null}</article>
    </section>

    <div className="runtime-visual-grid">
      <PerformanceChart market={market} longRun={longRun} />
      <aside className="runtime-side-rail">
        <section className="workspace-panel position-overview" aria-labelledby="position-title">
          <div className="panel-heading"><div><h2 id="position-title">模型自有仓位</h2><p>与账户原有 BTC 严格分离</p></div><WalletCards size={20} /></div>
          {position ? <div className="position-live"><div className="position-quantity"><strong>{position.remainingSize}</strong><span>BTC 剩余净库存</span></div><dl><div><dt>入场均价</dt><dd>{formatNumber(position.entryAvgPrice, 2)}</dd></div><div><dt>止损</dt><dd>{formatNumber(position.stopPrice, 2)}</dd></div><div><dt>止盈</dt><dd>{formatNumber(position.takeProfitPrice, 2)}</dd></div><div><dt>计划退出</dt><dd>{formatTime(position.exitDueAt)}</dd></div></dl><DetailLink onClick={() => onNavigate("execution")}>查看成交与退出链</DetailLink></div> : <div className="flat-position"><div className="flat-ring">0<span>BTC</span></div><div><strong>当前空仓</strong><p>账户总余额不会被计入模型自有库存。</p></div></div>}
          <div className="account-context"><Database size={16} /><span>账户只读权益</span><strong>{formatNumber(account?.equityUsdt, 2)} USDT</strong></div>
        </section>
        <DrawdownBand longRun={longRun} />
        <section className="workspace-panel alert-center" aria-labelledby="alert-title">
          <div className="panel-heading"><div><h2 id="alert-title">问题中心</h2><p>失败保持可见，不用短暂提示掩盖</p></div><span>{alerts.length}</span></div>
          {alerts.length ? <div className="alert-list">{alerts.map((alert, index) => <button key={`${alert.label}-${index}`} onClick={() => onNavigate("audit")}><AlertTriangle className={alert.tone} size={17} /><div><strong>{alert.label}</strong><span>{alert.detail} · {formatTime(alert.time)}</span></div><ArrowRight size={15} /></button>)}</div> : <div className="clear-state"><CheckCircle2 size={20} /><span>没有未处理的运行问题</span></div>}
        </section>
      </aside>
    </div>
    <PipelineMap longRun={longRun} research={research} />
    <section className="runtime-kpi-band" aria-label="关键风险指标">
      <div><span>模型闭环</span><strong>{longRun?.demoPerformance.closedPositions ?? 0}</strong><small>实际成交终态</small></div>
      <div><span>扣费净收益</span><strong className={(longRun?.demoPerformance.netReturn ?? 0) < 0 ? "negative" : "positive"}>{formatPercent(longRun?.demoPerformance.netReturn)}</strong><small>不代表未来收益</small></div>
      <div><span>最大回撤</span><strong>{formatPercent(longRun?.demoPerformance.maxDrawdown)}</strong><small>上限 {formatPercent(Number(longRun?.policy.max_demo_drawdown ?? 0.05))}</small></div>
      <div><span>候选 / 冠军</span><strong>{longRun?.review.models.length ?? 0} / {longRun?.champion ? 1 : 0}</strong><small>证据哈希绑定</small></div>
      <div><span>监督 Lease</span><strong>{longRun?.activeSupervisorLease ? "有效" : "无"}</strong><small>{longRun?.activeSupervisorLease ? `至 ${formatTime(longRun.activeSupervisorLease.expiresAt)}` : "不能开仓"}</small></div>
      <div><span>训练更新</span><strong>{formatTime(longRun?.latestTraining?.completedAt ?? longRun?.latestTraining?.startedAt)}</strong><small><Clock3 size={13} />每 {longRun?.policy.train_interval_hours ?? 24} 小时</small></div>
    </section>
  </div>;
}
