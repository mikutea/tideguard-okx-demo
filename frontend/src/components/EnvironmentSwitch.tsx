import { AlertOctagon, ArrowRight, Check, Fingerprint, LockKeyhole, RefreshCw, RotateCcw, Server, ShieldAlert, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { canConfirmEnvironmentChallenge } from "../lib/environment.js";
import { formatTime, shortId } from "../lib/format";
import type { EnvironmentAcknowledgements, EnvironmentChallenge, EnvironmentPreflight, EnvironmentStatus, EnvironmentTarget } from "../types";
import { CheckRow, StatusMark } from "./Primitives";

const ACKNOWLEDGEMENT_LABELS: Record<keyof EnvironmentAcknowledgements, string> = {
  automationStopped: "我确认长期自动化与本地限时授权均已停止",
  noOutstandingState: "我确认当前及目标环境没有未决订单或模型持仓",
  restartRequired: "我理解切换后必须重启，重启前交易保持锁定",
  liveFundsAtRisk: "我理解 Live 使用实际资金、API Trade 是写操作且收益不受保证"
};
const ALL_ACKNOWLEDGEMENTS = Object.keys(ACKNOWLEDGEMENT_LABELS) as Array<keyof EnvironmentAcknowledgements>;

function EnvironmentConfirmModal({ challenge, requirements, acknowledged, phrase, remainingSeconds, expired, transitionLocked, busy, onPhrase, onCancel, onConfirm }: {
  challenge: EnvironmentChallenge;
  requirements: Array<keyof EnvironmentAcknowledgements>;
  acknowledged: Array<keyof EnvironmentAcknowledgements>;
  phrase: string;
  remainingSeconds: number;
  expired: boolean;
  transitionLocked: boolean;
  busy: boolean;
  onPhrase: (value: string) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const cancelIfIdle = () => { if (!busy) onCancel(); };
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); previous?.focus(); };
  }, [busy, onCancel]);

  const allAcknowledged = requirements.every((item) => acknowledged.includes(item));
  const canConfirm = canConfirmEnvironmentChallenge({
    now: Date.now(),
    readyAt: challenge.readyAt,
    expiresAt: challenge.expiresAt,
    phrase,
    expectedPhrase: challenge.confirmationPhrase,
    allAcknowledged,
    transitionLocked
  });
  const currentFingerprint = challenge.preflight.binding?.currentAccountFingerprint;
  const targetFingerprint = challenge.preflight.binding?.targetAccountFingerprint;
  const liveTarget = challenge.target === "live";
  return <div className="environment-confirm-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && cancelIfIdle()}>
    <section ref={dialogRef} className={`environment-confirm-modal ${liveTarget ? "live" : ""}`} role="dialog" aria-modal="true" aria-labelledby="environment-confirm-title" tabIndex={-1}>
      <header><div className="confirm-risk-icon"><AlertOctagon size={24} /></div><div><h2 id="environment-confirm-title">最终高危确认</h2><p>{challenge.source.toUpperCase()} → {challenge.target.toUpperCase()} · 切换后必须重启</p></div><button className="icon-button" aria-label="取消环境切换" disabled={busy} onClick={cancelIfIdle}><X size={19} /></button></header>
      {liveTarget ? <div className="modal-live-funds"><AlertOctagon size={20} /><div><strong>LIVE 实际资金</strong><span>API Trade 包含订单写操作，亏损可能不可逆，收益不受保证。</span></div></div> : null}
      <div className="modal-fingerprints"><Fingerprint size={18} /><div><span>当前账户</span><code>{shortId(currentFingerprint, 14)}</code></div><ArrowRight size={17} /><div><span>目标账户</span><code>{shortId(targetFingerprint, 14)}</code></div></div>
      <div className="modal-risk-checks">{requirements.map((item) => <div className={acknowledged.includes(item) ? "checked" : "missing"} key={item}><span>{acknowledged.includes(item) ? <Check size={15} /> : <X size={15} />}</span><strong>{ACKNOWLEDGEMENT_LABELS[item]}</strong></div>)}</div>
      <label className="phrase-field"><span>逐字输入 <code>{challenge.confirmationPhrase}</code></span><input autoFocus value={phrase} onChange={(event) => onPhrase(event.target.value)} autoComplete="off" spellCheck={false} /></label>
      <div className={`server-countdown ${remainingSeconds === 0 && !expired ? "ready" : ""}`}><ShieldAlert size={20} /><div><strong>{transitionLocked ? "最终核对已锁定，重启后再试" : expired ? "挑战已过期" : remainingSeconds > 0 ? `服务器冷静期 ${remainingSeconds} 秒` : "冷静期结束，可确认"}</strong><span>readyAt {formatTime(challenge.readyAt)} · expires {formatTime(challenge.expiresAt)}</span></div></div>
      <div className="environment-confirm-actions"><button className="button secondary" disabled={busy} onClick={cancelIfIdle}>取消</button><button className="button danger" disabled={!canConfirm || busy} onClick={onConfirm}>{busy ? "服务器确认中…" : liveTarget ? "确认切换至 LIVE 实际资金" : "确认切换至 Demo"}</button></div>
      <p className={`modal-cancel-note ${busy ? "busy" : ""}`}><LockKeyhole size={15} />{busy ? "请求已发送，结果返回前不可撤回" : "Esc 或点击背景只会取消，不会确认或发送请求。"}</p>
    </section>
  </div>;
}

export function EnvironmentSwitch({ status, onChanged, onNotice }: { status: EnvironmentStatus; onChanged: () => Promise<void>; onNotice: (tone: "success" | "warning" | "error", message: string) => void }) {
  const [target, setTarget] = useState<EnvironmentTarget>(status.activeEnvironment === "live" ? "demo" : "live");
  const [preflight, setPreflight] = useState<EnvironmentPreflight | null>(null);
  const [challenge, setChallenge] = useState<EnvironmentChallenge | null>(null);
  const [acknowledged, setAcknowledged] = useState<Array<keyof EnvironmentAcknowledgements>>([]);
  const [phrase, setPhrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [complete, setComplete] = useState(false);
  const [clock, setClock] = useState(Date.now());

  useEffect(() => {
    if (!challenge) return;
    const interval = window.setInterval(() => setClock(Date.now()), 250);
    return () => window.clearInterval(interval);
  }, [challenge]);

  const requirements = useMemo(
    () => challenge?.requiredAcknowledgements?.length ? challenge.requiredAcknowledgements : ALL_ACKNOWLEDGEMENTS,
    [challenge?.requiredAcknowledgements]
  );
  const allAcknowledged = requirements.every((item) => acknowledged.includes(item));
  const remainingSeconds = challenge ? Math.max(0, Math.ceil((new Date(challenge.readyAt).getTime() - clock) / 1000)) : 0;
  const challengeExpired = challenge ? clock >= new Date(challenge.expiresAt).getTime() : false;
  const transitionLocked = Boolean(status.transitionPending || status.restartRequired || status.operatingMode === "transition_locked");

  const reset = (nextTarget = target) => {
    setTarget(nextTarget);
    setPreflight(null);
    setChallenge(null);
    setAcknowledged([]);
    setPhrase("");
    setComplete(false);
  };
  const cancelChallenge = useCallback(() => {
    setChallenge(null);
    setPhrase("");
  }, []);

  const runPreflight = async () => {
    setBusy(true);
    try {
      const result = await api.preflightEnvironmentSwitch(target);
      setPreflight(result);
      if (!result.allowed) onNotice("warning", "环境预检未通过；切换保持未发生");
    } catch (error) {
      onNotice("error", error instanceof Error ? error.message : "环境预检失败");
    } finally {
      setBusy(false);
    }
  };

  const createChallenge = async () => {
    if (!preflight || !allAcknowledged) return;
    setBusy(true);
    try {
      const result = await api.challengeEnvironmentSwitch(target);
      setChallenge(result);
      setClock(Date.now());
    } catch (error) {
      onNotice("error", error instanceof Error ? error.message : "服务器确认挑战创建失败");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!challenge || !canConfirmEnvironmentChallenge({ now: clock, readyAt: challenge.readyAt, expiresAt: challenge.expiresAt, phrase, expectedPhrase: challenge.confirmationPhrase, allAcknowledged, transitionLocked })) return;
    setBusy(true);
    try {
      const acknowledgementBody: EnvironmentAcknowledgements = {
        automationStopped: acknowledged.includes("automationStopped"),
        noOutstandingState: acknowledged.includes("noOutstandingState"),
        restartRequired: acknowledged.includes("restartRequired"),
        liveFundsAtRisk: acknowledged.includes("liveFundsAtRisk")
      };
      await api.confirmEnvironmentSwitch(target, challenge.nonce, phrase, acknowledgementBody);
      setComplete(true);
      await onChanged();
      onNotice("success", `${target === "live" ? "LIVE" : "Demo"} 切换请求已由服务器接受；重启后生效`);
    } catch (error) {
      onNotice("error", error instanceof Error ? error.message : "环境切换确认失败");
    } finally {
      setBusy(false);
    }
  };

  const liveTarget = target === "live";
  return <section className={`environment-console ${liveTarget ? "live-target" : "demo-target"}`} aria-labelledby="environment-title">
    <div className="environment-heading">
      <div><h2 id="environment-title">交易环境</h2><p>服务端状态是唯一权威；前端不会在本机伪造环境</p></div>
      <StatusMark tone={status.activeEnvironment === "live" ? "danger" : "healthy"}>当前 {status.activeEnvironment === "live" ? "LIVE" : "Demo"}</StatusMark>
    </div>
    <div className="environment-selector" role="group" aria-label="目标环境">
      <button className={target === "demo" ? "active" : ""} onClick={() => reset("demo")} disabled={busy || status.activeEnvironment === "demo"}>Demo 模拟盘<span>隔离资金 · 自动量化仅在此可用</span></button>
      <button className={target === "live" ? "active live" : "live"} onClick={() => reset("live")} disabled={busy || status.activeEnvironment === "live"}>LIVE 正式盘<span>实际资金 · 仅限人工保护交易</span></button>
    </div>
    {liveTarget ? <div className="live-risk-statement"><AlertOctagon size={21} /><div><strong>这不是“收益增强”开关</strong><p>Live 只开放独立限时人工交易，长期 AI 自动执行保持禁用。API Trade 可以创建实际订单，任何模型与历史结果都不能保证收益。</p></div></div> : null}
    {transitionLocked ? <div className="restart-required danger"><RotateCcw size={24} /><div><h3>最终核对已锁定，重启后再试</h3><p>当前进程仍是 {status.activeEnvironment.toUpperCase()}，切换目标是 {(status.transitionTarget ?? status.configuredEnvironment).toUpperCase()}。gate 与 kill 将保持到重启。</p></div></div> : null}

    {!preflight ? <div className="environment-step active"><span>1</span><div><h3>服务器预检</h3><p>核对凭证环境、账户身份、未决订单、持仓与风险边界。</p></div><button className={`button ${liveTarget ? "danger" : "primary"}`} disabled={busy || status.activeEnvironment === target || transitionLocked} onClick={runPreflight}>{busy ? <RefreshCw className="spin" size={16} /> : <Server size={16} />}{transitionLocked ? "重启后再试" : "开始预检"}</button></div> : null}

    {preflight ? <div className="environment-preflight">
      <div className="environment-step completed"><span><Check size={17} /></span><div><h3>预检结果</h3><p>{preflight.allowed ? "服务器允许进入风险确认流程" : "至少一个硬条件未通过"}</p></div><button className="text-action" onClick={() => reset()}><RotateCcw size={15} />重新预检</button></div>
      <div className="preflight-checks">{preflight.checks.map((check) => <CheckRow key={check.key} passed={check.passed} label={check.key.replaceAll("_", " ")} detail={check.detail ?? (check.passed ? "已通过" : "未通过")} />)}</div>
    </div> : null}

    {preflight?.allowed && !challenge ? <div className="environment-step active risk-ack-step"><span>2</span><div><h3>逐项承担风险</h3><p>每项确认都会写入服务端环境切换挑战，不得批量跳过。</p><div className="acknowledgement-list">{requirements.map((item) => <label key={item}><input type="checkbox" checked={acknowledged.includes(item)} onChange={(event) => setAcknowledged((current) => event.target.checked ? [...current, item] : current.filter((value) => value !== item))} /><span>{ACKNOWLEDGEMENT_LABELS[item]}</span></label>)}</div></div><button className={`button ${liveTarget ? "danger" : "primary"}`} disabled={!allAcknowledged || busy} onClick={createChallenge}>生成服务器挑战<ArrowRight size={16} /></button></div> : null}

    {challenge && !complete ? <EnvironmentConfirmModal challenge={challenge} requirements={requirements} acknowledged={acknowledged} phrase={phrase} remainingSeconds={remainingSeconds} expired={challengeExpired} transitionLocked={transitionLocked} busy={busy} onPhrase={setPhrase} onCancel={cancelChallenge} onConfirm={() => void confirm()} /> : null}

    {complete ? <div className={`restart-required restart-instructions ${liveTarget ? "danger" : ""}`}><RotateCcw size={24} /><div><h3>服务器已接受切换请求</h3><p>不会通过 HTTP 自行终止后台；请手动完成两步重启。</p><ol><li><span>1</span><strong>开始菜单 → 停止墨衡后台服务</strong></li><li><span>2</span><strong>重新打开墨衡 MOHENG</strong></li></ol></div></div> : null}
  </section>;
}
