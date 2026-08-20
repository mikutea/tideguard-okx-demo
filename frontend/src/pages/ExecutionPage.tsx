import { AlertOctagon, Check, CircleDashed, Eye, KeyRound, LockKeyhole, Play, RefreshCw, ShieldCheck, Square, WalletCards, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { PageHeader, StatusMark } from "../components/Primitives";
import { environmentUiPolicy } from "../lib/environment.js";
import { formatNumber, formatPercent, formatTime, numberValue, shortId } from "../lib/format";
import type { AccountData, DemoOrder, EnvironmentMode, EnvironmentStatus, MarketData, MLStatus, PreviewResult, SystemStatus } from "../types";

function KillResetModal({ environment, busy, onCancel, onConfirm }: { environment: Exclude<EnvironmentMode, "unknown">; busy: boolean; onCancel: () => void; onConfirm: (phrase: string) => void }) {
  const [phrase, setPhrase] = useState("");
  const dialogRef = useRef<HTMLElement>(null);
  const requiredPhrase = environment === "live" ? "解除实盘急停" : "解除模拟盘急停";
  const cancelIfIdle = () => { if (!busy) onCancel(); };
  useEffect(() => {
    dialogRef.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) { event.preventDefault(); onCancel(); }
    };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, [busy, onCancel]);
  return <div className="kill-reset-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && cancelIfIdle()}>
    <section ref={dialogRef} className="kill-reset-modal" role="dialog" aria-modal="true" aria-labelledby="kill-reset-title" tabIndex={-1}>
      <header><div><AlertOctagon size={23} /></div><section><h2 id="kill-reset-title">核对并解除{environment === "live" ? "实盘" : "模拟盘"}急停</h2><p>解除只会回到 observe，不会自动 arm 或启动长期 AI。</p></section><button className="icon-button" aria-label="取消解除急停" disabled={busy} onClick={cancelIfIdle}><X size={19} /></button></header>
      <div className="kill-reset-proof"><ShieldCheck size={21} /><div><strong>服务端将重新证明安全终态</strong><ul><li>核对当前环境凭证与账户身份</li><li>读取全部本程序挂单并确认可安全处理</li><li>回查潜在订单意图与交易所最终状态</li></ul></div></div>
      <label className="phrase-field"><span>逐字输入 <code>{requiredPhrase}</code></span><input autoFocus value={phrase} onChange={(event) => setPhrase(event.target.value)} autoComplete="off" spellCheck={false} /></label>
      <div className="environment-confirm-actions"><button className="button secondary" disabled={busy} onClick={cancelIfIdle}>取消</button><button className="button danger" disabled={busy || phrase !== requiredPhrase} onClick={() => onConfirm(phrase)}>{busy ? "核对中…" : "核对并解除急停"}</button></div>
      {busy ? <p className="modal-cancel-note busy"><LockKeyhole size={15} />请求已发送，结果返回前不可撤回</p> : null}
    </section>
  </div>;
}

function ManualExecution({ environment, transitionLocked, status, market, onRefresh, onNotice }: { environment: EnvironmentMode; transitionLocked: boolean; status: SystemStatus | null; market: MarketData | null; onRefresh: () => Promise<void>; onNotice: (tone: "success" | "warning" | "error", message: string) => void }) {
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [ordType, setOrdType] = useState<"limit" | "post_only">("limit");
  const [price, setPrice] = useState("");
  const [size, setSize] = useState("");
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [commitKey, setCommitKey] = useState(() => crypto.randomUUID());
  const [armPhrase, setArmPhrase] = useState("");
  useEffect(() => { if (!price && market?.ticker.last) setPrice(market.ticker.last); }, [market?.ticker.last, price]);
  const notional = numberValue(price) !== null && numberValue(size) !== null ? Number(price) * Number(size) : null;
  const armed = status?.safety.mode === "armed";
  const live = environment === "live";
  const unknown = environment === "unknown";
  const environmentPolicy = environmentUiPolicy(environment);
  const executionLocked = environmentPolicy.executionLocked || transitionLocked;
  const requiredArmPhrase = environmentPolicy.manualArmPhrase;

  const clear = () => { setPreview(null); setConfirmed(false); setCommitKey(crypto.randomUUID()); };
  const arm = async () => {
    setBusy(true);
    try { await api.arm(armPhrase); setArmPhrase(""); await onRefresh(); onNotice("success", "手动演练已限时启用"); }
    catch (error) { onNotice("error", error instanceof Error ? error.message : "启用失败"); }
    finally { setBusy(false); }
  };
  const previewOrder = async () => {
    setBusy(true);
    try { const result = await api.preview({ instId: "BTC-USDT", side, ordType, price, size }); setPreview(result); setConfirmed(false); }
    catch (error) { onNotice("error", error instanceof Error ? error.message : "预检失败"); }
    finally { setBusy(false); }
  };
  const commit = async () => {
    if (!preview || !confirmed) return;
    setBusy(true);
    try { const result = await api.commit(preview, commitKey); onNotice("success", `订单状态 ${result.status}${result.ordId ? ` · ${result.ordId}` : ""}`); clear(); await onRefresh(); }
    catch (error) { onNotice("error", error instanceof Error ? error.message : "提交失败"); }
    finally { setBusy(false); }
  };

  return <section className={`workspace-panel manual-execution ${live ? "live" : unknown ? "unknown" : ""}`} aria-labelledby="manual-execution-title">
    <div className="panel-heading"><div><h2 id="manual-execution-title">高级：手动订单诊断</h2><p>{transitionLocked ? "最终核对已锁定，必须重启后再试" : live ? "LIVE 只允许独立限时人工保护交易；长期 AI 自动执行仍禁用" : unknown ? "环境权威状态未知；预检、授权与提交全部锁定" : "仅用于验证预检与订单生命周期，不是长期 AI 的主控制器"}</p></div><StatusMark tone={live ? "danger" : executionLocked ? "warning" : armed ? "warning" : "neutral"}>{transitionLocked ? "切换锁定" : live ? armed ? `LIVE 授权 ${status?.safety.armedRemainingSeconds ?? 0}s` : "LIVE 实际资金" : unknown ? "环境未知 · 锁定" : armed ? `演练 ${status?.safety.armedRemainingSeconds ?? 0}s` : "未授权"}</StatusMark></div>
    {live ? <div className="live-manual-policy"><AlertOctagon size={19} /><strong>实际资金：每次限时 60 秒 · 单笔不超过 10 USDT · 最多 1 个活动挂单 · 收益不保证</strong></div> : null}
    {!armed ? <div className="manual-arm"><LockKeyhole size={21} /><div><strong>{transitionLocked ? "最终核对已锁定，重启后再试" : live ? "启用 60 秒 LIVE 人工授权" : unknown ? "等待服务端确认交易环境" : "先启用限时手动演练"}</strong><p>{transitionLocked ? "gate 与 kill 保持生效，重启前不会发送任何交易请求。" : live ? "授权只适用于人工预检/提交，不授予模型或 Codex 自动下单。" : unknown ? "未知环境不会回落为 Demo；最后一次 Live 风险样式会被保留。" : "这与长期自动化 master 完全独立；服务重启或超时后自动锁定。"}</p></div><label><span>{unknown || transitionLocked ? "解锁后显示授权短语" : `输入 ${requiredArmPhrase}`}</span><input value={armPhrase} disabled={executionLocked} onChange={(event) => setArmPhrase(event.target.value)} /></label><button className={`button ${live ? "danger" : "secondary"}`} disabled={busy || executionLocked || armPhrase !== requiredArmPhrase || !status?.credentialConfigured} onClick={arm}>{transitionLocked ? "重启后再试" : live ? "启用 60 秒 LIVE 授权" : unknown ? "环境未知 · 禁止授权" : "启用 10 分钟"}</button></div> : null}
    <div className="order-workbench">
      <div className="order-fields">
        <div className="segmented" role="group" aria-label="订单方向"><button className={side === "buy" ? "active buy" : ""} onClick={() => { setSide("buy"); clear(); }}>买入</button><button className={side === "sell" ? "active sell" : ""} onClick={() => { setSide("sell"); clear(); }}>卖出</button></div>
        <label><span>订单类型</span><select value={ordType} onChange={(event) => { setOrdType(event.target.value as "limit" | "post_only"); clear(); }}><option value="limit">限价</option><option value="post_only">只做 Maker</option></select></label>
        <label><span>价格 <em>USDT</em></span><input value={price} inputMode="decimal" onChange={(event) => { setPrice(event.target.value); clear(); }} /></label>
        <label><span>数量 <em>BTC</em></span><input value={size} inputMode="decimal" placeholder="0.00000" onChange={(event) => { setSize(event.target.value); clear(); }} /></label>
        <div className="order-notional"><span>预计金额</span><strong>{notional === null ? "—" : `${formatNumber(notional, 2)} USDT`}</strong></div>
        {!preview ? <button className="button primary full" disabled={executionLocked || !armed || busy || !price || !size} onClick={previewOrder}><Eye size={16} />{busy ? "预检中…" : transitionLocked ? "切换锁定 · 重启后再试" : unknown ? "环境未知 · 禁止预检" : "运行确定性预检"}</button> : null}
      </div>
      <div className="preview-evidence">
        {!preview ? <div className="visual-empty tall"><ShieldCheck /><div><strong>等待订单预检</strong><span>服务端将重新读取行情、账户、身份与风险策略。</span></div></div> : <>
          <div className={`preview-verdict ${preview.decision.allowed ? "allowed" : "rejected"}`}><strong>{preview.decision.allowed ? "预检通过" : "风控拒绝"}</strong><span>策略 {preview.decision.policyVersion} · 有效至 {formatTime(preview.expiresAt)}</span></div>
          <div className="risk-checks">{preview.decision.checks.map((check) => <div key={check.key} className={check.passed ? "passed" : "failed"}><span>{check.passed ? <Check size={15} /> : <AlertOctagon size={15} />}</span><div><strong>{check.label}</strong><small>{check.current}</small></div><em>{check.limit}</em></div>)}</div>
          {preview.decision.allowed ? <><label className={`final-order-confirm ${live ? "live" : ""}`}><input type="checkbox" checked={confirmed} disabled={executionLocked} onChange={(event) => setConfirmed(event.target.checked)} /><span>{transitionLocked ? "最终核对已锁定，重启前禁止提交" : live ? "我确认这会向正式盘提交实际资金订单" : unknown ? "环境状态未知，订单提交保持锁定" : "我确认这是 OKX 模拟盘订单"}</span></label><button className={`button full ${live ? "danger" : "primary"}`} disabled={executionLocked || !confirmed || busy} onClick={commit}>{busy ? "提交中…" : transitionLocked ? "切换锁定 · 重启后再试" : live ? "确认提交 LIVE 订单" : unknown ? "环境未知 · 禁止提交" : "确认提交 Demo 订单"}</button></> : <button className="button secondary full" onClick={clear}>修改订单</button>}
        </>}
      </div>
    </div>
  </section>;
}

export function ExecutionPage({ environment, environmentStatus, transitionLocked, status, market, account, orders, ml, busy, onEnableMaster, onDisableMaster, onRefresh, onNotice }: { environment: EnvironmentMode; environmentStatus: EnvironmentStatus | null; transitionLocked: boolean; status: SystemStatus | null; market: MarketData | null; account: AccountData | null; orders: DemoOrder[]; ml: MLStatus | null; busy: boolean; onEnableMaster: (phrase: string) => Promise<void>; onDisableMaster: () => Promise<void>; onRefresh: () => Promise<void>; onNotice: (tone: "success" | "warning" | "error", message: string) => void }) {
  const longRun = ml?.longRun ?? null;
  const [masterPhrase, setMasterPhrase] = useState("");
  const [resetOpen, setResetOpen] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  const masterEnabled = longRun?.state.desiredMode === "demo";
  const canResetKill = environment !== "unknown" && !transitionLocked;
  const readiness = useMemo(() => [
    { label: "交易凭证", passed: Boolean(status?.credentialConfigured) },
    { label: "审计链", passed: Boolean(status?.auditChainValid) },
    { label: "Champion", passed: Boolean(longRun?.champion) },
    { label: "Codex Lease", passed: Boolean(longRun?.activeSupervisorLease) },
    { label: "急停未触发", passed: !status?.safety.killActive }
  ], [longRun?.activeSupervisorLease, longRun?.champion, status?.auditChainValid, status?.credentialConfigured, status?.safety.killActive]);
  const resetKill = async (phrase: string) => {
    setResetBusy(true);
    try {
      await api.resetKill(phrase);
      setResetOpen(false);
      onNotice("success", "急停已解除；当前仍为 observe，未自动 arm，需另行输入限时授权短语");
      await onRefresh();
    } catch (error) {
      onNotice("error", error instanceof Error ? error.message : "急停核对失败");
    } finally {
      setResetBusy(false);
    }
  };
  return <div className="page-stack execution-page">
    <PageHeader title="策略执行" description="自动化 master、模型自有仓位和手动诊断彼此隔离；每笔订单仍通过最终风控门。" meta={<><StatusMark tone={environment === "live" ? "danger" : environment === "unknown" ? "warning" : masterEnabled ? "warning" : "healthy"}>{environment === "live" ? "LIVE 实际资金" : environment === "unknown" ? "环境未知 · 执行锁定" : masterEnabled ? "Demo master 已启用" : "新开仓已关闭"}</StatusMark><span>{orders.length} 个活动订单</span></>} />
    {status?.safety.killActive ? <section className="kill-reset-card" aria-labelledby="kill-reset-card-title"><AlertOctagon size={24} /><div><h2 id="kill-reset-card-title">当前环境急停已锁定</h2><p>先由服务端核对身份、全部挂单与潜在订单终态；解除后仍停留 observe。</p></div><button className="button danger" disabled={!canResetKill} onClick={() => setResetOpen(true)}>{transitionLocked ? "最终核对锁定 · 重启后再试" : environment === "unknown" ? "环境未知 · 禁止解除" : "核对并解除急停"}</button></section> : null}
    <div className="execution-grid">
      <section className={`workspace-panel automation-master ${environment === "live" ? "live-disabled" : ""}`} aria-labelledby="automation-title">
        <div className="panel-heading"><div><h2 id="automation-title">长期自动化 Master</h2><p>用户只决定是否允许；模型晋级与 lease 仍由证据门控制</p></div>{masterEnabled ? <Play size={20} /> : <Square size={20} />}</div>
        {transitionLocked ? <div className="live-automation-disabled"><AlertOctagon size={25} /><div><strong>最终核对已锁定，重启后再试</strong><p>环境切换 gate 与 kill 保持生效；重启前长期 AI 和人工交易入口均不可用。</p></div></div> : environment === "unknown" ? <div className="live-automation-disabled"><AlertOctagon size={25} /><div><strong>环境状态未知，长期 AI 执行锁定</strong><p>系统不会把未知环境当作 Demo。恢复服务端结构化环境状态后，才会重新显示对应控制。</p></div></div> : environment === "live" ? <div className="live-automation-disabled"><AlertOctagon size={25} /><div><strong>LIVE 长期 AI 自动执行不可用</strong><p>当前版本仅允许独立限时人工保护交易。模型必须先在 Demo 完成 OOS、Shadow、监督与闭环验证；即使通过也不会在 LIVE 自动启用。</p><span>server: liveAutomationAvailable = {String(environmentStatus?.liveAutomationAvailable ?? false)}</span></div></div> : <><div className="readiness-rail">{readiness.map((item) => <div className={item.passed ? "passed" : "waiting"} key={item.label}><span>{item.passed ? <Check size={15} /> : <CircleDashed size={15} />}</span><strong>{item.label}</strong></div>)}</div>
        {!masterEnabled ? <div className="master-enable"><label><span>逐字输入 <code>ENABLE LONG-RUN OKX DEMO</code></span><input value={masterPhrase} onChange={(event) => setMasterPhrase(event.target.value)} placeholder="ENABLE LONG-RUN OKX DEMO" /></label><button className="button primary" disabled={busy || masterPhrase !== "ENABLE LONG-RUN OKX DEMO" || !status?.credentialConfigured || Boolean(status?.safety.killActive)} onClick={() => void onEnableMaster(masterPhrase)}>启用长期 Demo Master</button></div> : <button className="button danger" disabled={busy} onClick={() => void onDisableMaster()}><Square size={16} />停止新开仓</button>}</>}
        <p className="boundary-note"><ShieldCheck size={17} />关闭 master 不会遗弃已成交仓位；系统会进入 exit-only。任何未知订单都会急停并等待交易所终态核对。</p>
      </section>
      <section className="workspace-panel execution-position" aria-labelledby="execution-position-title">
        <div className="panel-heading"><div><h2 id="execution-position-title">模型自有库存</h2><p>自动 SELL 永远不得超过本程序实际成交净库存</p></div><WalletCards size={20} /></div>
        {longRun?.activePosition ? <dl className="position-definition"><div><dt>状态</dt><dd>{longRun.activePosition.status}</dd></div><div><dt>剩余</dt><dd>{longRun.activePosition.remainingSize} BTC</dd></div><div><dt>入场均价</dt><dd>{formatNumber(longRun.activePosition.entryAvgPrice)}</dd></div><div><dt>止损 / 止盈</dt><dd>{formatNumber(longRun.activePosition.stopPrice)} / {formatNumber(longRun.activePosition.takeProfitPrice)}</dd></div><div><dt>退出尝试</dt><dd>{longRun.activePosition.exitAttempts}</dd></div><div><dt>最迟退出</dt><dd>{formatTime(longRun.activePosition.hardExitAt)}</dd></div></dl> : <div className="visual-empty tall"><CircleDashed /><div><strong>当前空仓</strong><span>账户原有资产不属于模型库存。</span></div></div>}
        <div className="account-readonly"><KeyRound size={17} /><span>账户只读权益</span><strong>{formatNumber(account?.equityUsdt)} USDT</strong></div>
      </section>
    </div>
    <section className="workspace-panel position-ledger" aria-labelledby="position-ledger-title"><div className="panel-heading"><div><h2 id="position-ledger-title">闭环仓位与决策记录</h2><p>只展示可追溯的模型自有仓位；收益为含费用实际终态</p></div></div><div className="table-scroll"><table><thead><tr><th>仓位</th><th>模型</th><th>状态</th><th className="numeric">成交数量</th><th className="numeric">剩余</th><th className="numeric">净收益</th><th>创建</th><th>关闭</th></tr></thead><tbody>{(longRun?.recentPositions ?? []).map((position) => <tr key={position.positionId}><td>{shortId(position.positionId, 10)}</td><td>{shortId(position.modelId, 10)}</td><td>{position.status}</td><td className="numeric">{position.filledSize}</td><td className="numeric">{position.remainingSize}</td><td className={`numeric ${(position.realizedReturn ?? 0) < 0 ? "negative" : "positive"}`}>{formatPercent(position.realizedReturn)}</td><td>{formatTime(position.createdAt)}</td><td>{formatTime(position.closedAt)}</td></tr>)}</tbody></table>{(longRun?.recentPositions.length ?? 0) === 0 ? <div className="visual-empty"><CircleDashed /><span>尚无模型闭环仓位</span></div> : null}</div></section>
    <section className="workspace-panel order-ledger" aria-labelledby="order-ledger-title"><div className="panel-heading"><div><h2 id="order-ledger-title">当前活动订单</h2><p>仅本程序 tag；每次刷新都从当前环境重新读取</p></div><button className="icon-button" aria-label="刷新订单" onClick={() => void onRefresh()}><RefreshCw size={17} /></button></div><div className="table-scroll"><table><thead><tr><th>时间</th><th>订单</th><th>方向</th><th>类型</th><th className="numeric">价格</th><th className="numeric">数量</th><th className="numeric">已成交</th><th>状态</th></tr></thead><tbody>{orders.map((order) => <tr key={order.ordId}><td>{formatTime(order.createdAt)}</td><td>{shortId(order.ordId, 12)}</td><td className={order.side}>{order.side === "buy" ? "买入" : "卖出"}</td><td>{order.ordType}</td><td className="numeric">{order.price}</td><td className="numeric">{order.size}</td><td className="numeric">{order.filledSize}</td><td>{order.state}</td></tr>)}</tbody></table>{orders.length === 0 ? <div className="visual-empty"><CircleDashed /><span>没有本程序活动订单</span></div> : null}</div></section>
    <ManualExecution environment={environment} transitionLocked={transitionLocked} status={status} market={market} onRefresh={onRefresh} onNotice={onNotice} />
    {resetOpen && environment !== "unknown" ? <KillResetModal environment={environment} busy={resetBusy} onCancel={() => setResetOpen(false)} onConfirm={(phrase) => void resetKill(phrase)} /> : null}
  </div>;
}
