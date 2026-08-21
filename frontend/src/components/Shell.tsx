import {
  Activity,
  AlertOctagon,
  BrainCircuit,
  Database,
  FileClock,
  GraduationCap,
  HardDriveDownload,
  LockKeyhole,
  Play,
  Settings2,
  ShieldCheck,
  Wifi,
  WifiOff
} from "lucide-react";
import type { ReactNode } from "react";
import brandIcon from "../assets/moheng-app-icon.png";
import { environmentUiPolicy } from "../lib/environment.js";
import { formatTime } from "../lib/format";
import type { EnvironmentMode, SystemStatus } from "../types";

export type ViewKey = "runtime" | "data" | "training" | "models" | "execution" | "audit";
export type ExplanationMode = "summary" | "evidence";

const navItems: Array<{ key: ViewKey; label: string; short: string; icon: typeof Activity }> = [
  { key: "runtime", label: "运行中心", short: "运行", icon: Activity },
  { key: "data", label: "数据中心", short: "数据", icon: Database },
  { key: "training", label: "训练任务", short: "训练", icon: GraduationCap },
  { key: "models", label: "模型评估", short: "模型", icon: BrainCircuit },
  { key: "execution", label: "策略执行", short: "执行", icon: Play },
  { key: "audit", label: "审计与设置", short: "设置", icon: Settings2 }
];

function EnvironmentPill({ mode }: { mode: EnvironmentMode }) {
  return <span className={`environment-pill ${mode}`}>{mode === "live" ? "LIVE · 实际资金" : mode === "demo" ? "OKX DEMO" : "ENV UNKNOWN · LOCKED"}</span>;
}

export function Shell({
  active,
  onNavigate,
  explanationMode,
  onExplanationMode,
  environment,
  transitionLocked,
  status,
  connection,
  lastUpdated,
  onEmergency,
  children
}: {
  active: ViewKey;
  onNavigate: (view: ViewKey) => void;
  explanationMode: ExplanationMode;
  onExplanationMode: (mode: ExplanationMode) => void;
  environment: EnvironmentMode;
  transitionLocked: boolean;
  status: SystemStatus | null;
  connection: "live" | "stale" | "offline" | "loading";
  lastUpdated: string | null;
  onEmergency: () => void;
  children: ReactNode;
}) {
  const live = environment === "live";
  const unknown = environment === "unknown";
  const environmentPolicy = environmentUiPolicy(environment);
  return (
    <div className={`moheng-shell environment-${environment}`}>
      {environmentPolicy.showLiveBanner ? <div className="live-banner" role="alert"><AlertOctagon size={18} />LIVE · 当前环境可提交实际资金订单 · 收益不受保证 · API Trade 包含写操作</div> : null}
      <aside className="workspace-sidebar" aria-label="主导航">
        <div className="brand-lockup">
          <img src={brandIcon} alt="" />
          <div><strong>墨衡</strong><span>MOHENG</span></div>
        </div>
        <nav>
          {navItems.map(({ key, label, icon: Icon }) => <button key={key} className={active === key ? "active" : ""} onClick={() => onNavigate(key)} aria-current={active === key ? "page" : undefined}><Icon size={19} strokeWidth={1.7} /><span>{label}</span></button>)}
        </nav>
        <div className="sidebar-boundary">
          <ShieldCheck size={18} />
          <div><strong>{live ? "LIVE 高危边界" : unknown ? "环境未知 · 执行锁定" : "Demo 安全边界"}</strong><span>{live ? "实际资金 · 写操作" : unknown ? "等待服务端权威状态" : "模拟盘 · 本机监督"}</span></div>
        </div>
      </aside>
      <div className="workspace-main">
        <header className="workspace-topbar">
          <div className="topbar-cluster">
            <EnvironmentPill mode={environment} />
            <span className={`connection-state ${connection}`}>
              {connection === "offline" ? <WifiOff size={16} /> : <Wifi size={16} />}
              {connection === "live" ? "数据同步健康" : connection === "stale" ? "数据可能延迟" : connection === "offline" ? "本地服务离线" : "正在连接"}
            </span>
            <span className="credential-state"><ShieldCheck size={16} />{status ? status.credentialConfigured ? "凭证已保存" : "凭证未配置" : "凭证状态未知"}</span>
            <time>{lastUpdated ? `更新 ${formatTime(lastUpdated, false)}` : "尚未完成同步"}</time>
          </div>
          <div className="topbar-actions">
            <div className="explanation-toggle" role="group" aria-label="解释层级">
              <button className={explanationMode === "summary" ? "active" : ""} onClick={() => onExplanationMode("summary")}>结论</button>
              <button className={explanationMode === "evidence" ? "active" : ""} onClick={() => onExplanationMode("evidence")}><FileClock size={14} />证据</button>
            </div>
            <button className="emergency-button" onClick={onEmergency}><AlertOctagon size={18} />紧急停止</button>
          </div>
        </header>
        {transitionLocked ? <div className="transition-lock-banner" role="alert"><LockKeyhole size={17} />最终核对已锁定，重启后再试 · 当前禁止授权、预检与订单提交</div> : null}
        <main className="workspace-content">{children}</main>
        <footer className="workspace-footer">
          <span><HardDriveDownload size={15} />本机数据与模型工件</span>
          <span>127.0.0.1</span>
          <span>{live ? "LIVE · 实际资金" : unknown ? "ENV UNKNOWN · LOCKED" : "DEMO 私有交易 · x-simulated-trading: 1"}</span>
          <span>不构成投资建议 · 不保证收益</span>
        </footer>
      </div>
      <nav className="mobile-navigation" aria-label="移动端导航">
        {navItems.map(({ key, short, icon: Icon }) => <button key={key} className={active === key ? "active" : ""} onClick={() => onNavigate(key)} aria-current={active === key ? "page" : undefined}><Icon size={18} /><span>{short}</span></button>)}
      </nav>
    </div>
  );
}
