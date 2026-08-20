import { AlertTriangle, Check, ChevronRight, Info, X } from "lucide-react";
import { useEffect, useRef } from "react";
import type { ReactNode } from "react";

export function PageHeader({ title, description, meta, actions }: { title: string; description: string; meta?: ReactNode; actions?: ReactNode }) {
  return <header className="page-header"><div><h1>{title}</h1><p>{description}</p></div>{meta ? <div className="page-meta">{meta}</div> : null}{actions ? <div className="page-actions">{actions}</div> : null}</header>;
}

export function StatusMark({ tone = "neutral", children }: { tone?: "healthy" | "warning" | "danger" | "neutral" | "info"; children: ReactNode }) {
  return <span className={`status-mark ${tone}`}><i aria-hidden="true" />{children}</span>;
}

export function EvidenceDrawer({ open, title, subtitle, onClose, children }: { open: boolean; title: string; subtitle?: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    ref.current?.focus();
    const close = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", close);
    return () => { document.removeEventListener("keydown", close); previous?.focus(); };
  }, [open, onClose]);
  if (!open) return null;
  return <div className="drawer-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <aside ref={ref} className="evidence-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title" tabIndex={-1}>
      <div className="drawer-heading"><div><h2 id="drawer-title">{title}</h2>{subtitle ? <p>{subtitle}</p> : null}</div><button className="icon-button" aria-label="关闭详情" onClick={onClose}><X size={19} /></button></div>
      <div className="drawer-body">{children}</div>
    </aside>
  </div>;
}

export function ConfirmDialog({ open, title, description, confirmLabel, danger = false, busy = false, onClose, onConfirm }: { open: boolean; title: string; description: ReactNode; confirmLabel: string; danger?: boolean; busy?: boolean; onClose: () => void; onConfirm: () => void }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    ref.current?.focus();
    const close = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", close);
    return () => { document.removeEventListener("keydown", close); previous?.focus(); };
  }, [open, onClose]);
  if (!open) return null;
  return <div className="modal-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <section ref={ref} className={`confirm-dialog ${danger ? "danger" : ""}`} role="dialog" aria-modal="true" aria-labelledby="confirm-title" tabIndex={-1}>
      <div className="dialog-symbol">{danger ? <AlertTriangle /> : <Info />}</div>
      <h2 id="confirm-title">{title}</h2>
      <div className="dialog-copy">{description}</div>
      <div className="dialog-actions"><button className="button secondary" onClick={onClose}>取消</button><button className={`button ${danger ? "danger" : "primary"}`} disabled={busy} onClick={onConfirm}>{busy ? "处理中…" : confirmLabel}</button></div>
    </section>
  </div>;
}

export function DetailLink({ children, onClick }: { children: ReactNode; onClick?: () => void }) {
  return <button className="detail-link" onClick={onClick}>{children}<ChevronRight size={15} /></button>;
}

export function CheckRow({ passed, label, detail }: { passed: boolean; label: string; detail: string }) {
  return <div className={`check-row ${passed ? "passed" : "failed"}`}><span>{passed ? <Check size={15} /> : <X size={15} />}</span><div><strong>{label}</strong><small>{detail}</small></div></div>;
}
