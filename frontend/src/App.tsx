import {
  Activity,
  AlertTriangle,
  BarChart3,
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

function StrategyLab() {
  return (
    <main className="page-view">
      <div className="page-title"><div><p>离线研究</p><h1>策略实验室</h1></div></div>
      <section className="lab-hero surface"><FlaskConical size={30} /><div><h2>先冻结策略，再进入模拟盘</h2><p>当前版本不会在线自改代码、参数、风险阈值、交易品种或资金规模。候选策略只生成建议意图，必须经过时间隔离验证、人工晋级和同一套确定性风控。</p></div></section>
      <div className="lab-flow"><div><span>01</span><strong>离线数据</strong><p>保存数据时间与来源，不允许前视。</p></div><ChevronRight /><div><span>02</span><strong>Walk-forward</strong><p>计入手续费、滑点、延迟与拒单。</p></div><ChevronRight /><div><span>03</span><strong>Shadow</strong><p>只产生信号，不发送任何订单。</p></div><ChevronRight /><div><span>04</span><strong>人工晋级</strong><p>版本、哈希、风控全部复核后启用。</p></div></div>
      <section className="surface empty-lab"><LockKeyhole size={24} /><div><h3>当前版本未集成自动学习或策略引擎</h3><p>首版提供连接、风控、幂等和审计底座；冻结模型建议意图适配器仍待实现。</p></div></section>
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
    if (active === "lab") return <StrategyLab />;
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
