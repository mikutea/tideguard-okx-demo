import { Activity, BrainCircuit, Check, Clock3, Cpu, DatabaseZap, Gauge, HardDrive, Play, ShieldCheck } from "lucide-react";
import { WalkForwardMatrix } from "../components/Charts";
import { PageHeader, StatusMark } from "../components/Primitives";
import { formatTime, shortId } from "../lib/format";
import { deriveResearchStatus } from "../lib/research";
import type { MLStatus } from "../types";

const featureGroups = [
  { label: "多尺度收益", count: 5, color: "jade" },
  { label: "波动率", count: 2, color: "blue" },
  { label: "区间与成交量", count: 2, color: "bronze" },
  { label: "EMA 距离", count: 2, color: "cyan" },
  { label: "RSI", count: 1, color: "vermilion" },
  { label: "小时 / 星期周期", count: 4, color: "slate" }
];

const stageOrder = ["syncing", "snapshotting", "feature_build", "walk_forward", "final_fit", "registering", "completed"] as const;
const stageLabels: Record<(typeof stageOrder)[number], string> = {
  syncing: "公共历史回填",
  snapshotting: "冻结数据快照",
  feature_build: "冻结特征计算",
  walk_forward: "Walk-forward OOS",
  final_fit: "最终窗口拟合",
  registering: "内容寻址注册",
  completed: "等待监督审查"
};

export function TrainingPage({ ml, busy, onTrain }: { ml: MLStatus | null; busy: boolean; onTrain: () => Promise<void> }) {
  const research = deriveResearchStatus(ml);
  const training = research.training;
  const currentStageIndex = training?.stage === "failed" || training?.stage === "idle" ? -1 : stageOrder.indexOf(training?.stage ?? "syncing");
  const running = ml?.longRun.latestTraining?.status === "running";
  const models = ml?.longRun.review.models ?? [];
  return <div className="page-stack training-page">
    <PageHeader title="训练任务" description="训练、验证、工件冻结与 Shadow 分阶段运行；模型不能在线修改代码或风险边界。" meta={<StatusMark tone={!ml ? "neutral" : running ? "warning" : ml.longRun.latestTraining?.status === "failed" ? "danger" : "healthy"}>{!ml ? "等待训练遥测" : running ? "训练进行中" : ml.longRun.latestTraining?.status === "failed" ? "最近训练失败" : "调度正常"}</StatusMark>} actions={<button className="button primary" disabled={busy || running || !ml} onClick={() => void onTrain()}><Play size={16} />{busy || running ? "训练中…" : "运行下一批训练"}</button>} />

    <section className="workspace-panel training-stage-panel" aria-labelledby="training-stage-title">
      <div className="panel-heading"><div><h2 id="training-stage-title">任务阶段与证据产物</h2><p>阶段级进度；没有后端细粒度遥测时不伪造百分比</p></div><span>{ml?.longRun.latestTraining ? `run ${shortId(ml.longRun.latestTraining.runId, 12)}` : "尚无 run"}</span></div>
      <ol className="training-stages">{stageOrder.map((stage, index) => {
        const done = currentStageIndex > index || training?.stage === "completed";
        const active = currentStageIndex === index && running;
        return <li className={`${done ? "done" : ""} ${active ? "active" : ""}`} key={stage}><span>{done ? <Check size={16} /> : index + 1}</span><div><strong>{stageLabels[stage]}</strong><small>{active ? "正在处理" : done ? "证据已落盘" : "等待上游"}</small></div></li>;
      })}</ol>
      <div className="training-runtime-line"><div><Clock3 size={16} /><span>开始</span><strong>{formatTime(ml?.longRun.latestTraining?.startedAt)}</strong></div><div><Clock3 size={16} /><span>完成</span><strong>{formatTime(ml?.longRun.latestTraining?.completedAt)}</strong></div><div><Activity size={16} /><span>下次计划</span><strong>{formatTime(training?.nextRunAt)}</strong></div><div><Gauge size={16} /><span>耗时</span><strong>{training?.elapsedSeconds === null || training?.elapsedSeconds === undefined ? "—" : `${training.elapsedSeconds.toFixed(1)} 秒`}</strong></div></div>
    </section>

    <div className="training-grid">
      <section className="workspace-panel feature-contract" aria-labelledby="feature-contract-title">
        <div className="panel-heading"><div><h2 id="feature-contract-title">冻结特征契约</h2><p>下图表示 16 个输入字段的组成，不代表特征重要性</p></div><BrainCircuit size={20} /></div>
        <div className="feature-stack" role="img" aria-label="16个冻结特征：5个多尺度收益、2个波动率、2个区间与成交量、2个EMA距离、1个RSI、4个时间周期">
          {featureGroups.map((group) => <span className={group.color} style={{ flex: group.count }} key={group.label}><strong>{group.count}</strong></span>)}
        </div>
        <div className="feature-list">{featureGroups.map((group) => <div key={group.label}><i className={group.color} /><span>{group.label}</span><strong>{group.count}</strong></div>)}</div>
        <div className="contract-hash"><ShieldCheck size={17} /><div><span>Feature contract SHA-256</span><code>{shortId(ml?.engine.featureContractSha256, 24)}</code></div></div>
      </section>
      <section className="workspace-panel resource-panel" aria-labelledby="resource-title">
        <div className="panel-heading"><div><h2 id="resource-title">本机资源与降级</h2><p>资源遥测未接入时只显示未知，不用示例数字冒充实时值</p></div><Cpu size={20} /></div>
        <div className="resource-gauges">
          <div><span><Cpu size={17} />CPU</span><div className="resource-track"><i style={{ width: `${training?.cpuPercent ?? 0}%` }} /></div><strong>{training?.cpuPercent === null || training?.cpuPercent === undefined ? "待上报" : `${training.cpuPercent.toFixed(0)}%`}</strong></div>
          <div><span><HardDrive size={17} />内存</span><div className="resource-track"><i style={{ width: training?.memoryMb ? `${Math.min(100, training.memoryMb / 64)}%` : "0%" }} /></div><strong>{training?.memoryMb === null || training?.memoryMb === undefined ? "待上报" : `${training.memoryMb.toFixed(0)} MB`}</strong></div>
          <div><span><DatabaseZap size={17} />数据</span><div className="resource-track"><i style={{ width: "100%" }} /></div><strong>{research.dataset?.confirmedRows.toLocaleString("zh-CN") ?? "—"} 行</strong></div>
        </div>
        <div className="degradation-note"><ShieldCheck size={18} /><p>训练不会阻塞活动仓位监控。持仓存在时跳过计划训练；资源不足时保留上次有效 champion 与证据。</p></div>
      </section>
    </div>
    <WalkForwardMatrix models={models} research={research} />
  </div>;
}
