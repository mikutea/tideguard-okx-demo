import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleStop,
  Clipboard,
  Database,
  Eye,
  FlaskConical,
  KeyRound,
  LayoutDashboard,
  ListOrdered,
  LockKeyhole,
  Play,
  RefreshCw,
  ScrollText,
  Settings,
  ShieldCheck,
  Wifi,
  X,
  XCircle
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, getStatus } from "./api";
import type {
  AccountData,
  AuditEvent,
  DemoOrder,
  MarketData,
  MLStatus,
  PreviewResult,
  RiskCheck,
  SystemStatus
} from "./types";

type NavKey = "overview" | "market" | "lab" | "orders" | "audit" | "settings";

const navItems: Array<{ key: NavKey; label: string; icon: typeof LayoutDashboard }> = [
  { key: "overview", label: "总览", icon: LayoutDashboard },
  { key: "market", label: "市场", icon: BarChart3 },
  { key: "lab", label: "策略实验室", icon: FlaskConical },
  { key: "orders", label: "订单", icon: ListOrdered },
  { key: "audit", label: "审计日志", icon: ScrollText },
  { key: "settings", label: "设置", icon: Settings }
];

const eventLabels: Record<string, string> = {
  "system.started": "本地服务已启动",
  "safety.armed": "用户启用演练",
  "safety.disarmed": "演练已锁定",
  "safety.kill_engaged": "急停已触发",
  "safety.kill_reset": "急停已解除",
  "safety.cancel_attempt_complete": "撤单尝试已完成",
  "safety.deadman_failed": "失联撤单心跳失败",
  "connection.tested": "连接检查已完成",
  "order.previewed": "订单预检已通过",
  "risk.rejected": "风控拒绝预检",
  "risk.rejected_at_commit": "提交前风控拒绝",
  "order.dispatching": "模拟订单正在提交",
  "order.accepted": "OKX 模拟盘已接受订单",
  "order.uncertain": "订单状态不确定，正在回查",
  "order.reconciled": "订单已按编号完成回查",
  "order.rejected": "OKX 模拟盘拒绝订单",
  "order.transport_error": "下单传输失败",
  "order.lifecycle_error": "订单生命周期异常",
  "order.recovery_required": "启动时发现未决订单"
};

function numeric(value: string | number | null | undefined): number | null {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatNumber(value: string | number | null | undefined, digits = 2): string {
  const parsed = numeric(value);
  if (parsed === null) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  }).format(parsed);
}

function formatCompact(value: string | number | null | undefined): string {
  const parsed = numeric(value);
  if (parsed === null) return "—";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 2 }).format(parsed);
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = /^\d+$/.test(value) ? new Date(Number(value)) : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(date);
}

function marketAge(ts?: string): number | null {
  if (!ts) return null;
  const age = (Date.now() - Number(ts)) / 1000;
  return Number.isFinite(age) ? Math.max(0, age) : null;
}

function StatusDot({ tone = "healthy" }: { tone?: "healthy" | "warning" | "danger" | "muted" }) {
  return <span className={`status-dot ${tone}`} aria-hidden="true" />;
}

function ChainState({ value, error }: { value: boolean | null; error?: string | null }) {
  if (error || value === null) return <span className="chain-state pending"><AlertTriangle size={15} />审计待检查</span>;
  return <span className={`chain-state ${value ? "valid" : "invalid"}`}>{value ? <CheckCircle2 size={15} /> : <XCircle size={15} />}{value ? "哈希链有效" : "哈希链异常"}</span>;
}

function LineChart({ market }: { market: MarketData | null }) {
  const geometry = useMemo(() => {
    const rows = market?.candles ?? [];
    const closes = rows.map((item) => Number(item.close)).filter(Number.isFinite);
    if (closes.length < 2) return null;
    const width = 900;
    const height = 300;
    const chartBottom = 242;
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const range = max - min || 1;
    const points = closes.map((value, index) => ({
      x: (index / (closes.length - 1)) * width,
      y: 18 + ((max - value) / range) * (chartBottom - 36)
    }));
    const line = points.map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ");
    const area = `${line} L${width},${chartBottom} L0,${chartBottom} Z`;
    const volumes = rows.map((item) => Number(item.volume) || 0);
    const maxVolume = Math.max(...volumes, 1);
    return { width, height, chartBottom, min, max, points, line, area, volumes, maxVolume };
  }, [market]);

  if (!geometry) {
    return (
      <div className="chart-empty" role="status">
        <Activity size={24} />
        <span>等待 OKX 公共行情</span>
      </div>
    );
  }

  const last = geometry.points.at(-1)!;
  return (
    <div className="chart-wrap">
      <svg
        className="market-chart"
        viewBox={`0 0 ${geometry.width} ${geometry.height}`}
        role="img"
        aria-label={`BTC/USDT 最近 96 根五分钟 K 线的收盘价折线，最低 ${formatNumber(geometry.min, 1)}，最高 ${formatNumber(geometry.max, 1)}`}
      >
        {[40, 90, 140, 190, 240].map((y) => (
          <line key={y} x1="0" y1={y} x2={geometry.width} y2={y} className="grid-line" />
        ))}
        <path d={geometry.area} className="chart-area" />
        <path d={geometry.line} className="chart-line" />
        <line x1="0" y1={last.y} x2={geometry.width} y2={last.y} className="last-line" />
        <circle cx={last.x} cy={last.y} r="4" className="last-dot" />
        {geometry.volumes.map((volume, index) => {
          const barWidth = geometry.width / geometry.volumes.length;
          const barHeight = (volume / geometry.maxVolume) * 38;
          return (
            <rect
              key={index}
              x={index * barWidth + 1}
              y={geometry.height - barHeight}
              width={Math.max(1, barWidth - 2)}
              height={barHeight}
              className="volume-bar"
            />
          );
        })}
      </svg>
      <div className="chart-scale" aria-hidden="true">
        <span>{formatNumber(geometry.max, 1)}</span>
        <span>{formatNumber((geometry.max + geometry.min) / 2, 1)}</span>
        <span>{formatNumber(geometry.min, 1)}</span>
      </div>
    </div>
  );
}

function Sidebar({ active, onChange }: { active: NavKey; onChange: (key: NavKey) => void }) {
  return (
    <aside className="sidebar" aria-label="主导航">
      <div className="brand-block">
        <span className="brand-cn">潮汐台</span>
        <span className="brand-en">TIDEGUARD</span>
      </div>
      <nav className="nav-list">
        {navItems.map(({ key, label, icon: Icon }) => (
          <button
            className={`nav-button ${active === key ? "active" : ""}`}
            key={key}
            onClick={() => onChange(key)}
            aria-current={active === key ? "page" : undefined}
          >
            <Icon size={19} strokeWidth={1.8} />
            <span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <ShieldCheck size={18} />
        <span>本地 · Demo only</span>
      </div>
    </aside>
  );
}

function Topbar({ status, market, latency }: { status: SystemStatus | null; market: MarketData | null; latency: number | null }) {
  const age = marketAge(market?.ticker.ts);
  const stale = age !== null && status ? age > status.policy.staleMarketSeconds : false;
  const modeLabel = status?.safety.mode === "armed" ? "演练已启用" : status?.safety.mode === "killed" ? "急停锁定" : "观察模式";
  return (
    <header className="topbar">
      <div className="safety-badges">
        <span className="demo-badge">OKX 模拟盘</span>
        <span className={`mode-badge ${status?.safety.mode ?? "observe"}`}>{modeLabel}</span>
      </div>
      <div className="health-line">
        <span className="health-item"><StatusDot tone={stale ? "warning" : market ? "healthy" : "muted"} />{market ? "连接健康" : "连接中"}</span>
        <span className="health-item"><Wifi size={15} />行情请求 {latency === null ? "—" : `${latency} ms`}</span>
        <span className="health-item"><KeyRound size={15} />{status?.credentialConfigured ? "凭证已在本机保护" : "凭证未配置"}</span>
        <time className="local-time" dateTime={new Date().toISOString()}>{new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date())}</time>
      </div>
    </header>
  );
}

function Modal({
  title,
  description,
  requiredPhrase,
  confirmLabel,
  tone = "safe",
  busy,
  onClose,
  onConfirm
}: {
  title: string;
  description: React.ReactNode;
  requiredPhrase?: string;
  confirmLabel: string;
  tone?: "safe" | "danger";
  busy?: boolean;
  onClose: () => void;
  onConfirm: (phrase: string) => void;
}) {
  const [phrase, setPhrase] = useState("");
  const enabled = !requiredPhrase || phrase === requiredPhrase;
  const dialogRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'));
    (dialog.querySelector<HTMLElement>("[autofocus]") ?? focusable()[0] ?? dialog).focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previous?.focus();
    };
  }, [onClose]);
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabIndex={-1}>
        <button className="icon-button modal-close" aria-label="关闭" onClick={onClose}><X size={18} /></button>
        <div className={`modal-icon ${tone}`} aria-hidden="true">{tone === "danger" ? <AlertTriangle /> : <ShieldCheck />}</div>
        <h2 id="modal-title">{title}</h2>
        <div className="modal-copy">{description}</div>
        {requiredPhrase && (
          <label className="field-label modal-field">
            <span>输入 <strong>{requiredPhrase}</strong> 确认</span>
            <input autoFocus value={phrase} onChange={(event) => setPhrase(event.target.value)} autoComplete="off" />
          </label>
        )}
        <div className="modal-actions">
          <button className="button secondary" onClick={onClose}>取消</button>
          <button className={`button ${tone === "danger" ? "danger" : "primary"}`} disabled={!enabled || busy} onClick={() => onConfirm(phrase)}>
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}

function MarketPanel({ market, error, onRefresh }: { market: MarketData | null; error: string | null; onRefresh: () => void }) {
  const last = numeric(market?.ticker.last);
  const open = numeric(market?.ticker.open24h);
  const change = last !== null && open && open !== 0 ? ((last - open) / open) * 100 : null;
  const age = marketAge(market?.ticker.ts);
  return (
    <section className="surface market-surface" aria-labelledby="market-title">
      <div className="market-heading">
        <div>
          <div className="instrument-line">
            <h1 id="market-title">BTC / USDT</h1>
            <span className="source-tag">{market?.source ?? "公共行情"}</span>
          </div>
          <p className="instrument-sub">现货 · cash · 5 分钟</p>
        </div>
        <button className="icon-button" aria-label="刷新行情" onClick={onRefresh}><RefreshCw size={17} /></button>
      </div>
      {error ? (
        <div className="inline-warning"><AlertTriangle size={17} />{error}</div>
      ) : (
        <>
          <div className="quote-strip">
            <div className="last-price">{formatNumber(market?.ticker.last, 1)}</div>
            <div className={`change ${change !== null && change < 0 ? "negative" : "positive"}`}>{change === null ? "—" : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`}</div>
            <dl className="quote-stats">
              <div><dt>24h 高</dt><dd>{formatNumber(market?.ticker.high24h, 1)}</dd></div>
              <div><dt>24h 低</dt><dd>{formatNumber(market?.ticker.low24h, 1)}</dd></div>
              <div><dt>24h 成交量</dt><dd>{formatCompact(market?.ticker.volume24h)} BTC</dd></div>
              <div><dt>数据年龄</dt><dd>{age === null ? "—" : `${age.toFixed(1)} 秒`}</dd></div>
            </dl>
          </div>
          <div className="chart-toolbar">
            <span className="chart-title">公共行情</span>
            <div className="range-list" aria-label="图表周期"><button className="active">5m</button><button disabled>15m</button><button disabled>1H</button><button disabled>4H</button></div>
          </div>
          <LineChart market={market} />
        </>
      )}
    </section>
  );
}

function ArmControl({ status, onRefresh, onError }: { status: SystemStatus | null; onRefresh: () => Promise<void>; onError: (message: string) => void }) {
  const [modal, setModal] = useState<"arm" | "reset" | null>(null);
  const [busy, setBusy] = useState(false);
  const mode = status?.safety.mode ?? "observe";

  const run = async (phrase: string) => {
    setBusy(true);
    try {
      if (modal === "arm") await api.arm(phrase);
      if (modal === "reset") await api.resetKill(phrase);
      setModal(null);
      await onRefresh();
    } catch (error) {
      onError(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const disarm = async () => {
    try {
      await api.disarm();
      await onRefresh();
    } catch (error) {
      onError(error instanceof Error ? error.message : "锁定失败");
    }
  };

  return (
    <section className="surface arm-surface" aria-labelledby="arm-title">
      <div className="section-heading compact"><div><h2 id="arm-title">本地启用流程</h2><p>服务重启、超时或显式锁定后回到观察</p></div></div>
      <div className="arm-steps" aria-label="当前本地安全状态">
        <div className={`arm-step ${mode === "observe" ? "active" : "done"}`}><span>1</span><strong>观察</strong><small>只读不下单</small></div>
        <ChevronRight size={17} aria-hidden="true" />
        <div className={`arm-step ${mode === "armed" ? "active" : ""}`}><span>2</span><strong>演练</strong><small>{mode === "armed" ? `${status?.safety.armedRemainingSeconds ?? 0}s` : "限时授权"}</small></div>
        <ChevronRight size={17} aria-hidden="true" />
        <div className={`arm-step ${mode === "killed" ? "danger active" : ""}`}><span>3</span><strong>锁定</strong><small>停止新单</small></div>
      </div>
      {mode === "armed" ? (
        <button className="button secondary full" onClick={disarm}><LockKeyhole size={17} />立即锁定</button>
      ) : mode === "killed" ? (
        <button className="button secondary full" onClick={() => setModal("reset")}>核对后解除急停</button>
      ) : (
        <button className="button primary full" disabled={!status?.credentialConfigured} onClick={() => setModal("arm")}><ShieldCheck size={17} />启用 10 分钟演练</button>
      )}
      {!status?.credentialConfigured && <p className="helper warning-text">先在本机配置模拟盘凭证才能启用。</p>}
      {modal === "arm" && (
        <Modal title="启用模拟盘演练" description={<><p>这只允许在未来 10 分钟内提交通过确定性风控的 OKX 模拟盘现货单。</p><p>程序会同时启用失联撤单保护。</p></>} requiredPhrase="DEMO" confirmLabel="启用演练" busy={busy} onClose={() => setModal(null)} onConfirm={run} />
      )}
      {modal === "reset" && (
        <Modal title="解除模拟盘急停" description="解除前程序会确认本程序没有仍在活动的模拟挂单；解除后仍保持观察模式。" requiredPhrase="解除模拟盘急停" confirmLabel="解除并保持观察" busy={busy} onClose={() => setModal(null)} onConfirm={run} />
      )}
    </section>
  );
}

function OrderTicket({
  status,
  market,
  onPreview,
  onRefresh,
  onError
}: {
  status: SystemStatus | null;
  market: MarketData | null;
  onPreview: (value: PreviewResult | null) => void;
  onRefresh: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [ordType, setOrdType] = useState<"limit" | "post_only">("limit");
  const [price, setPrice] = useState("");
  const [size, setSize] = useState("");
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [commitKey, setCommitKey] = useState(() => crypto.randomUUID());
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  useEffect(() => {
    if (!price && market?.ticker.last) setPrice(market.ticker.last);
  }, [market?.ticker.last, price]);

  const clearPreview = () => {
    setPreview(null);
    setConfirmed(false);
    setResult(null);
    setCommitKey(crypto.randomUUID());
    onPreview(null);
  };

  const notional = numeric(price) !== null && numeric(size) !== null ? Number(price) * Number(size) : null;
  const armed = status?.safety.mode === "armed";

  const runPreview = async () => {
    setBusy(true);
    setResult(null);
    try {
      const next = await api.preview({ instId: "BTC-USDT", side, ordType, price, size });
      setPreview(next);
      setCommitKey(crypto.randomUUID());
      onPreview(next);
    } catch (error) {
      onError(error instanceof Error ? error.message : "预检失败");
    } finally {
      setBusy(false);
    }
  };

  const commit = async () => {
    if (!preview || !confirmed) return;
    setBusy(true);
    try {
      const response = await api.commit(preview, commitKey);
      setResult(`状态：${response.status}${response.ordId ? ` · 订单 ${response.ordId}` : ""}`);
      setPreview(null);
      onPreview(null);
      setConfirmed(false);
      await onRefresh();
    } catch (error) {
      onError(error instanceof Error ? error.message : "提交失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="surface order-ticket" aria-labelledby="ticket-title">
      <div className="section-heading compact">
        <div><h2 id="ticket-title">模拟下单</h2><p>仅现货 cash · 不发送至实盘</p></div>
        <Eye size={17} aria-hidden="true" />
      </div>
      <div className="side-tabs" role="group" aria-label="订单方向">
        <button className={side === "buy" ? "active buy" : ""} onClick={() => { setSide("buy"); clearPreview(); }}>买入</button>
        <button className={side === "sell" ? "active sell" : ""} onClick={() => { setSide("sell"); clearPreview(); }}>卖出</button>
      </div>
      <label className="field-label"><span>订单类型</span><select value={ordType} onChange={(event) => { setOrdType(event.target.value as "limit" | "post_only"); clearPreview(); }}><option value="limit">限价</option><option value="post_only">只做 Maker</option></select></label>
      <label className="field-label"><span>价格 <em>USDT</em></span><input inputMode="decimal" value={price} onChange={(event) => { setPrice(event.target.value); clearPreview(); }} placeholder="等待公共行情" /></label>
      <label className="field-label"><span>数量 <em>BTC</em></span><input inputMode="decimal" value={size} onChange={(event) => { setSize(event.target.value); clearPreview(); }} placeholder="0.00000" /></label>
      <div className="estimate-row"><span>预计金额</span><strong>{notional === null || !Number.isFinite(notional) ? "—" : `${formatNumber(notional, 4)} USDT`}</strong></div>
      {!preview ? (
        <button className="button primary full" disabled={!armed || !price || !size || busy} onClick={runPreview}>
          <ShieldCheck size={17} />{busy ? "检查中…" : armed ? `模拟${side === "buy" ? "买入" : "卖出"}预检` : "先启用演练"}
        </button>
      ) : preview.decision.allowed ? (
        <div className="commit-box">
          <div className="commit-title"><CheckCircle2 size={18} />风控允许</div>
          <p>{preview.order.side === "buy" ? "买入" : "卖出"} {preview.order.size} BTC · {formatNumber(preview.notionalUsdt, 4)} USDT</p>
          <label className="confirm-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>我确认这是 OKX 模拟盘订单</span></label>
          <button className="button primary full" disabled={!confirmed || busy} onClick={commit}>{busy ? "提交中…" : "确认发送模拟盘订单"}</button>
          <button className="text-button" onClick={clearPreview}>放弃本次预检</button>
        </div>
      ) : (
        <div className="commit-box rejected"><div className="commit-title"><XCircle size={18} />风控拒绝</div><p>{preview.decision.reasonCodes.join(" · ")}</p><button className="text-button" onClick={clearPreview}>修改订单</button></div>
      )}
      {result && <div className="inline-success" role="status"><Check size={16} />{result}</div>}
    </section>
  );
}

function RiskPanel({ status, preview, onRefresh, onError }: { status: SystemStatus | null; preview: PreviewResult | null; onRefresh: () => Promise<void>; onError: (message: string) => void }) {
  const [killOpen, setKillOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const baseline: RiskCheck[] = status
      ? [
         { key: "single", label: "单笔金额", passed: false, current: "待订单预检", limit: `≤ ${status.policy.maxOrderNotionalUsdt} USDT`, reason: "另受模拟权益 0.10% 限制" },
        { key: "fresh", label: "行情新鲜度", passed: false, current: "待订单预检", limit: `≤ ${status.policy.staleMarketSeconds} 秒`, reason: "行情超时会拒绝下单" },
        { key: "deviation", label: "限价偏离", passed: false, current: "待订单预检", limit: `≤ ${Number(status.policy.maxPriceDeviation) * 100}%`, reason: "相对最新价的绝对偏离" },
        { key: "orders", label: "未完成订单", passed: false, current: "待账户同步", limit: `< ${status.policy.maxOpenOrders}`, reason: "预检时读取模拟账户挂单" },
        { key: "spot", label: "仅现货 / 零杠杆", passed: true, current: "BTC-USDT", limit: "SPOT + cash", reason: "服务端不可覆盖" }
      ]
    : [];
  const checks = preview?.decision.checks ?? baseline;
  const allowed = preview?.decision.allowed;

  const kill = async () => {
    setBusy(true);
    try {
      await api.kill();
      setKillOpen(false);
      await onRefresh();
    } catch (error) {
      onError(error instanceof Error ? error.message : "急停失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="surface risk-surface" aria-labelledby="risk-title">
      <div className="section-heading compact">
        <div><h2 id="risk-title">确定性风控</h2><p>策略与模型不能绕过</p></div>
        <span className={`risk-summary ${allowed === true ? "pass" : allowed === false ? "fail" : "pending"}`}>{allowed === true ? "全部通过" : allowed === false ? "已拒绝" : "等待预检"}</span>
      </div>
      <div className="risk-list">
        {checks.slice(0, 8).map((check) => (
          <div className="risk-row" key={check.key} title={check.reason}>
            {preview ? check.passed ? <CheckCircle2 className="ok" size={17} /> : <XCircle className="bad" size={17} /> : check.passed ? <CheckCircle2 className="ok" size={17} /> : <AlertTriangle className="wait" size={17} />}
            <div><strong>{check.label}</strong><small>{check.current}</small></div>
            <span>{check.limit}</span>
          </div>
        ))}
      </div>
      <button className="button emergency full" onClick={() => setKillOpen(true)}><CircleStop size={18} />立即停止并撤单</button>
      <p className="helper danger-text">阻止新单并尝试撤销本程序挂单；不会逆转已成交结果。</p>
      {killOpen && (
        <Modal
          title="触发模拟盘急停"
          description={<ul className="modal-list"><li>立即禁止后续新订单</li><li>尝试撤销本程序的活动挂单</li><li>保留审计与连接检查</li><li>不会逆转已经成交的模拟资产</li></ul>}
          confirmLabel="确认急停并撤单"
          tone="danger"
          busy={busy}
          onClose={() => setKillOpen(false)}
          onConfirm={kill}
        />
      )}
    </section>
  );
}

function OrdersTable({ orders, configured, error }: { orders: DemoOrder[]; configured: boolean; error?: string | null }) {
  return (
    <section className="surface orders-surface" aria-labelledby="orders-title">
      <div className="section-heading"><div><h2 id="orders-title">当前挂单</h2><p>{error ? "本轮数据不可用" : configured ? "OKX 模拟账户 · 仅本程序 tag" : "尚未配置模拟盘凭证"}</p></div><span className="count-label">{error ? "—" : `${orders.length} 笔`}</span></div>
      {error || orders.length === 0 ? (
        <div className="empty-row"><ListOrdered size={20} /><span>{error ? `挂单读取失败：${error}` : configured ? "没有本程序活动挂单" : "配置凭证后才会读取模拟账户挂单"}</span></div>
      ) : (
        <div className="table-scroll"><table><thead><tr><th>时间</th><th>交易对</th><th>方向</th><th>类型</th><th className="number">价格</th><th className="number">数量</th><th>状态</th><th>clOrdId</th></tr></thead><tbody>{orders.map((order) => <tr key={order.clOrdId}><td>{formatTime(order.createdAt)}</td><td>{order.instId}</td><td className={order.side}>{order.side === "buy" ? "买入" : "卖出"}</td><td>{order.ordType}</td><td className="number">{order.price}</td><td className="number">{order.size}</td><td><span className="order-state">{order.state}</span></td><td className="mono muted">{order.clOrdId}</td></tr>)}</tbody></table></div>
      )}
    </section>
  );
}

function AuditRail({ events, chainValid, error }: { events: AuditEvent[]; chainValid: boolean | null; error?: string | null }) {
  return (
    <section className="surface audit-rail" aria-labelledby="audit-rail-title">
      <div className="section-heading"><div><h2 id="audit-rail-title">审计流</h2><p>本地只追加事件 · 敏感字段脱敏</p></div><ChainState value={chainValid} error={error} /></div>
      {error || events.length === 0 ? (
        <div className="empty-row"><Database size={20} /><span>{error ? `审计读取失败：${error}` : "还没有本地事件"}</span></div>
      ) : (
        <div className="event-rail">{events.slice(0, 4).map((event, index) => <div className="event-item" key={event.id}><span className="event-node"><Check size={13} /></span><div><time>{formatTime(event.utcTime)}</time><strong>{eventLabels[event.eventType] ?? event.eventType}</strong><small>{index === 0 ? "最新本地事件" : `哈希 ${event.eventHash.slice(0, 8)}`}</small></div></div>)}</div>
      )}
    </section>
  );
}

function AccountStrip({ account, error }: { account: AccountData | null; error?: string | null }) {
  if (error) return <div className="account-empty"><AlertTriangle size={18} /><div><strong>模拟账户数据不可用</strong><span>{error}</span></div></div>;
  if (!account?.configured) return <div className="account-empty"><KeyRound size={18} /><div><strong>尚无模拟账户数据</strong><span>凭证只从 Windows Credential Manager 读取</span></div></div>;
  return (
    <section className="account-strip" aria-label="模拟账户摘要">
      <div><span>模拟权益</span><strong>{formatNumber(account.equityUsdt, 2)} USDT</strong></div>
      {account.balances.map((balance) => <div key={balance.currency}><span>{balance.currency} 可用</span><strong>{formatNumber(balance.available, balance.currency === "BTC" ? 6 : 2)}</strong></div>)}
      <div><span>数据来源</span><strong>{account.source}</strong></div>
    </section>
  );
}

function SettingsView({ status, onError }: { status: SystemStatus | null; onError: (message: string) => void }) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const command = ".\\.venv\\Scripts\\python.exe -m okx_demo_lab.cli credentials set";
  const testConnection = async () => {
    setTesting(true);
    try {
      const result = await api.testConnection();
      setTestResult(`公共 API ${result.public ? "通过" : "失败"} · 私有 Demo ${result.private ? "通过" : "未连接"}`);
    } catch (error) {
      onError(error instanceof Error ? error.message : "连接检查失败");
    } finally {
      setTesting(false);
    }
  };
  return (
    <main className="page-view settings-view">
      <div className="page-title"><div><p>本机配置</p><h1>设置与安全边界</h1></div></div>
      <section className="surface settings-section"><div className="settings-icon"><KeyRound /></div><div className="settings-copy"><h2>Windows Credential Manager</h2><p>前端只知道“已配置 / 未配置”，看不到 API Key、Secret 或 Passphrase。程序拒绝明文文件降级。</p><div className="code-row"><code>{command}</code><button className="icon-button" aria-label="复制凭证设置命令" onClick={() => navigator.clipboard.writeText(command)}><Clipboard size={17} /></button></div></div><span className={`settings-status ${status?.credentialConfigured ? "ok" : "pending"}`}>{status?.credentialConfigured ? "已配置" : "未配置"}</span></section>
      <section className="surface settings-section"><div className="settings-icon"><ShieldCheck /></div><div className="settings-copy"><h2>不可更改的执行边界</h2><p>固定 `openapi.okx.com`、`x-simulated-trading: 1`、`BTC-USDT`、`SPOT` 和 `cash`。没有实盘开关、杠杆、转账或提现 API。</p><div className="token-row"><span>策略 {status?.policy.version ?? "—"}</span><span>绑定 {status?.bind ?? "127.0.0.1"}</span><span>审计链 {status?.auditChainValid ? "有效" : "待检查"}</span></div></div></section>
      <section className="surface settings-section"><div className="settings-icon"><Wifi /></div><div className="settings-copy"><h2>只读连接检查</h2><p>检查公共时间接口与模拟账户鉴权，不下单、不撤单。</p>{testResult && <div className="inline-success"><Check size={16} />{testResult}</div>}</div><button className="button secondary" disabled={testing || !status?.credentialConfigured} onClick={testConnection}>{testing ? "检查中…" : "运行检查"}</button></section>
    </main>
  );
}

const gateLabels: Record<string, string> = {
  unsupported_evaluation_semantics: "旧版 long/short 重叠验证不可晋级",
  insufficient_folds: "Walk-forward 折数不足",
  insufficient_oos_rows: "样本外行数不足",
  insufficient_trades: "样本外信号数不足",
  cost_assumption_too_low: "成本假设过低",
  aggregate_accuracy_below_gate: "long / flat 决策准确率未过门",
  aggregate_net_return_below_gate: "固定周期 OOS 诊断净值未过门",
  worst_fold_below_gate: "最差固定周期 OOS 窗口未过门",
  drawdown_above_gate: "最大回撤超限",
  validation_missing: "缺少绑定验证"
};

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

function StrategyLab({ status, onRefresh, onError }: { status: SystemStatus | null; onRefresh: () => Promise<void>; onError: (message: string) => void }) {
  const [ml, setML] = useState<MLStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [selectedModel, setSelectedModel] = useState("");
  const [reviewer, setReviewer] = useState("local-user");
  const [rationale, setRationale] = useState("");
  const [promotionPhrase, setPromotionPhrase] = useState("");
  const [automationPhrase, setAutomationPhrase] = useState("");

  const refreshML = useCallback(async () => {
    try {
      const next = await api.getMLStatus();
      setML(next);
      if (!selectedModel) {
        const ready = next.models.find((model) => model.state === "validated" && model.gateFailures.length === 0);
        if (ready) setSelectedModel(ready.modelId);
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : "模型状态读取失败");
    } finally {
      setLoading(false);
    }
  }, [onError, selectedModel]);

  useEffect(() => {
    void refreshML();
    const interval = window.setInterval(() => void refreshML(), 5_000);
    return () => window.clearInterval(interval);
  }, [refreshML]);

  const run = async (name: string, action: () => Promise<unknown>, success?: string) => {
    setBusy(name);
    try {
      await action();
      if (success) onError(success);
      await Promise.all([refreshML(), onRefresh()]);
    } catch (error) {
      onError(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  const selected = ml?.models.find((model) => model.modelId === selectedModel) ?? null;
  const activePermit = ml?.automation.permit?.active ? ml.automation.permit : null;
  const canPromote = Boolean(selected && selected.state === "validated" && selected.gateFailures.length === 0);
  const canAuthorize = Boolean(ml?.champion && status?.credentialConfigured && status.safety.mode === "armed");

  return (
    <main className="page-view lab-page">
      <div className="page-title"><div><p>离线训练 · 人工晋级 · Demo 执行</p><h1>策略实验室</h1></div><button className="button secondary" disabled={loading || Boolean(busy)} onClick={() => void refreshML()}><RefreshCw size={15} />刷新</button></div>
      <section className="lab-hero surface"><Bot size={32} /><div><h2>模型已经接入，但没有收益承诺</h2><p>本机用 OKX 公共、已完成的 BTC-USDT 5 分钟 K 线训练数据模型；时间隔离验证通过后仍需你逐字确认人工晋级。固定周期 long-only OOS 只是研究诊断，不代表部署后收益。v0.2 自动路径只允许一笔 10 USDT Demo BUY 入场，不会自动卖出，退出必须人工处理。</p></div><div className="lab-engine"><span>{ml?.engine.name ?? "加载中"}</span><small>严格 JSON · long-only 研究诊断</small></div></section>
      <div className="lab-flow"><div><span>01</span><strong>公共历史 K 线</strong><p>只读、已确认、5m 连续时间轴。</p></div><ChevronRight /><div><span>02</span><strong>Long-only OOS</strong><p>固定 12 根持有、非重叠资本、计入双边 12 bps。</p></div><ChevronRight /><div><span>03</span><strong>人工 champion</strong><p>门槛、哈希与代次 CAS 全部复核。</p></div><ChevronRight /><div><span>04</span><strong>单次 Demo 入场</strong><p>≤10 分钟、仅 1 笔 BUY、10 USDT。</p></div></div>

      <section className="lab-grid">
        <article className="surface lab-card lab-train-card">
          <div className="lab-card-head"><div><span className="eyebrow">离线训练</span><h2>创建候选模型</h2></div><FlaskConical size={22} /></div>
          <p>下载最近 2,000 根公共已完成 K 线，在本机训练。训练不会读取凭证、不会晋级、不会触发下单。</p>
          <div className="lab-facts"><span>约 7 天数据</span><span>12 根预测窗</span><span>固定 seed</span></div>
          <button className="button primary" disabled={Boolean(busy) || ml?.training.running} onClick={() => void run("train", () => api.trainModel(2000), "候选训练完成，请审阅样本外指标")}>{busy === "train" || ml?.training.running ? <><RefreshCw className="spin" size={15} />训练中…</> : <><Play size={15} />训练新候选</>}</button>
        </article>

        <article className="surface lab-card">
          <div className="lab-card-head"><div><span className="eyebrow">当前 champion</span><h2>{ml?.champion?.modelId ?? "尚未晋级"}</h2></div><ShieldCheck size={22} /></div>
          {ml?.champion ? <><p>第 {ml.champion.generation} 代，由 {ml.champion.reviewer} 于 {formatTime(ml.champion.approvedAt)} 人工晋级。</p><code className="hash-line">{ml.champion.artifactSha256}</code></> : <p>候选不会自动成为 champion。只有全部验证门通过并提供人工说明后才能晋级。</p>}
          <div className="lab-facts"><span>代次 {ml?.generation ?? 0}</span><span>自动执行默认关闭</span><span>仅 OKX Demo</span></div>
        </article>
      </section>

      <section className="surface model-section">
        <div className="section-heading"><div><span className="eyebrow">候选与验证</span><h2>固定周期 long-only OOS 诊断</h2></div><span className="count-pill">{ml?.models.length ?? 0} 个模型</span></div>
        {!ml || ml.models.length === 0 ? <div className="empty-lab"><LockKeyhole size={24} /><div><h3>还没有候选模型</h3><p>先运行一次公共数据训练，再根据每个时间窗的结果决定是否保留。</p></div></div> : <div className="model-list">{ml.models.map((model) => {
          const metrics = model.metrics;
          const isLongOnlyDiagnostic = metrics?.evaluationMode === "long-only-fixed-horizon-non-overlapping";
          const ready = model.state === "validated" && model.gateFailures.length === 0;
          return <article className={`model-row ${selectedModel === model.modelId ? "selected" : ""}`} key={model.modelId}>
            <div className="model-identity"><div><span className={`model-state ${ready ? "ready" : model.state}`}>{ready ? "可供人工晋级" : model.state}</span><strong>{model.modelId}</strong></div><time>{formatTime(model.createdAt)}</time></div>
            <div className="metric-grid"><div><span>OOS 窗口行</span><strong>{metrics?.oosRows ?? "—"}</strong></div><div><span>折数</span><strong>{metrics?.folds ?? "—"}</strong></div><div><span>{isLongOnlyDiagnostic ? "非重叠 long 入场" : "旧版方向交易（仅审计）"}</span><strong>{metrics?.trades ?? "—"}</strong></div><div><span>{isLongOnlyDiagnostic ? "long / flat 准确率" : "旧版方向准确率（仅审计）"}</span><strong>{percent(metrics?.accuracy)}</strong></div><div><span>{isLongOnlyDiagnostic ? "固定周期诊断净值" : "旧版重叠诊断净值（禁晋级）"}</span><strong className={(metrics?.netReturn ?? -1) >= 0 ? "positive" : "negative"}>{percent(metrics?.netReturn)}</strong></div><div><span>{isLongOnlyDiagnostic ? "诊断最大回撤" : "旧版诊断回撤（仅审计）"}</span><strong>{percent(metrics?.maxDrawdown)}</strong></div></div>
            <div className="gate-row">{!isLongOnlyDiagnostic ? <span className="gate-fail">旧版 long/short 重叠验证仅供审计，禁止晋级</span> : model.gateFailures.length === 0 ? <span className="gate-ok"><CheckCircle2 size={14} />全部晋级门通过</span> : model.gateFailures.map((failure) => <span className="gate-fail" key={failure}>{gateLabels[failure] ?? failure}</span>)}</div>
            <button className="button secondary" disabled={model.state !== "validated"} onClick={() => setSelectedModel(model.modelId)}>{selectedModel === model.modelId ? "已选择" : "选择审阅"}</button>
          </article>;
        })}</div>}
      </section>

      <section className="lab-grid">
        <article className="surface lab-card lab-form-card">
          <div className="lab-card-head"><div><span className="eyebrow">人工晋级</span><h2>冻结为 champion</h2></div><CheckCircle2 size={22} /></div>
          <label>候选模型<input value={selectedModel} onChange={(event) => setSelectedModel(event.target.value)} placeholder="从上方选择可晋级候选" /></label>
          <label>审阅人<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} maxLength={128} /></label>
          <label>审阅说明<textarea value={rationale} onChange={(event) => setRationale(event.target.value)} placeholder="至少 16 个字符，说明你核对了哪些样本外指标与风险" /></label>
          <label>逐字输入确认短语<input value={promotionPhrase} onChange={(event) => setPromotionPhrase(event.target.value)} placeholder={String(ml?.promotionPolicy.confirmation ?? "")} /></label>
          <button className="button primary" disabled={!canPromote || Boolean(busy) || promotionPhrase !== ml?.promotionPolicy.confirmation || rationale.trim().length < 16} onClick={() => selected && void run("promote", () => api.promoteModel({ modelId: selected.modelId, reviewer, rationale, confirmation: promotionPhrase, expectedGeneration: ml?.generation ?? 0 }), "champion 已晋级；自动执行仍保持关闭")}>{busy === "promote" ? "晋级中…" : "人工晋级"}</button>
        </article>

        <article className="surface lab-card lab-form-card automation-card">
          <div className="lab-card-head"><div><span className="eyebrow">受控模型试运行</span><h2>单次 OKX Demo BUY 入场</h2></div><Activity size={22} /></div>
          {activePermit ? <div className="permit-panel"><StatusDot /><div><strong>单次入场许可正在运行</strong><span>剩余 {activePermit.remainingSeconds}s · 已用 {activePermit.usedOrders}/1 笔 · {activePermit.usedNotionalUsdt}/10 USDT</span></div></div> : <p>这不是长期闭环执行。只有 champion、Demo 凭证和“演练”状态同时存在时才能启用；许可最多发送一笔 10 USDT BUY。SELL 与自动退出均被后端硬拒绝，成交后的退出需人工完成。</p>}
          {!activePermit && <><div className="lab-facts"><span>仅 1 笔 BUY</span><span>固定 10 USDT</span><span>退出需人工</span></div><label>逐字输入确认短语<input value={automationPhrase} onChange={(event) => setAutomationPhrase(event.target.value)} placeholder={ml?.automation.confirmation ?? ""} /></label><div className="readiness-list"><span className={ml?.champion ? "ok" : "wait"}>{ml?.champion ? "✓" : "○"} champion</span><span className={status?.credentialConfigured ? "ok" : "wait"}>{status?.credentialConfigured ? "✓" : "○"} Demo 凭证</span><span className={status?.safety.mode === "armed" ? "ok" : "wait"}>{status?.safety.mode === "armed" ? "✓" : "○"} 演练已启用</span></div><button className="button primary" disabled={!canAuthorize || Boolean(busy) || automationPhrase !== ml?.automation.confirmation} onClick={() => void run("authorize", () => api.authorizeAutomation({ issuedBy: reviewer, confirmation: automationPhrase, ttlSeconds: 300, maxOrders: 1, maxTotalNotionalUsdt: "10" }), "Demo 单次 BUY 入场许可已启用")}>{busy === "authorize" ? "启用中…" : "启用 5 分钟单次入场"}</button></>}
          {activePermit && <button className="button danger" disabled={Boolean(busy)} onClick={() => void run("stop", () => api.stopAutomation(), "自动会话已撤销，并触发急停核对")}>{busy === "stop" ? "停止中…" : "停止、撤单并锁定"}</button>}
          <small className="form-note">研究 OOS 假设 12 根后按双边成本结算；运行时并不自动执行该退出。停止只会撤销 permit、触发急停并尝试撤销未成交挂单，不能逆转已成交 BUY。</small>
        </article>
      </section>

      <section className="surface freqai-note"><Database size={22} /><div><h3>FreqAI 2026.7 兼容边界</h3><p>可选适配器只接收本机独立 FreqAI dry-run 进程的冻结信号，不把 Freqtrade 打进安装器，也不给它 OKX 凭证。Freqtrade 官方仍不支持 OKX sandbox，因此 Tideguard 保持唯一的 Demo 执行与风控入口。</p></div><span>未捆绑</span></section>
    </main>
  );
}

function AuditView({ events, chainValid, error }: { events: AuditEvent[]; chainValid: boolean | null; error?: string | null }) {
  return (
    <main className="page-view">
      <div className="page-title"><div><p>可追溯性</p><h1>审计日志</h1></div><ChainState value={chainValid} error={error} /></div>
      <section className="surface audit-list">{error || events.length === 0 ? <div className="empty-row"><Database size={20} />{error ? `审计读取失败：${error}` : "还没有本地事件"}</div> : events.map((event) => <article key={event.id} className="audit-entry"><div className="audit-symbol"><Check size={14} /></div><div className="audit-body"><div><strong>{eventLabels[event.eventType] ?? event.eventType}</strong><time>{formatTime(event.utcTime)}</time></div><p>{Object.entries(event.payload).slice(0, 4).map(([key, value]) => `${key}: ${String(value)}`).join(" · ") || "没有附加字段"}</p><code>{event.eventHash}</code></div></article>)}</section>
    </main>
  );
}

export default function App() {
  const [active, setActive] = useState<NavKey>("overview");
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [market, setMarket] = useState<MarketData | null>(null);
  const [account, setAccount] = useState<AccountData | null>(null);
  const [orders, setOrders] = useState<DemoOrder[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [chainValid, setChainValid] = useState<boolean | null>(null);
  const [marketError, setMarketError] = useState<string | null>(null);
  const [accountError, setAccountError] = useState<string | null>(null);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [latency, setLatency] = useState<number | null>(null);
  const [preview, setPreview] = useState<PreviewResult | null>(null);

  const refresh = useCallback(async () => {
    try {
      const nextStatus = await getStatus();
      setStatus(nextStatus);
      const marketRequest = (async () => {
        const started = performance.now();
        const value = await api.getMarket();
        return { value, latency: Math.max(1, Math.round(performance.now() - started)) };
      })();
      const [marketResult, accountResult, orderResult, auditResult] = await Promise.allSettled([
        marketRequest, api.getAccount(), api.getOrders(), api.getAudit(80)
      ]);
      if (marketResult.status === "fulfilled") {
        setMarket(marketResult.value.value);
        setMarketError(null);
        setLatency(marketResult.value.latency);
      } else {
        setMarket(null);
        setLatency(null);
        setMarketError(marketResult.reason instanceof Error ? marketResult.reason.message : "公共行情暂不可用");
      }
      if (accountResult.status === "fulfilled") { setAccount(accountResult.value); setAccountError(null); }
      else { setAccount(null); setAccountError(accountResult.reason instanceof Error ? accountResult.reason.message : "模拟账户读取失败"); }
      if (orderResult.status === "fulfilled") { setOrders(orderResult.value); setOrdersError(null); }
      else { setOrders([]); setOrdersError(orderResult.reason instanceof Error ? orderResult.reason.message : "挂单读取失败"); }
      if (auditResult.status === "fulfilled") {
        setEvents(auditResult.value.events);
        setChainValid(auditResult.value.chainValid);
        setAuditError(null);
      } else {
        setEvents([]);
        setChainValid(null);
        setAuditError(auditResult.reason instanceof Error ? auditResult.reason.message : "审计读取失败");
      }
    } catch (error) {
      setStatus(null);
      setMarket(null);
      setAccount(null);
      setOrders([]);
      setEvents([]);
      setChainValid(null);
      setNotice(error instanceof Error ? error.message : "本地服务不可用");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 8_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  useEffect(() => {
    if (!notice) return;
    const timeout = window.setTimeout(() => setNotice(null), 5_000);
    return () => window.clearTimeout(timeout);
  }, [notice]);

  const content = (() => {
    if (active === "settings") return <SettingsView status={status} onError={setNotice} />;
    if (active === "lab") return <StrategyLab status={status} onRefresh={refresh} onError={setNotice} />;
    if (active === "audit") return <AuditView events={events} chainValid={chainValid} error={auditError} />;
    if (active === "orders") return <main className="page-view"><div className="page-title"><div><p>OKX 模拟账户</p><h1>订单与对账</h1></div></div><OrdersTable orders={orders} configured={Boolean(status?.credentialConfigured)} error={ordersError} /></main>;
    if (active === "market") return <main className="page-view"><div className="page-title"><div><p>OKX 公共行情</p><h1>BTC / USDT 现货</h1></div></div><MarketPanel market={market} error={marketError} onRefresh={() => void refresh()} /><AccountStrip account={account} error={accountError} /></main>;
    return (
      <main className="dashboard-grid">
        <div className="dashboard-main">
          <MarketPanel market={market} error={marketError} onRefresh={() => void refresh()} />
          <AccountStrip account={account} error={accountError} />
          <OrdersTable orders={orders} configured={Boolean(status?.credentialConfigured)} error={ordersError} />
          <AuditRail events={events} chainValid={chainValid} error={auditError} />
        </div>
        <aside className="dashboard-inspector" aria-label="模拟下单与风控">
          <OrderTicket status={status} market={market} onPreview={setPreview} onRefresh={refresh} onError={setNotice} />
          <ArmControl status={status} onRefresh={refresh} onError={setNotice} />
          <RiskPanel status={status} preview={preview} onRefresh={refresh} onError={setNotice} />
        </aside>
      </main>
    );
  })();

  return (
    <div className={`app-shell mode-${status?.safety.mode ?? "observe"}`}>
      <Sidebar active={active} onChange={setActive} />
      <div className="app-content">
        <Topbar status={status} market={market} latency={latency} />
        {content}
        <footer className="app-footer"><ShieldCheck size={15} />仅绑定 127.0.0.1 <span>·</span> x-simulated-trading: 1 <span>·</span> 本地模拟环境 <span>·</span> 不构成投资建议</footer>
      </div>
      <nav className="mobile-nav" aria-label="移动端主导航">{navItems.map(({ key, label, icon: Icon }) => <button key={key} className={active === key ? "active" : ""} onClick={() => setActive(key)}><Icon size={19} /><span>{label === "策略实验室" ? "实验室" : label === "审计日志" ? "审计" : label}</span></button>)}</nav>
      {notice && <div className="toast" role="alert"><AlertTriangle size={18} /><span>{notice}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}><X size={16} /></button></div>}
    </div>
  );
}
