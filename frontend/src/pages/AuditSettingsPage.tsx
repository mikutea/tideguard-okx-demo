import { AlertTriangle, CheckCircle2, Clipboard, Database, ExternalLink, FileClock, KeyRound, RefreshCw, ScrollText, Settings2, ShieldCheck, Wifi } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { EnvironmentSwitch } from "../components/EnvironmentSwitch";
import { EvidenceDrawer, PageHeader, StatusMark } from "../components/Primitives";
import { formatTime, shortId } from "../lib/format";
import type { AuditEvent, EnvironmentMode, EnvironmentStatus, SystemStatus } from "../types";

const eventLabels: Record<string, string> = {
  "system.started": "本地服务已启动",
  "connection.tested": "连接检查已完成",
  "safety.armed": "手动演练已启用",
  "safety.disarmed": "手动演练已锁定",
  "safety.kill_engaged": "急停已触发",
  "safety.kill_reset": "急停已解除",
  "safety.deadman_failed": "失联撤单保护失败",
  "order.previewed": "订单预检已通过",
  "order.dispatching": "订单正在提交",
  "order.accepted": "交易所已接受订单",
  "order.uncertain": "订单终态不确定",
  "order.reconciled": "订单终态已核对",
  "order.rejected": "交易所拒绝订单",
  "autonomy.cycle_failed": "自动运行周期未完成",
  "autonomy.training_completed": "自动训练已完成",
  "autonomy.model_rejected": "候选模型已拒绝",
  "autonomy.model_promoted": "Champion 已晋级"
};

function severity(eventType: string): "healthy" | "warning" | "danger" | "neutral" {
  if (eventType.includes("kill") || eventType.includes("uncertain") || eventType.includes("manual_review")) return "danger";
  if (eventType.includes("failed") || eventType.includes("rejected") || eventType.includes("warning")) return "warning";
  if (eventType.includes("accepted") || eventType.includes("completed") || eventType.includes("reconciled")) return "healthy";
  return "neutral";
}

export function AuditSettingsPage({ status, environmentStatus, environmentMode, events, chainValid, onRefreshEnvironment, onRefresh, onNotice }: { status: SystemStatus | null; environmentStatus: EnvironmentStatus | null; environmentMode: EnvironmentMode; events: AuditEvent[]; chainValid: boolean | null; onRefreshEnvironment: () => Promise<void>; onRefresh: () => Promise<void>; onNotice: (tone: "success" | "warning" | "error", message: string) => void }) {
  const [selectedEvent, setSelectedEvent] = useState<AuditEvent | null>(null);
  const [testing, setTesting] = useState(false);
  const [connectionResult, setConnectionResult] = useState<Awaited<ReturnType<typeof api.testConnection>> | null>(null);
  const testConnection = async () => {
    setTesting(true);
    try {
      const result = await api.testConnection();
      setConnectionResult(result);
      const tone = result.public && result.privateReachable && result.policyValid ? "success" : result.privateReachable && !result.policyValid ? "error" : "warning";
      onNotice(tone, `连接检查：公共 ${result.public ? "可达" : "失败"} · 账户 API ${result.privateReachable ? "可达" : "失败"} · 权限策略 ${result.policyValid ? "通过" : "拒绝"}`);
      await onRefresh();
    }
    catch (error) { onNotice("error", error instanceof Error ? error.message : "连接检查失败"); }
    finally { setTesting(false); }
  };
  const credentialHint = "开始菜单 > 墨衡 MOHENG > 墨衡凭证管理";
  return <div className="page-stack audit-settings-page">
    <PageHeader title="审计与设置" description="问题、证据、连接与交易环境集中管理；秘密始终留在 Windows Credential Manager。" meta={<><StatusMark tone={chainValid ? "healthy" : chainValid === false ? "danger" : "warning"}>{chainValid ? "审计链有效" : chainValid === false ? "审计链异常" : "审计待检查"}</StatusMark><span>{events.length} 条近期事件</span></>} actions={<button className="button secondary" onClick={() => void onRefresh()}><RefreshCw size={16} />刷新</button>} />
    <section className="workspace-panel audit-timeline" aria-labelledby="audit-title">
      <div className="panel-heading"><div><h2 id="audit-title">事件与问题时间线</h2><p>哈希链只追加；失败不会因页面刷新而消失</p></div><ScrollText size={20} /></div>
      <div className="audit-table-head"><span>时间</span><span>影响</span><span>事件</span><span>摘要</span><span>证据</span></div>
      <div className="audit-entries">{events.map((event) => {
        const tone = severity(event.eventType);
        const summary = Object.entries(event.payload).slice(0, 2).map(([key, value]) => `${key}: ${String(value)}`).join(" · ") || "无附加字段";
        return <button key={event.id} onClick={() => setSelectedEvent(event)}><time>{formatTime(event.utcTime)}</time><StatusMark tone={tone}>{tone === "danger" ? "阻断" : tone === "warning" ? "警告" : tone === "healthy" ? "正常" : "信息"}</StatusMark><strong>{eventLabels[event.eventType] ?? event.eventType}</strong><span>{summary}</span><code>{shortId(event.eventHash, 10)}</code></button>;
      })}{events.length === 0 ? <div className="visual-empty"><Database /><span>还没有本地审计事件</span></div> : null}</div>
    </section>
    <div className="settings-grid">
      <section className="workspace-panel credential-settings" aria-labelledby="credential-title">
        <div className="panel-heading"><div><h2 id="credential-title">凭证与连接</h2><p>前端只读取“已配置/未配置”，永远看不到秘密原文</p></div><KeyRound size={20} /></div>
        <div className="settings-state"><div className={!status ? "unknown" : status.credentialConfigured ? "configured" : "missing"}>{status?.credentialConfigured ? <CheckCircle2 /> : <AlertTriangle />}<div><strong>{!status ? "凭证状态尚未从本地服务返回" : status.credentialConfigured ? "凭证已由本机保护" : "尚未配置凭证"}</strong><span>{status?.credentialStore ?? "Windows Credential Manager"}</span></div></div><button className="button secondary" disabled={!status?.credentialConfigured || testing} onClick={testConnection}><Wifi size={16} />{testing ? "检查中…" : "只读连接检查"}</button></div>
        {connectionResult ? <div className={`connection-proof ${connectionResult.policyValid ? "valid" : "invalid"}`} role="status"><div><StatusMark tone={connectionResult.public ? "healthy" : "danger"}>公共行情 {connectionResult.public ? "可达" : "失败"}</StatusMark><StatusMark tone={connectionResult.privateReachable ? "healthy" : "danger"}>账户 API {connectionResult.privateReachable ? "可达" : "失败"}</StatusMark><StatusMark tone={connectionResult.policyValid ? "healthy" : "danger"}>权限策略 {connectionResult.policyValid ? "通过" : "拒绝"}</StatusMark></div>{!connectionResult.policyValid ? <p><AlertTriangle size={16} />{connectionResult.policyReason ?? "当前凭证不满足该环境的安全策略"}</p> : <p><ShieldCheck size={16} />可达性与权限策略已分别核验；仍不代表已授权交易。</p>}</div> : null}
        <div className="credential-path"><code>{credentialHint}</code><button className="icon-button" aria-label="复制凭证管理入口" onClick={() => void navigator.clipboard.writeText(credentialHint)}><Clipboard size={17} /></button></div>
        <p className="boundary-note"><ShieldCheck size={17} />API Key 只应授予必要的读取与 Trade 权限，绝不能包含提现权限；Trade 本身仍是写操作。</p>
      </section>
      <section className="workspace-panel system-settings" aria-labelledby="system-title">
        <div className="panel-heading"><div><h2 id="system-title">系统与硬边界</h2><p>服务端重新校验，前端不能覆盖</p></div><Settings2 size={20} /></div>
        <dl className="evidence-list"><div><dt>应用版本</dt><dd>{status?.version ?? "—"}</dd></div><div><dt>监听地址</dt><dd><code>{status?.bind ?? "127.0.0.1"}</code></dd></div><div><dt>策略版本</dt><dd>{status?.policy.version ?? "—"}</dd></div><div><dt>单笔上限</dt><dd>{status?.policy.maxOrderNotionalUsdt ?? "—"} USDT</dd></div><div><dt>行情新鲜度</dt><dd>≤ {status?.policy.staleMarketSeconds ?? "—"} 秒</dd></div><div><dt>正式盘免责声明</dt><dd>不保证收益</dd></div></dl>
        <a className="document-link" href="https://github.com/mikutea/tideguard-okx-demo" target="_blank" rel="noreferrer">查看公开仓库与安全文档<ExternalLink size={15} /></a>
      </section>
    </div>
    {environmentStatus ? <EnvironmentSwitch status={environmentStatus} onChanged={onRefreshEnvironment} onNotice={onNotice} /> : <section className={`environment-status-unavailable ${environmentMode === "live" ? "live" : ""}`}><AlertTriangle size={24} /><div><h2>{environmentMode === "live" ? "最后确认环境为 LIVE；切换控制暂时锁定" : "环境权威状态尚未返回"}</h2><p>{environmentMode === "live" ? "LIVE 朱砂横幅与真实资金确认继续保留。等待服务端环境接口恢复后才能切换。" : "完全未知时不会默认 Demo，授权、预检和订单提交均保持锁定。"}</p></div></section>}
    <EvidenceDrawer open={Boolean(selectedEvent)} title={selectedEvent ? eventLabels[selectedEvent.eventType] ?? selectedEvent.eventType : "事件证据"} subtitle={selectedEvent ? `${formatTime(selectedEvent.utcTime)} · ${selectedEvent.actor}` : undefined} onClose={() => setSelectedEvent(null)}>
      {selectedEvent ? <><section className="drawer-verdict"><StatusMark tone={severity(selectedEvent.eventType)}>{severity(selectedEvent.eventType)}</StatusMark><h3>{eventLabels[selectedEvent.eventType] ?? selectedEvent.eventType}</h3><p>该事件来自本机只追加审计链。若属于订单终态、急停或身份异常，应以交易所回查和服务端 fail-closed 状态为准。</p></section><section className="drawer-section"><h3>事件负载</h3><dl className="payload-list">{Object.entries(selectedEvent.payload).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd></div>)}</dl></section><section className="drawer-section technical-evidence"><h3>链式标识</h3><dl className="evidence-list"><div><dt>Event ID</dt><dd>{selectedEvent.id}</dd></div><div><dt>Correlation</dt><dd><code>{selectedEvent.correlationId ?? "—"}</code></dd></div><div><dt>Event hash</dt><dd><code>{selectedEvent.eventHash}</code></dd></div></dl></section></> : null}
    </EvidenceDrawer>
  </div>;
}
