import {
  Activity,
  Braces,
  Check,
  CircleDashed,
  Database,
  FileWarning,
  LockKeyhole,
  Newspaper,
  ShieldCheck
} from "lucide-react";
import { formatTime, shortId } from "../lib/format";
import type { ResearchMonitorStatus } from "../types";
import { StatusMark } from "./Primitives";

const stageLabels: Record<string, string> = {
  waiting: "等待队列",
  headRefresh: "刷新最新 K 线",
  headRefreshed: "最新端已确认",
  oldestBackfill: "向历史起点回填",
  oldestBackfillPaused: "分批检查点",
  originBaselineProbe: "首次起点探测",
  originConfirmationProbe: "跨轮起点复核",
  originPending: "等待起点复核",
  originBaseline: "起点证据 1/2",
  originConfirmed: "起点已确认",
  gapRepair: "修复时间缺口",
  gapRepairChecked: "缺口复核",
  incomplete: "等待完整数据",
  snapshotReady: "不可变快照就绪",
  pageBudgetExhausted: "等待下一批"
};

const blockerLabels: Record<string, string> = {
  research_data_not_configured: "尚未配置本机研究数据目录",
  universe_integrity_unverified: "研究宇宙哈希尚未通过",
  history_universe_mismatch: "回填进度不属于当前冻结研究宇宙",
  multi_asset_history_incomplete: "多资产全历史仍未完整",
  unresolved_history_gaps: "历史时间网格仍有缺口",
  immutable_data_conflicts: "不可变行情存在冲突",
  aligned_cohort_not_built: "严格对齐 cohort 尚未生成",
  requires_90_day_forward_public_shadow: "需完成至少 90 天前瞻公共 Shadow",
  static_cost_only: "当前仅采用静态保守成本，尚未完成成交冲击校准"
};

function bytes(value: number | undefined): string {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let shown = value;
  let unit = 0;
  while (shown >= 1024 && unit < units.length - 1) {
    shown /= 1024;
    unit += 1;
  }
  return `${shown.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
}

function estimatedProgress(
  item: NonNullable<ResearchMonitorStatus["history"]>["instruments"][number],
  universeAt: string | null | undefined
): number {
  if (item.backfillComplete) return 1;
  const listed = item.listedAt ? Date.parse(item.listedAt) : Number.NaN;
  const end = universeAt ? Date.parse(universeAt) : Number.NaN;
  const oldest = item.firstOpenTsMs;
  if (!Number.isFinite(listed) || !Number.isFinite(end) || !oldest || end <= listed) return 0;
  return Math.max(0, Math.min(0.99, (end - oldest) / (end - listed)));
}

export function ResearchObservatory({ status }: { status: ResearchMonitorStatus | null }) {
  const history = status?.history;
  const universe = status?.universe;
  const instruments = history?.instruments ?? [];
  const completeAssets = instruments.filter((item) => item.backfillComplete).length;
  const active = Boolean(history?.active);
  return <>
    <section className="workspace-panel research-observatory" aria-labelledby="research-observatory-title">
      <div className="panel-heading">
        <div><h2 id="research-observatory-title">多资产公开研究观测台</h2><p>串行采集确认的 5 分钟 K 线；进度按当前上线记录估算，最终以起点双重证据为准</p></div>
        <StatusMark tone={!status?.available ? "neutral" : active ? "info" : completeAssets === instruments.length && instruments.length ? "healthy" : "warning"}>{!status?.available ? "研究目录未连接" : active ? "公开历史回填中" : completeAssets === instruments.length && instruments.length ? "全资产快照就绪" : "等待下一检查点"}</StatusMark>
      </div>
      <div className="research-command-strip">
        <div><Activity size={17} /><span>任务</span><strong>{active ? "RUNNING" : history?.state?.toUpperCase() ?? "IDLE"}</strong></div>
        <div><Braces size={17} /><span>页预算</span><strong>{history ? `${history.pagesConsumed.toLocaleString("zh-CN")} / ${history.pageBudget.toLocaleString("zh-CN")}` : "—"}</strong></div>
        <div><Database size={17} /><span>公共仓库</span><strong>{bytes(history?.databaseBytes)}</strong></div>
        <div><ShieldCheck size={17} /><span>交易能力</span><strong>无</strong></div>
        <div><span>最后证据</span><strong>{formatTime(history?.updatedAt)}</strong></div>
      </div>
      {instruments.length ? <div className="asset-backfill-list">
        {instruments.map((item) => {
          const progress = estimatedProgress(item, universe?.createdAt);
          const healthy = item.missingBars === 0 && item.unresolvedConflicts === 0;
          return <article className="asset-backfill-row" key={item.instrument}>
            <div className="asset-identity"><strong>{item.instrument.replace("-USDT", "")}</strong><span>/ USDT · 5m</span></div>
            <div className="asset-progress">
              <div><span style={{ width: `${Math.max(progress ? 2 : 0, progress * 100)}%` }} /></div>
              <small>{item.backfillComplete ? "100% · 起点已确认" : progress ? `约 ${(progress * 100).toFixed(1)}%` : "等待采集"}</small>
            </div>
            <div className="asset-stat"><span>覆盖</span><strong>{item.coverageDays ? `${item.coverageDays.toFixed(1)} 天` : "—"}</strong></div>
            <div className="asset-stat"><span>本轮新增</span><strong>{item.rowsInsertedThisRun.toLocaleString("zh-CN")}</strong></div>
            <div className="asset-stage"><StatusMark tone={item.backfillComplete ? "healthy" : item.stage === "waiting" ? "neutral" : "info"}>{stageLabels[item.stage] ?? item.stage}</StatusMark><small className={healthy ? "quality-good" : "quality-bad"}>{healthy ? "0 缺口 · 0 冲突" : `${item.missingBars} 缺口 · ${item.unresolvedConflicts} 冲突`}</small></div>
          </article>;
        })}
      </div> : <div className="visual-empty"><CircleDashed /><span>等待冻结研究宇宙与首个历史检查点</span></div>}
      <div className="research-storage-line"><LockKeyhole size={15} /><span>研究存储</span><code>{status?.storageRoot ?? "未配置"}</code><span>执行白名单</span><code>{status?.safety.executionAllowlist.join(", ") ?? "BTC-USDT"}</code></div>
    </section>

    <div className="research-readiness-grid">
      <section className="workspace-panel research-readiness" aria-labelledby="research-readiness-title">
        <div className="panel-heading"><div><h2 id="research-readiness-title">训练准入门</h2><p>完成历史训练不等于可晋级，更不等于可投入实际资金</p></div><FileWarning size={20} /></div>
        <div className="readiness-flow" role="img" aria-label="研究宇宙、全历史、对齐 cohort、模型样本外评估、90 天前瞻影子验证的顺序门">
          {[
            { label: "宇宙哈希", done: Boolean(universe?.valid) },
            { label: "完整历史", done: instruments.length > 0 && completeAssets === instruments.length },
            { label: "严格对齐", done: Boolean(status?.cohort) },
            { label: "模型 OOS", done: false },
            { label: "90 天 Shadow", done: false }
          ].map((step, index) => <div className={step.done ? "done" : index === (universe?.valid ? 1 : 0) ? "active" : "locked"} key={step.label}><span>{step.done ? <Check size={15} /> : index + 1}</span><strong>{step.label}</strong></div>)}
        </div>
        <ul className="research-blockers">
          {(status?.blockers ?? ["research_data_not_configured"]).map((blocker) => <li key={blocker}><LockKeyhole size={15} /><span>{blockerLabels[blocker] ?? blocker}</span></li>)}
        </ul>
      </section>

      <section className="workspace-panel weak-signal-panel" aria-labelledby="weak-signal-title">
        <div className="panel-heading"><div><h2 id="weak-signal-title">新闻弱信号与对齐 cohort</h2><p>标题元数据只做消融研究，不直接触发买卖</p></div><Newspaper size={20} /></div>
        <dl className="evidence-list">
          <div><dt>信号基线</dt><dd>{status?.signals.source ?? "GDELT metadata + VADER"}</dd></div>
          <div><dt>文章正文</dt><dd>{status?.signals.fullTextStored ? "已保存" : "不保存"}</dd></div>
          <div><dt>信号数据库</dt><dd>{status?.signals.available ? bytes(status.signals.databaseBytes) : "尚未采集"}</dd></div>
          <div><dt>对齐 cohort</dt><dd>{status?.cohort ? shortId(status.cohort.cohortId, 16) : "等待完整历史"}</dd></div>
          <div><dt>可晋级</dt><dd><StatusMark tone="warning">否 · 前瞻证据不足</StatusMark></dd></div>
        </dl>
      </section>
    </div>
  </>;
}
