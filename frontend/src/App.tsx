import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, getStatus } from "./api";
import { ConfirmDialog } from "./components/Primitives";
import { Shell } from "./components/Shell";
import type { ExplanationMode, ViewKey } from "./components/Shell";
import { environmentFromSystemStatus, resolveEnvironmentMode } from "./lib/environment.js";
import { AuditSettingsPage } from "./pages/AuditSettingsPage";
import { DataPage } from "./pages/DataPage";
import { ExecutionPage } from "./pages/ExecutionPage";
import { ModelsPage } from "./pages/ModelsPage";
import { RuntimePage } from "./pages/RuntimePage";
import { TrainingPage } from "./pages/TrainingPage";
import type { AccountData, AuditEvent, DemoOrder, EnvironmentMode, EnvironmentStatus, MarketData, MLStatus, ResearchMonitorStatus, SystemStatus } from "./types";

type NoticeTone = "success" | "warning" | "error" | "info";
interface Notice { tone: NoticeTone; message: string }

const VIEW_KEYS = new Set<ViewKey>(["runtime", "data", "training", "models", "execution", "audit"]);

function initialView(): ViewKey {
  const view = new URLSearchParams(window.location.search).get("view") as ViewKey | null;
  return view && VIEW_KEYS.has(view) ? view : "runtime";
}

function normalizeEnvironment(value: EnvironmentStatus): EnvironmentStatus | null {
  const raw = value as unknown as Record<string, unknown>;
  const candidate = raw.activeEnvironment ?? raw.current ?? raw.environment ?? raw.mode;
  if (candidate !== "live" && candidate !== "demo") return null;
  return { ...value, activeEnvironment: candidate };
}

export default function App() {
  const [active, setActive] = useState<ViewKey>(initialView);
  const [explanationMode, setExplanationMode] = useState<ExplanationMode>("summary");
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [environmentStatus, setEnvironmentStatus] = useState<EnvironmentStatus | null>(null);
  const [lastKnownEnvironment, setLastKnownEnvironment] = useState<EnvironmentMode>("unknown");
  const [market, setMarket] = useState<MarketData | null>(null);
  const [account, setAccount] = useState<AccountData | null>(null);
  const [orders, setOrders] = useState<DemoOrder[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [chainValid, setChainValid] = useState<boolean | null>(null);
  const [ml, setML] = useState<MLStatus | null>(null);
  const [researchMonitor, setResearchMonitor] = useState<ResearchMonitorStatus | null>(null);
  const [connection, setConnection] = useState<"live" | "stale" | "offline" | "loading">("loading");
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [killDialog, setKillDialog] = useState(false);

  const notify = useCallback((tone: NoticeTone, message: string) => setNotice({ tone, message }), []);
  const environmentMode = resolveEnvironmentMode({ lastKnown: lastKnownEnvironment, systemStatus: status, environmentStatus });
  const transitionLocked = Boolean(
    environmentStatus?.transitionPending
    || environmentStatus?.restartRequired
    || environmentStatus?.operatingMode === "transition_locked"
    || status?.environmentProfile?.transitionPending
    || status?.environmentProfile?.restartRequired
    || status?.environmentProfile?.operatingMode === "transition_locked"
  );

  const refreshEnvironment = useCallback(async () => {
    try {
      const result = await api.getEnvironmentStatus();
      const normalized = normalizeEnvironment(result);
      if (normalized) {
        setEnvironmentStatus(normalized);
        setLastKnownEnvironment(normalized.activeEnvironment);
      }
    } catch {
      // Preserve the structured system status and last-known environment. Never downgrade to Demo.
    }
  }, []);

  const refresh = useCallback(async () => {
    if (document.visibilityState === "hidden") return;
    setRefreshing(true);
    try {
      const nextStatus = await getStatus();
      setStatus(nextStatus);
      const systemEnvironment = environmentFromSystemStatus(nextStatus);
      if (systemEnvironment) setLastKnownEnvironment(systemEnvironment);
      if (nextStatus.environmentProfile) {
        const normalizedProfile = normalizeEnvironment(nextStatus.environmentProfile);
        if (normalizedProfile) setEnvironmentStatus(normalizedProfile);
      }
      const [marketResult, accountResult, orderResult, auditResult, mlResult, environmentResult, researchResult] = await Promise.allSettled([
        api.getMarket(), api.getAccount(), api.getOrders(), api.getAudit(120), api.getMLStatus(), api.getEnvironmentStatus(), api.getResearchStatus()
      ]);
      let partial = false;
      if (marketResult.status === "fulfilled") setMarket(marketResult.value); else partial = true;
      if (accountResult.status === "fulfilled") setAccount(accountResult.value); else partial = true;
      if (orderResult.status === "fulfilled") setOrders(orderResult.value); else partial = true;
      if (auditResult.status === "fulfilled") { setEvents(auditResult.value.events); setChainValid(auditResult.value.chainValid); } else partial = true;
      if (mlResult.status === "fulfilled") setML(mlResult.value); else partial = true;
      if (researchResult.status === "fulfilled") setResearchMonitor(researchResult.value); else partial = true;
      if (environmentResult.status === "fulfilled") {
        const normalized = normalizeEnvironment(environmentResult.value);
        if (normalized) {
          setEnvironmentStatus(normalized);
          setLastKnownEnvironment(normalized.activeEnvironment);
        }
      }
      setConnection(partial ? "stale" : "live");
      setLastUpdated(new Date().toISOString());
    } catch (error) {
      setConnection((current) => current === "loading" ? "offline" : "stale");
      notify("error", error instanceof Error ? error.message : "本地服务不可用；已保留最后有效数据");
    } finally {
      setRefreshing(false);
    }
  }, [notify]);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 10_000);
    const resume = () => document.visibilityState === "visible" && void refresh();
    document.addEventListener("visibilitychange", resume);
    return () => { window.clearInterval(interval); document.removeEventListener("visibilitychange", resume); };
  }, [refresh]);

  useEffect(() => {
    if (!notice || notice.tone === "error") return;
    const timeout = window.setTimeout(() => setNotice(null), 6_000);
    return () => window.clearTimeout(timeout);
  }, [notice]);

  const navigate = useCallback((view: ViewKey) => {
    setActive(view);
    const url = new URL(window.location.href);
    url.searchParams.set("view", view);
    window.history.replaceState(null, "", url);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const runAction = useCallback(async (action: () => Promise<unknown>, success: string) => {
    setActionBusy(true);
    try { await action(); notify("success", success); await refresh(); }
    catch (error) { notify("error", error instanceof Error ? error.message : "操作失败"); }
    finally { setActionBusy(false); }
  }, [notify, refresh]);

  const emergencyStop = async () => {
    setActionBusy(true);
    try {
      const result = await api.kill();
      setKillDialog(false);
      notify(result.failures > 0 ? "warning" : "success", `急停已触发；撤单请求 ${result.acceptedCancelRequests}，失败 ${result.failures}`);
      await refresh();
    } catch (error) { notify("error", error instanceof Error ? error.message : "急停请求失败"); }
    finally { setActionBusy(false); }
  };

  const content = useMemo(() => {
    if (active === "data") return <DataPage ml={ml} researchMonitor={researchMonitor} refreshing={refreshing} onRefresh={refresh} />;
    if (active === "training") return <TrainingPage ml={ml} busy={actionBusy} onTrain={() => runAction(() => api.trainAutonomy(), "新训练批次已启动；不会直接晋级或下单")} />;
    if (active === "models") return <ModelsPage ml={ml} explanationMode={explanationMode} />;
    if (active === "execution") return <ExecutionPage environment={environmentMode} environmentStatus={environmentStatus} transitionLocked={transitionLocked} status={status} market={market} account={account} orders={orders} ml={ml} busy={actionBusy} onEnableMaster={(phrase) => runAction(() => api.enableAutonomy(phrase), "长期 Demo master 已启用；无 champion 与 lease 仍不会开仓")} onDisableMaster={() => runAction(() => api.disableAutonomy("用户通过墨衡运行中心停止新开仓"), "已停止新开仓；已有模型仓位继续退出管理")} onRefresh={refresh} onNotice={notify} />;
    if (active === "audit") return <AuditSettingsPage status={status} environmentStatus={environmentStatus} environmentMode={environmentMode} events={events} chainValid={chainValid} onRefreshEnvironment={refreshEnvironment} onRefresh={refresh} onNotice={notify} />;
    return <RuntimePage status={status} market={market} account={account} ml={ml} events={events} explanationMode={explanationMode} onNavigate={navigate} />;
  }, [account, actionBusy, active, chainValid, environmentMode, environmentStatus, events, explanationMode, market, ml, navigate, notify, orders, refresh, refreshEnvironment, refreshing, researchMonitor, runAction, status, transitionLocked]);

  return <>
    <Shell active={active} onNavigate={navigate} explanationMode={explanationMode} onExplanationMode={setExplanationMode} environment={environmentMode} transitionLocked={transitionLocked} status={status} connection={connection} lastUpdated={lastUpdated} onEmergency={() => setKillDialog(true)}>{content}</Shell>
    <ConfirmDialog open={killDialog} title="立即停止新订单并撤销本程序挂单？" description={<><p>急停会禁止后续新订单，并尝试撤销墨衡标记的活动挂单。</p><p>它不会逆转已经成交的资产；已有模型自有仓位仍需安全退出。</p></>} confirmLabel="触发紧急停止" danger busy={actionBusy} onClose={() => setKillDialog(false)} onConfirm={() => void emergencyStop()} />
    {notice ? <div className={`notice-toast ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>{notice.tone === "success" ? <CheckCircle2 size={19} /> : notice.tone === "info" ? <Info size={19} /> : <AlertTriangle size={19} />}<span>{notice.message}</span><button aria-label="关闭通知" onClick={() => setNotice(null)}><X size={17} /></button></div> : null}
  </>;
}
