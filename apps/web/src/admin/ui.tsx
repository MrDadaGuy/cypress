import { useCallback, useEffect, useState, type ReactNode } from "react";
import { VISIBILITIES, type JobStatus, type Visibility } from "./api";

/** Load data, with a reload function and an error message. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    fn().then((d) => { setData(d); setError(null); }, (e) => setError(String(e.message ?? e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(reload, [reload]);
  return { data, error, reload };
}

/** Poll while `active` is true (e.g. jobs queued or running). */
export function usePoll(active: boolean, reload: () => void, ms = 3000) {
  useEffect(() => {
    if (!active) return;
    const t = setInterval(reload, ms);
    return () => clearInterval(t);
  }, [active, reload, ms]);
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

export const fmt = (n: number) => n.toLocaleString();

export function StatusChip({ status }: { status: JobStatus | string | null | undefined }) {
  if (!status) return <span className="chip muted">never run</span>;
  return <span className={`chip st-${status}`}>{status}</span>;
}

export function VisChip({ v }: { v: string }) {
  const o = VISIBILITIES.find((x) => x.value === v);
  return <span className={`chip vis-${v}`} title={o?.who}>{o?.label ?? v}</span>;
}

export function VisOptions() {
  return <>{VISIBILITIES.map((o) => <option key={o.value} value={o.value}>{o.label}: {o.who.toLowerCase()}</option>)}</>;
}

export type { Visibility };

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="error" role="alert">{error}</div> : null;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><h3>{title}</h3>{children}</div>;
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="field"><span className="lbl">{label}</span>{children}{hint && <span className="hint">{hint}</span>}</label>;
}

/** Wrap an async action: tracks busy state and surfaces errors. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); return true; } catch (e) { setError(String((e as Error).message ?? e)); return false; } finally { setBusy(false); }
  }, []);
  return { busy, error, run, setError };
}
