import { AlertTriangle, Archive, CheckCircle2, Database, FileKey, RefreshCw, Rows3, ShieldCheck } from "lucide-react";
import { CoverageTimeline, DataLineage } from "../components/Charts";
import { PageHeader, StatusMark } from "../components/Primitives";
import { formatTime, shortId } from "../lib/format";
import { deriveResearchStatus } from "../lib/research";
import type { MLStatus } from "../types";

export function DataPage({ ml, refreshing, onRefresh }: { ml: MLStatus | null; refreshing: boolean; onRefresh: () => Promise<void> }) {
  const research = deriveResearchStatus(ml);
  const dataset = research.dataset;
  const hasConfirmedHistory = Boolean(
    dataset
    && dataset.confirmedRows > 0
    && dataset.syncState === "complete"
    && dataset.snapshotSha256
  );
  const validQuality = hasConfirmedHistory && dataset?.gaps === 0 && dataset?.conflicts === 0;
  const coverageDays = ml?.longRun.dataWarehouse?.coverageDays ?? null;
  const integrityMessage = validQuality
    ? "缺口与未解决冲突均为零。"
    : dataset?.lastErrorType === "HistoryOriginUnconfirmed"
      ? "交易所最早边界正在跨轮复核；首次空页不会生成训练快照。"
      : "当前后端尚未上报完整缺口/冲突统计；不会据此宣称数据完整。";
  const reportedFolds = Math.max(0, ...((ml?.longRun.review.models ?? []).map((model) => model.metrics?.folds ?? 0)));
  return <div className="page-stack data-page">
    <PageHeader title="数据中心" description="扩展历史覆盖，但先保证连续、完成、去重和时间隔离；更多数据不等于更高收益。" meta={<StatusMark tone={!dataset ? "neutral" : dataset.syncState === "failed" ? "danger" : dataset.syncState === "running" ? "warning" : hasConfirmedHistory ? "healthy" : "neutral"}>{!dataset ? "等待仓库遥测" : dataset.syncState === "running" ? "回填中" : dataset.syncState === "failed" ? "回填失败" : hasConfirmedHistory ? "公共数据已确认" : "等待首次全量回填"}</StatusMark>} actions={<button className="button secondary" disabled={refreshing} onClick={() => void onRefresh()}><RefreshCw className={refreshing ? "spin" : ""} size={16} />刷新证据</button>} />
    <CoverageTimeline research={research} />
    <div className="data-grid">
      <DataLineage research={research} />
      <section className="workspace-panel data-integrity" aria-labelledby="integrity-title">
        <div className="panel-heading"><div><h2 id="integrity-title">快照完整性</h2><p>模型只读取冻结快照，不直接读取账户或凭证</p></div><FileKey size={20} /></div>
        <div className="integrity-score"><div className={validQuality ? "good" : "unknown"}>{validQuality ? <CheckCircle2 /> : <ShieldCheck />}<strong>{validQuality ? "通过" : "待全量遥测"}</strong></div><p>{integrityMessage}</p></div>
        <dl className="evidence-list">
          <div><dt>快照哈希</dt><dd><code>{shortId(dataset?.snapshotSha256, 18)}</code></dd></div>
          <div><dt>更新时刻</dt><dd>{formatTime(dataset?.updatedAt)}</dd></div>
          <div><dt>覆盖时长</dt><dd>{coverageDays === null ? "—" : `约 ${coverageDays.toFixed(1)} 天`}</dd></div>
          <div><dt>确认行数</dt><dd>{dataset?.confirmedRows.toLocaleString("zh-CN") ?? "—"}</dd></div>
        </dl>
      </section>
    </div>
    <section className="workspace-panel partition-panel" aria-labelledby="partition-title">
      <div className="panel-heading"><div><h2 id="partition-title">v4 滚动 Walk-forward 时间协议</h2><p>固定窗口向前滚动；每折训练与 OOS 之间强制 purge + embargo</p></div><Archive size={20} /></div>
      <div className="rolling-protocol" role="img" aria-label={`每折365天训练，随后13根五分钟K线隔离，再进行90天非重叠样本外评估，步长90天；当前报告约${reportedFolds || "未知"}折`}>
        <span className="rolling-train"><strong>ROLLING TRAIN</strong><small>365 天 · 固定窗口</small></span>
        <span className="rolling-gap"><strong>隔离</strong><small>12 bars purge + 1 bar embargo</small></span>
        <span className="rolling-oos"><strong>OOS</strong><small>90 天 · 非重叠</small></span>
        <span className="rolling-step"><strong>STEP → 90 天</strong><small>{reportedFolds ? `当前报告 ${reportedFolds} 折` : "折数等待报告"}</small></span>
      </div>
      <div className="partition-notes">
        <div><Rows3 size={17} /><span>测试窗与 step 均为 90 天，OOS 资本与交易不跨折重叠。</span></div>
        <div><Database size={17} /><span>标签跨度 12 bars，再加 1 bar embargo，阻断相邻窗口泄漏。</span></div>
        <div><AlertTriangle size={17} /><span>训练结束后的未来 Shadow 独立累积，不属于 walk-forward 任一折。</span></div>
      </div>
    </section>
    <section className="workspace-panel dataset-ledger" aria-labelledby="dataset-ledger-title">
      <div className="panel-heading"><div><h2 id="dataset-ledger-title">当前数据账本</h2><p>前端已预留多品种/多周期契约；当前执行边界仍固定 BTC-USDT 5m</p></div></div>
      <div className="table-scroll"><table><thead><tr><th>来源</th><th>品种</th><th>周期</th><th>最早</th><th>最新</th><th className="numeric">确认行</th><th>质量</th><th>用途</th></tr></thead><tbody><tr><td>{dataset?.source ?? "OKX public"}</td><td>{dataset?.instrument ?? "BTC-USDT"}</td><td>{dataset?.bar ?? "5m"}</td><td>{formatTime(dataset?.earliestAt)}</td><td>{formatTime(dataset?.latestAt)}</td><td className="numeric">{dataset?.confirmedRows.toLocaleString("zh-CN") ?? "—"}</td><td><StatusMark tone={validQuality ? "healthy" : "neutral"}>{validQuality ? "连续" : "待遥测"}</StatusMark></td><td>训练 / OOS / Shadow</td></tr></tbody></table></div>
    </section>
  </div>;
}
