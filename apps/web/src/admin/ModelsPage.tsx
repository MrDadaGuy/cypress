import { useEffect, useMemo, useRef, useState } from "react";
import {
  api, AUDIENCES, type AgentConfig, type Audience, type Budget, type LlmModel, type ModelsData, type NodeInfo,
  type ProtectionData, type ProtectionSettings, type Probe, type Runtime, type Usage,
} from "./api";
import { ago, Empty, ErrorBox, Field, fmt, useAction, useLoad } from "./ui";

type Tab = "spend" | "steps" | "catalog" | "settings" | "protection";

const usd = (n: number | null | undefined, digits?: number) => {
  if (n == null) return "—";
  const d = digits ?? (n === 0 ? 2 : n < 0.01 ? 4 : n < 1 ? 3 : 2);
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
};
const perM = (n: number | null) => (n == null ? "—" : `$${n.toLocaleString(undefined, { maximumFractionDigits: 4 })}`);
const tok = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));

export default function ModelsPage() {
  const { data, error, reload } = useLoad(api.models);
  const [tab, setTab] = useState<Tab>("spend");
  const [override, setOverride] = useState<ModelsData | null>(null);
  const d = override ?? data;
  if (error) return <ErrorBox error={error} />;
  if (!d) return <p className="muted">Loading…</p>;
  const refresh = (next?: ModelsData) => (next ? setOverride(next) : (setOverride(null), reload()));
  const label = (id: string) => d.models.find((m) => m.id === id)?.label ?? id;
  return (
    <>
      <header className="page-head"><div><h1>Models &amp; spend</h1>
        <p className="sub">Which model each step of Cypress uses, what it costs, and the monthly budget. Every paid call is metered, including backups, failures and tests. Changes apply within 30 seconds and are versioned.</p></div></header>
      <BudgetBanner d={d} label={label} onSettings={() => setTab("settings")} />
      <div className="tabs" role="tablist">
        {([["spend", "Spend"], ["steps", "Steps"], ["catalog", `Catalog (${d.models.length})`], ["settings", "Budget & fallback"], ["protection", "Protection"]] as [Tab, string][]).map(([t, l]) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{l}</button>))}
      </div>
      {tab === "spend" && <SpendTab d={d} label={label} />}
      {tab === "steps" && <StepsTab d={d} label={label} refresh={refresh} />}
      {tab === "catalog" && <CatalogTab d={d} refresh={refresh} />}
      {tab === "settings" && <SettingsTab d={d} refresh={refresh} />}
      {tab === "protection" && <ProtectionTab />}
    </>
  );
}

function BudgetBanner({ d, label, onSettings }: { d: ModelsData; label: (id: string) => string; onSettings: () => void }) {
  const b = d.budget;
  if (b.guard_active) return (
    <div className="panel warn slim" role="status"><b>Budget reached.</b> {usd(b.month_to_date, 2)} spent of {usd(b.monthly_usd, 2)} this month,
      so every step is running on {label(b.guard_model)} until the month ends or the budget is raised. <button className="linkish" onClick={onSettings}>Change budget</button></div>);
  if (b.alert) return (
    <div className="panel warn slim" role="status"><b>{Math.round((b.month_to_date / b.monthly_usd) * 100)}% of this month's budget used</b> ({usd(b.month_to_date, 2)} of {usd(b.monthly_usd, 2)}).
      {b.guard ? ` At 100% every step switches to ${label(b.guard_model)}.` : " The guard is off, so spending won't be capped."}</div>);
  return null;
}

// ---------------- spend ----------------

function SpendTab({ d, label }: { d: ModelsData; label: (id: string) => string }) {
  const [days, setDays] = useState(30);
  const { data: u, error } = useLoad(() => api.usage(days), [days]);
  const b = d.budget;
  return (
    <>
      <div className="kpis">
        <Kpi label="This month" value={usd(b.month_to_date)} sub={b.monthly_usd ? `of ${usd(b.monthly_usd, 2)} budget` : "no budget set"}
          meter={b.monthly_usd ? b.month_to_date / b.monthly_usd : undefined} />
        <Kpi label="Projected month" value={usd(b.projected)} sub={`day ${b.day} of ${b.days_in_month}, at this month's pace`} />
        <Kpi label="Per question" value={u ? usd(u.totals.cost_per_turn) : "…"} sub={u ? `${fmt(u.totals.turns)} chat turns, last ${days} days` : ""} />
        <Kpi label={`Last ${days} days`} value={u ? usd(u.totals.cost) : "…"} sub={u ? `${fmt(u.totals.calls)} paid calls` : ""} />
      </div>
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Period">
          {[7, 30, 90].map((n) => <button key={n} className={days === n ? "on" : ""} aria-pressed={days === n} onClick={() => setDays(n)}>{n} days</button>)}
        </div>
      </div>
      <ErrorBox error={error} />
      {!u ? <p className="muted">Loading…</p> : u.totals.calls === 0 ? <Empty title="No model calls in this period" /> : (
        <>
          <Leaks u={u} />
          <section className="panel"><h3 className="h3">Daily spend</h3><DailyChart u={u} label={label} /></section>
          <section className="panel"><h3 className="h3">By step</h3>
            <p className="small muted">Hedges are backup calls sent when a model is slow to start; fallbacks are sent when it fails. Both are billed.</p>
            <div className="tbl"><table className="stack-sm">
              <thead><tr><th>Step</th><th>Model</th><th className="num">Calls</th><th className="num">Tokens in / out</th><th className="num">Latency p50 / p95</th><th className="num">Backups</th><th className="num">Cost</th></tr></thead>
              <tbody>{u.by_node.map((r) => (
                <tr key={r.node + r.model_id}>
                  <td data-label="Step" className="mono">{r.node}</td>
                  <td data-label="Model">{label(r.model_id)}</td>
                  <td data-label="Calls" className="num">{fmt(r.calls)}{r.failed ? <span className="small crit-text"> · {r.failed} failed</span> : null}</td>
                  <td data-label="Tokens" className="num">{tok(r.input_tokens)} / {tok(r.output_tokens)}</td>
                  <td data-label="Latency" className="num">{r.p50_ms != null ? `${(r.p50_ms / 1000).toFixed(1)}s / ${((r.p95_ms ?? 0) / 1000).toFixed(1)}s` : "—"}</td>
                  <td data-label="Backups" className="num">{r.backups || "—"}</td>
                  <td data-label="Cost" className="num strong">{usd(r.cost)}</td>
                </tr>))}</tbody>
            </table></div>
          </section>
          <WhatIf u={u} label={label} />
          <div className="grid2 top">
            <section className="panel"><h3 className="h3">By audience</h3>
              <div className="tbl"><table className="stack-sm">
                <thead><tr><th>Channel</th><th>Who</th><th className="num">Turns</th><th className="num">Cost</th></tr></thead>
                <tbody>{u.by_channel.map((r) => (
                  <tr key={r.channel + r.role}><td data-label="Channel">{r.channel}</td><td data-label="Who">{r.role}</td>
                    <td data-label="Turns" className="num">{r.channel === "background" ? "—" : fmt(r.turns)}</td><td data-label="Cost" className="num">{usd(r.cost)}</td></tr>))}</tbody>
              </table></div>
            </section>
            <section className="panel"><h3 className="h3">Most expensive turns</h3>
              <div className="tbl"><table className="stack-sm">
                <thead><tr><th>When</th><th>Intent</th><th className="num">Calls</th><th className="num">Cost</th></tr></thead>
                <tbody>{u.top_turns.map((t) => (
                  <tr key={t.turn_id}><td data-label="When">{t.interaction_id ? <a href={`/admin/conversations?turn=${t.interaction_id}`}>{ago(t.at)}</a> : ago(t.at)} <span className="small muted">{t.channel}</span></td>
                    <td data-label="Intent">{t.intent ?? "—"}</td><td data-label="Calls" className="num">{t.calls}</td><td data-label="Cost" className="num">{usd(t.cost)}</td></tr>))}</tbody>
              </table></div>
            </section>
          </div>
        </>)}
    </>
  );
}

function Kpi({ label, value, sub, meter }: { label: string; value: string; sub?: string; meter?: number }) {
  return (
    <div className="kpi">
      <div className="kpi-l">{label}</div>
      <div className="kpi-v">{value}</div>
      {meter != null && <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(meter * 100)} aria-label={`${label}: ${Math.round(meter * 100)}% of budget`}>
        <span style={{ width: `${Math.min(100, meter * 100)}%` }} className={meter >= 1 ? "over" : meter >= 0.8 ? "near" : ""} /></div>}
      {sub && <div className="kpi-s">{sub}</div>}
    </div>
  );
}

function Leaks({ u }: { u: Usage }) {
  const t = u.totals;
  const items = [
    t.unpriced ? `${fmt(t.unpriced)} calls used a model with no price, so their cost is missing. Add the price in Catalog, then re-price.` : null,
    t.estimated ? `${fmt(t.estimated)} calls had token counts estimated because the provider didn't report usage (mostly failed calls).` : null,
  ].filter(Boolean);
  const extra = t.hedge_cost + t.fallback_cost + t.probe_cost;
  return (
    <>
      {items.length > 0 && <div className="panel warn slim">{items.map((i) => <div key={i}>{i}</div>)}</div>}
      {extra > 0 && <p className="small muted">Included above: {usd(t.hedge_cost)} on hedges, {usd(t.fallback_cost)} on fallbacks, {usd(t.probe_cost)} on test calls.</p>}
    </>
  );
}

/** Single-series bar chart of total spend per day; hover a day for its per-model breakdown. */
function DailyChart({ u, label }: { u: Usage; label: (id: string) => string }) {
  const [hover, setHover] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(720);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(260, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const days = useMemo(() => {
    const byDay = new Map<string, { cost: number; calls: number; models: Map<string, number> }>();
    for (let i = u.days - 1; i >= 0; i--) {
      const dt = new Date(Date.now() - i * 864e5);
      byDay.set(dt.toLocaleDateString("en-CA", { timeZone: "America/Chicago" }), { cost: 0, calls: 0, models: new Map() });
    }
    for (const r of u.daily) {
      const e = byDay.get(r.day);
      if (!e) continue;
      e.cost += r.cost; e.calls += r.calls;
      e.models.set(r.model_id, (e.models.get(r.model_id) ?? 0) + r.cost);
    }
    return [...byDay.entries()].map(([day, v]) => ({ day, ...v }));
  }, [u]);
  const max = Math.max(...days.map((x) => x.cost), 0.0001);
  const nice = niceMax(max);
  const H = W < 480 ? 150 : 180, padL = 52, padB = 22, padT = 8;
  const bw = (W - padL) / days.length;
  const y = (v: number) => padT + (H - padT - padB) * (1 - v / nice);
  const h = hover != null ? days[hover] : null;
  return (
    <div className="chart">
      <div ref={box} className="plot"><svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Daily model spend over the last ${u.days} days, peak ${usd(max)}`} onMouseLeave={() => setHover(null)}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={padL} x2={W} y1={y(nice * f)} y2={y(nice * f)} className="grid" />
            <text x={padL - 6} y={y(nice * f) + 4} textAnchor="end" className="axis">{usd(nice * f, nice < 0.1 ? 3 : 2)}</text>
          </g>))}
        {days.map((x, i) => {
          const top = y(x.cost), bh = Math.max(0, H - padB - top);
          return (
            <g key={x.day} onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} tabIndex={0} aria-label={`${x.day}: ${usd(x.cost)}`}>
              <rect x={padL + i * bw} y={padT} width={bw} height={H - padT - padB} className="hit" />
              {bh > 0 && <path className={`bar${hover === i ? " on" : ""}`} d={roundTop(padL + i * bw + 1, top, Math.max(1, bw - 2), bh, Math.min(4, (bw - 2) / 2))} />}
            </g>);
        })}
        {[0, Math.floor(days.length / 2), days.length - 1].map((i) => (
          <text key={i} x={padL + i * bw + bw / 2} y={H - 6} textAnchor={i === 0 ? "start" : i === days.length - 1 ? "end" : "middle"} className="axis">{shortDay(days[i].day)}</text>))}
      </svg></div>
      <div className="tip" aria-live="polite">
        {h ? <><b>{shortDay(h.day)}</b> · {usd(h.cost)} · {fmt(h.calls)} calls
          {[...h.models.entries()].sort((a, b) => b[1] - a[1]).map(([m, c]) => <div key={m} className="small muted">{label(m)}: {usd(c)}</div>)}</>
          : <span className="small muted">Hover or tap a day for its breakdown by model.</span>}
      </div>
    </div>
  );
}

const shortDay = (d: string) => new Date(d + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
function niceMax(v: number) {
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v) ?? v;
}
function roundTop(x: number, y: number, w: number, h: number, r: number) {
  r = Math.min(r, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function WhatIf({ u, label }: { u: Usage; label: (id: string) => string }) {
  if (!u.what_if.length) return null;
  return (
    <section className="panel"><h3 className="h3">What if?</h3>
      <p className="small muted">The same tokens priced on other models. Cost only: check answer quality (Conversations, ratings) before switching.</p>
      <div className="tbl"><table className="stack-sm">
        <thead><tr><th>Step</th><th className="num">Actual</th><th>Cheaper options</th></tr></thead>
        <tbody>{u.what_if.map((w) => {
          const cheaper = w.options.filter((o) => o.gemini_ok && o.cost < w.actual - 0.00005).slice(0, 3);
          return (
            <tr key={w.node}><td data-label="Step" className="mono">{w.node}</td><td data-label="Actual" className="num">{usd(w.actual)}</td>
              <td data-label="Cheaper">{cheaper.length ? <div className="chips">{cheaper.map((o) => (
                <span key={o.model_id} className="chip" title={o.enabled ? "" : "Switched off in Catalog"}>{label(o.model_id)} {usd(o.cost)}{o.enabled ? "" : " (off)"}</span>))}</div>
                : <span className="small muted">Already the cheapest option</span>}</td></tr>);
        })}</tbody>
      </table></div>
    </section>
  );
}

// ---------------- steps (versioned configs) ----------------

function StepsTab({ d, label, refresh }: { d: ModelsData; label: (id: string) => string; refresh: (n?: ModelsData) => void }) {
  const [edit, setEdit] = useState<{ node: NodeInfo; audience: Audience } | null>(null);
  const [hist, setHist] = useState<{ node: NodeInfo; audience: Audience } | null>(null);
  if (edit) return <ConfigForm d={d} node={edit.node} audience={edit.audience} onDone={(n) => { setEdit(null); refresh(n); }} onCancel={() => setEdit(null)} />;
  if (hist) return <History d={d} node={hist.node} audience={hist.audience} label={label} onBack={(n) => { setHist(null); if (n) refresh(n); }} />;
  return (
    <>
      <p className="small muted">Each step can use one model for everyone, or a different one for residents (public chat) and the team (signed-in). Steps marked “Gemini” need tool calling or image input.</p>
      <div className="tbl"><table className="stack-sm">
        <thead><tr><th>Step</th><th>Everyone</th><th>Residents</th><th>Team</th><th /></tr></thead>
        <tbody>{d.nodes.map((n) => (
          <tr key={n.node}>
            <td data-label="Step"><div className="mono strong">{n.node}{n.gemini_only && <span className="chip">Gemini</span>}</div><div className="small muted">{n.description}</div></td>
            {AUDIENCES.map((a) => {
              const c = n.configs[a.value], r = n.resolved[a.value], eff = n.effective[a.value];
              const inherited = a.value !== "all" && !c?.model_id;
              return (
                <td key={a.value} data-label={a.label}>
                  <button className="linkish" onClick={() => setEdit({ node: n, audience: a.value })} title={`Edit for ${a.who.toLowerCase()}`}>
                    {inherited ? <span className="muted">same as everyone</span> : label(r.model_id)}
                  </button>
                  {eff !== r.model_id && !inherited && <div className="small warn-text">running on {label(eff)}</div>}
                  <div className="small muted">
                    {c ? <button className="linkish small" onClick={() => setHist({ node: n, audience: a.value })}>v{c.version}</button> : a.value === "all" ? "built-in default" : null}
                    {c?.temperature != null && <> · temp {c.temperature}</>}{c?.max_tokens != null && <> · max {fmt(c.max_tokens)}</>}
                  </div>
                </td>);
            })}
            <td className="row-actions"><button className="btn ghost small" onClick={() => setHist({ node: n, audience: "all" })}>History</button></td>
          </tr>))}</tbody>
      </table></div>
    </>
  );
}

function ConfigForm({ d, node, audience, onDone, onCancel }: {
  d: ModelsData; node: NodeInfo; audience: Audience; onDone: (n: ModelsData) => void; onCancel: () => void;
}) {
  const live = node.configs[audience];
  const [aud, setAud] = useState<Audience>(audience);
  const cur = node.configs[aud];
  const [f, setF] = useState({ model_id: live?.model_id ?? (audience === "all" ? node.resolved.all.model_id : ""),
    temperature: live?.temperature?.toString() ?? "", max_tokens: live?.max_tokens?.toString() ?? "", note: "" });
  const { busy, error, run } = useAction();
  const options = d.models.filter((m) => m.kind === "chat" && (!node.gemini_only || m.provider === "gemini"));
  const pick = (a: Audience) => {
    const c = node.configs[a];
    setAud(a);
    setF({ model_id: c?.model_id ?? (a === "all" ? node.resolved.all.model_id : ""), temperature: c?.temperature?.toString() ?? "",
      max_tokens: c?.max_tokens?.toString() ?? "", note: "" });
  };
  const save = () => run(async () => {
    const r = await api.saveConfig({ node: node.node, audience: aud, model_id: f.model_id || null,
      temperature: f.temperature === "" ? null : Number(f.temperature), max_tokens: f.max_tokens === "" ? null : Number(f.max_tokens), note: f.note || null });
    onDone(r);
  });
  return (
    <section className="panel">
      <h3 className="h3"><span className="mono">{node.node}</span>: {node.description}</h3>
      <div className="seg" role="group" aria-label="Audience">
        {AUDIENCES.map((a) => <button key={a.value} className={aud === a.value ? "on" : ""} aria-pressed={aud === a.value} onClick={() => pick(a.value)} title={a.who}>{a.label}</button>)}
      </div>
      <p className="small muted">{AUDIENCES.find((a) => a.value === aud)!.who}. {cur ? `Live: version ${cur.version}, saved by ${cur.created_by} ${ago(cur.created_at)}.` : "No saved version yet."} Saving creates a new version.</p>
      <ErrorBox error={error} />
      <Field label="Model" hint={node.gemini_only ? "This step needs a Gemini model." : "Prices are USD per 1M tokens (input / output)."}>
        <select value={f.model_id} onChange={(e) => setF({ ...f, model_id: e.target.value })}>
          {aud !== "all" && <option value="">Same as everyone ({d.models.find((m) => m.id === node.resolved.all.model_id)?.label})</option>}
          {options.map((m) => <option key={m.id} value={m.id} disabled={!m.enabled}>
            {m.label} · {perM(m.input_per_m)} / {perM(m.output_per_m)}{m.enabled ? "" : " · off"}</option>)}
        </select>
      </Field>
      <div className="grid2">
        <Field label="Temperature" hint="Blank = the step's built-in setting. Lower is more consistent.">
          <input type="number" min={0} max={2} step={0.1} value={f.temperature} onChange={(e) => setF({ ...f, temperature: e.target.value })} placeholder="built-in" />
        </Field>
        <Field label="Max output tokens" hint="Blank = built-in. Caps the cost of a single reply.">
          <input type="number" min={16} max={32768} step={50} value={f.max_tokens} onChange={(e) => setF({ ...f, max_tokens: e.target.value })} placeholder="built-in" />
        </Field>
      </div>
      <Field label="Change note" hint="Why you're changing it; shows in the version history.">
        <input value={f.note} maxLength={300} onChange={(e) => setF({ ...f, note: e.target.value })} placeholder="e.g. Trying gpt-oss for routing to cut cost" />
      </Field>
      <div className="actions">
        <button className="btn primary" disabled={busy || (aud === "all" && !f.model_id)} onClick={save}>{busy ? "Saving…" : "Save new version"}</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </section>
  );
}

function History({ d, node, audience, label, onBack }: {
  d: ModelsData; node: NodeInfo; audience: Audience; label: (id: string) => string; onBack: (n?: ModelsData) => void;
}) {
  const [aud, setAud] = useState(audience);
  const { data, error } = useLoad(() => api.configHistory(node.node, aud), [aud]);
  const { busy, error: rErr, run } = useAction();
  const restore = (v: AgentConfig) => run(async () => onBack(await api.restoreConfig(v.id)));
  void d;
  return (
    <section className="panel">
      <div className="row-end wrap"><h3 className="h3"><span className="mono">{node.node}</span> history</h3><span className="spacer" />
        <button className="btn ghost small" onClick={() => onBack()}>Back to steps</button></div>
      <div className="seg" role="group" aria-label="Audience">
        {AUDIENCES.map((a) => <button key={a.value} className={aud === a.value ? "on" : ""} aria-pressed={aud === a.value} onClick={() => setAud(a.value)}>{a.label}</button>)}
      </div>
      <ErrorBox error={error ?? rErr} />
      {!data ? <p className="muted">Loading…</p> : data.versions.length === 0 ? <Empty title="No versions yet">This step uses its built-in default.</Empty> : (
        <div className="tbl"><table className="stack-sm">
          <thead><tr><th>Version</th><th>Model</th><th>Settings</th><th>Note</th><th className="num">Calls · cost while live</th><th /></tr></thead>
          <tbody>{data.versions.map((v, i) => (
            <tr key={v.id}>
              <td data-label="Version"><b>v{v.version}</b>{i === 0 && <span className="chip st-succeeded">live</span>}<div className="small muted">{v.created_by} · {ago(v.created_at)}</div></td>
              <td data-label="Model">{v.model_id ? label(v.model_id) : <span className="muted">same as everyone</span>}</td>
              <td data-label="Settings" className="small">{v.temperature != null ? `temp ${v.temperature}` : "temp built-in"} · {v.max_tokens != null ? `max ${fmt(v.max_tokens)}` : "max built-in"}{v.prompt ? " · custom prompt" : ""}</td>
              <td data-label="Note" className="small">{v.note ?? "—"}</td>
              <td data-label="Calls · cost" className="num">{fmt(v.calls ?? 0)} · {usd(v.cost ?? 0)}{v.p50_ms != null && <div className="small muted">p50 {(v.p50_ms / 1000).toFixed(1)}s</div>}</td>
              <td className="row-actions">{i > 0 && <button className="btn ghost small" disabled={busy} onClick={() => restore(v)}>Restore</button>}</td>
            </tr>))}</tbody>
        </table></div>)}
    </section>
  );
}

// ---------------- catalog ----------------

const blankModel: LlmModel = { id: "", kind: "chat", provider: "gemini", name: "", region: "global", label: "", thinking: null, input_per_m: null,
  cached_per_m: null, output_per_m: null, per_call_usd: null, price_source: "", enabled: false, note: "" };

function CatalogTab({ d, refresh }: { d: ModelsData; refresh: (n?: ModelsData) => void }) {
  const [edit, setEdit] = useState<{ isNew: boolean; m: LlmModel } | null>(null);
  const [probes, setProbes] = useState<Record<string, Probe | "running">>({});
  const { error, run } = useAction();
  const test = async (id: string) => {
    setProbes((p) => ({ ...p, [id]: "running" }));
    try { const r = await api.testModel(id); setProbes((p) => ({ ...p, [id]: r })); }
    catch (e) { setProbes((p) => ({ ...p, [id]: { ok: false, error: String((e as Error).message) } })); }
  };
  if (edit) return <ModelForm d={d} isNew={edit.isNew} initial={edit.m} onDone={(n) => { setEdit(null); refresh(n); }} onCancel={() => setEdit(null)} />;
  return (
    <>
      <div className="toolbar"><p className="small muted grow">Prices from Google's Cloud Billing catalog, in USD per 1M tokens. A model can only be switched on once it has prices, so nothing runs unmetered. Test sends one tiny paid call.</p>
        <button className="btn primary" onClick={() => setEdit({ isNew: true, m: blankModel })}>Add model</button></div>
      <ErrorBox error={error} />
      <div className="tbl"><table className="stack-sm">
        <thead><tr><th>Model</th><th className="num">Input</th><th className="num">Cached</th><th className="num">Output</th><th>Status</th><th className="num">30 days</th><th /></tr></thead>
        <tbody>{d.models.map((m) => {
          const p = probes[m.id];
          return (
            <tr key={m.id} className={m.enabled ? "" : "disabled"}>
              <td data-label="Model"><div className="strong">{m.label}</div><div className="small muted mono break">{m.provider} · {m.name}{m.region !== "global" ? ` · ${m.region}` : ""}</div>
                {m.assigned_to?.length ? <div className="small muted">Used by: {m.assigned_to.join(", ")}</div> : null}</td>
              {m.kind === "rerank"
                ? <td data-label="Price" className="num" colSpan={3}>{m.per_call_usd ? `${usd(m.per_call_usd, 3)} per search` : "Free (runs on our server)"}</td>
                : <><td data-label="Input" className="num">{perM(m.input_per_m)}</td><td data-label="Cached" className="num">{perM(m.cached_per_m)}</td><td data-label="Output" className="num">{perM(m.output_per_m)}</td></>}
              <td data-label="Status">
                {m.enabled ? <span className="chip st-succeeded">on</span> : <span className="chip muted">off</span>}
                {m.kind !== "chat" && <span className="chip">{m.kind}</span>}
                {!m.priced && <span className="chip st-failed" title="No price: calls would be unmetered">no price</span>}
                {m.note && <div className="small muted">{m.note}</div>}
                {p === "running" && <div className="small muted">Testing…</div>}
                {p && p !== "running" && (p.ok ? <div className="small ok-text">Works · {((p.latency_ms ?? 0) / 1000).toFixed(1)}s</div>
                  : p.ok === null ? <div className="small muted">{p.error}</div>
                  : <div className="small crit-text" title={p.error}>{p.kind === "quota" ? "No quota yet (429)" : p.kind === "not_found" ? "Not available in this region" : p.kind === "permission" ? "No access" : "Failed"}</div>)}
              </td>
              <td data-label="30 days" className="num">{usd(m.cost_30d ?? 0)}<div className="small muted">{fmt(m.calls_30d ?? 0)} calls · {m.last_used ? ago(m.last_used) : "unused"}</div>
                {(m.unpriced_30d ?? 0) > 0 && m.priced && <button className="linkish small" onClick={() => run(async () => { await api.repriceModel(m.id); refresh(); })}>Re-price {m.unpriced_30d} calls</button>}</td>
              <td className="row-actions">
                <button className="btn ghost small" disabled={p === "running"} onClick={() => test(m.id)}>Test</button>
                <button className="btn ghost small" onClick={() => setEdit({ isNew: false, m })}>Edit</button>
              </td>
            </tr>);
        })}</tbody>
      </table></div>
    </>
  );
}

function ModelForm({ d, isNew, initial, onDone, onCancel }: { d: ModelsData; isNew: boolean; initial: LlmModel; onDone: (n: ModelsData) => void; onCancel: () => void }) {
  const [m, setM] = useState<LlmModel>(initial);
  const { busy, error, run } = useAction();
  const num = (v: string) => (v === "" ? null : Number(v));
  const set = <K extends keyof LlmModel>(k: K, v: LlmModel[K]) => setM({ ...m, [k]: v });
  const save = () => run(async () => onDone(isNew ? await api.addModel(m) : await api.saveModel(m)));
  const remove = () => run(async () => onDone(await api.removeModel(m.id)));
  void d;
  return (
    <section className="panel">
      <h3 className="h3">{isNew ? "Add a model" : m.label}</h3>
      <ErrorBox error={error} />
      <div className="grid2">
        <Field label="Id" hint="Short and permanent, e.g. gemini-pro"><input value={m.id} disabled={!isNew} onChange={(e) => set("id", e.target.value.toLowerCase())} /></Field>
        <Field label="Label"><input value={m.label} onChange={(e) => set("label", e.target.value)} /></Field>
        <Field label="Kind">
          <select value={m.kind} onChange={(e) => setM({ ...m, kind: e.target.value as LlmModel["kind"], provider: e.target.value === "rerank" ? "vertex_search" : m.provider === "vertex_search" ? "gemini" : m.provider })} disabled={!isNew}>
            <option value="chat">Chat (assignable to steps)</option><option value="embed">Embedding</option><option value="rerank">Reranker</option>
          </select></Field>
        <Field label="Provider">
          <select value={m.provider} onChange={(e) => set("provider", e.target.value as LlmModel["provider"])} disabled={m.kind === "embed"}>
            <option value="gemini">Gemini (Vertex)</option><option value="claude">Claude (Vertex)</option><option value="maas">Open models (Vertex MaaS)</option>
            {m.kind === "rerank" && <><option value="local">Local (runs on our server, free)</option><option value="vertex_search">Vertex AI Search ranking</option></>}
          </select></Field>
        <Field label="Provider model name" hint="As Vertex names it, e.g. gemini-3.8-flash"><input className="mono" value={m.name} onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="Region"><input value={m.region} onChange={(e) => set("region", e.target.value)} /></Field>
      </div>
      {m.kind === "rerank"
        ? <Field label="Price per request (USD)"><input type="number" min={0} step={0.0001} value={m.per_call_usd ?? ""} onChange={(e) => set("per_call_usd", num(e.target.value))} /></Field>
        : <div className="grid3">
            <Field label="Input $/1M tokens"><input type="number" min={0} step={0.01} value={m.input_per_m ?? ""} onChange={(e) => set("input_per_m", num(e.target.value))} /></Field>
            <Field label="Cached input $/1M" hint="Blank = same as input"><input type="number" min={0} step={0.01} value={m.cached_per_m ?? ""} onChange={(e) => set("cached_per_m", num(e.target.value))} /></Field>
            <Field label="Output $/1M" hint="Thinking tokens bill as output"><input type="number" min={0} step={0.01} value={m.output_per_m ?? ""} onChange={(e) => set("output_per_m", num(e.target.value))} /></Field>
          </div>}
      <div className="grid2">
        <Field label="Price source"><input value={m.price_source ?? ""} onChange={(e) => set("price_source", e.target.value)} placeholder="e.g. Cloud Billing catalog, 2026-11-20" /></Field>
        {m.provider === "gemini" && m.kind === "chat" && <Field label="Thinking level" hint="Higher thinks longer: better on hard questions, more output tokens">
          <select value={m.thinking ?? ""} onChange={(e) => set("thinking", (e.target.value || null) as LlmModel["thinking"])}>
            <option value="">Model default</option><option value="minimal">Minimal</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option>
          </select></Field>}
      </div>
      <Field label="Note"><input value={m.note ?? ""} onChange={(e) => set("note", e.target.value)} placeholder="e.g. Quota pending" /></Field>
      <label className="check"><input type="checkbox" checked={m.enabled} onChange={(e) => set("enabled", e.target.checked)} /> {m.kind === "rerank" ? "Use this reranker (only one can be on; this switches the others off)" : "Switched on (available to steps)"}</label>
      <div className="actions">
        <button className="btn primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
        {!isNew && <><span className="spacer" /><button className="btn ghost danger" disabled={busy} onClick={remove}>Delete</button></>}
      </div>
    </section>
  );
}

// ---------------- budget & runtime ----------------

function SettingsTab({ d, refresh }: { d: ModelsData; refresh: (n?: ModelsData) => void }) {
  const { month_to_date: _m, projected: _p, day: _d, days_in_month: _n, guard_active: _g, alert: _a, ...b0 } = d.budget;
  void _m; void _p; void _d; void _n; void _g; void _a;
  const [b, setB] = useState<Budget>(b0);
  const [rt, setRt] = useState<Runtime>(d.runtime);
  const { busy, error, run } = useAction();
  const [saved, setSaved] = useState(false);
  const chat = d.models.filter((m) => m.kind === "chat" && m.enabled);
  const save = () => run(async () => { refresh(await api.saveLlmSettings(rt, b)); setSaved(true); });
  return (
    <section className="panel">
      <ErrorBox error={error} />
      <h3 className="h3">Monthly budget</h3>
      <p className="small muted">Covers every model, embedding and ranking call Cypress makes. Your Google Cloud budget alert still covers the whole project.</p>
      <div className="grid3">
        <Field label="Budget (USD per month)"><input type="number" min={0} step={1} value={b.monthly_usd} onChange={(e) => setB({ ...b, monthly_usd: Number(e.target.value) })} /></Field>
        <Field label="Warn at (% of budget)" hint="Shows a warning at the top of this page"><input type="number" min={1} max={100} value={b.alert_pct} onChange={(e) => setB({ ...b, alert_pct: Number(e.target.value) })} /></Field>
        <Field label="When the budget is reached, use" hint="Every step switches to this model for the rest of the month">
          <select value={b.guard_model} onChange={(e) => setB({ ...b, guard_model: e.target.value })}>{chat.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}</select></Field>
      </div>
      <label className="check"><input type="checkbox" checked={b.guard} onChange={(e) => setB({ ...b, guard: e.target.checked })} /> Enforce the budget (switch to the cheaper model at 100%)</label>
      <h3 className="h3 mt">Fallback and speed</h3>
      <p className="small muted">When a model is slow to start, Cypress also asks the fallback model and uses whichever answers first. That keeps replies quick but bills both calls. Longer waits mean fewer backup calls.</p>
      <div className="grid2">
        <Field label="Fallback model"><select value={rt.fallback_model} onChange={(e) => setRt({ ...rt, fallback_model: e.target.value })}>{chat.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}</select></Field>
        <Field label="Request timeout (seconds)"><input type="number" min={5} max={120} value={rt.timeout_s} onChange={(e) => setRt({ ...rt, timeout_s: Number(e.target.value) })} /></Field>
        <Field label="Ask the fallback after (seconds)" hint="For routing, grading and tools"><input type="number" min={0.5} max={30} step={0.5} value={rt.hedge_s} onChange={(e) => setRt({ ...rt, hedge_s: Number(e.target.value) })} /></Field>
        <Field label="…or for streamed answers, after (seconds)" hint="Time to first word"><input type="number" min={0.5} max={30} step={0.5} value={rt.stream_hedge_s} onChange={(e) => setRt({ ...rt, stream_hedge_s: Number(e.target.value) })} /></Field>
      </div>
      <div className="actions"><button className="btn primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>{saved && !busy && !error && <span className="ok-text small">Saved</span>}</div>
    </section>
  );
}

// ---------------- protection ----------------

const REASONS: Record<string, string> = {
  rate_hour: "Hourly limit reached", rate_day: "Daily limit reached", spend_cap: "Visitor spend cap reached",
  app_check_missing: "No App Check token", app_check_invalid: "Invalid App Check token",
  off_topic: "Off-topic question", off_topic_strikes: "Too many off-topic questions",
};
const MODES: [ProtectionSettings["app_check"], string, string][] = [
  ["off", "Off", "Don't check. Rate limits and the spend cap still apply."],
  ["monitor", "Monitor", "Check and count, but let requests through. Use this to confirm real visitors pass before enforcing."],
  ["enforce", "Enforce", "Visitors without a valid token are asked to sign in. Signed-in members are never blocked."],
];

function ProtectionTab() {
  const [days, setDays] = useState(7);
  const { data, error, reload } = useLoad(() => api.protection(days), [days]);
  const [next, setNext] = useState<ProtectionData | null>(null);
  const d = next && next.days === days ? next : data;
  if (error) return <ErrorBox error={error} />;
  if (!d) return <p className="muted">Loading…</p>;
  const blocked = d.by_reason.filter((r) => r.enforced).reduce((a, r) => a + r.n, 0);
  const flagged = d.by_reason.filter((r) => !r.enforced).reduce((a, r) => a + r.n, 0);
  const checks = (d.app_check.verified ?? 0) + (d.app_check.missing ?? 0) + (d.app_check.invalid ?? 0);
  const passRate = checks ? (d.app_check.verified ?? 0) / checks : null;
  const cap = d.settings.anon_daily_usd;
  return (
    <>
      <div className="toolbar"><span className="small muted">Last</span>
        <select value={days} onChange={(e) => { setNext(null); setDays(Number(e.target.value)); }} aria-label="Period">
          {[1, 7, 30, 90].map((n) => <option key={n} value={n}>{n === 1 ? "day" : `${n} days`}</option>)}</select>
        <button className="btn ghost small" onClick={() => { setNext(null); reload(); }}>Refresh</button></div>
      <div className="kpis">
        <Kpi label="Blocked or throttled" value={fmt(blocked)} sub={blocked ? `${new Set(d.by_reason.filter((r) => r.enforced).map((r) => r.reason)).size} kinds` : "Nothing refused"} />
        <Kpi label="Flagged, let through" value={fmt(flagged)} sub={d.settings.app_check === "monitor" ? "Monitor mode" : "Before enforcing"} />
        <Kpi label="App Check pass rate" value={passRate == null ? "—" : `${Math.round(passRate * 100)}%`} sub={checks ? `${fmt(checks)} visitor requests checked` : d.settings.app_check === "off" ? "Checking is off" : "No visitor requests yet"} />
        <Kpi label="Visitor spend today" value={usd(d.anon_spend_today)} sub={cap ? `Cap ${usd(cap, 2)} a day` : "No cap"} meter={cap ? d.anon_spend_today / cap : undefined} />
      </div>
      <Readiness d={d} passRate={passRate} checks={checks} />
      <section className="panel"><h3 className="h3">What was refused or flagged</h3>
        {d.by_reason.length === 0 ? <Empty title="Nothing refused or flagged in this period." /> : (
          <div className="tbl"><table className="stack-sm">
            <thead><tr><th>Reason</th><th>Outcome</th><th className="num">Times</th><th className="num">Visitors</th><th>Last seen</th></tr></thead>
            <tbody>{d.by_reason.map((r) => (
              <tr key={r.reason + r.enforced}>
                <td data-label="Reason">{REASONS[r.reason] ?? r.reason}</td>
                <td data-label="Outcome">{r.enforced ? <span className="chip st-failed">Refused</span> : <span className="chip muted">Let through</span>}</td>
                <td data-label="Times" className="num">{fmt(r.n)}</td>
                <td data-label="Visitors" className="num">{fmt(r.visitors)}</td>
                <td data-label="Last seen">{ago(r.last)}</td>
              </tr>))}</tbody>
          </table></div>)}
        {d.top_visitors.length > 0 && <>
          <h3 className="h3 mt">Busiest visitors</h3>
          <p className="small muted">Visitors are shown as a short code, never an IP address. One code with many entries usually means a script.</p>
          <div className="tbl"><table className="stack-sm">
            <thead><tr><th>Visitor</th><th className="num">Times</th><th>Reasons</th><th>Last seen</th></tr></thead>
            <tbody>{d.top_visitors.map((v) => (
              <tr key={v.visitor}><td data-label="Visitor" className="mono">{v.visitor}</td><td data-label="Times" className="num">{fmt(v.n)}</td>
                <td data-label="Reasons">{v.reasons.map((r) => REASONS[r] ?? r).join(", ")}</td><td data-label="Last seen">{ago(v.last)}</td></tr>))}</tbody>
          </table></div></>}
      </section>
      <ProtectionSettingsPanel key={JSON.stringify(d.settings)} s0={d.settings} onSaved={(n) => setNext({ ...n, days: 7 }) } />
    </>
  );
}

function Readiness({ d, passRate, checks }: { d: ProtectionData; passRate: number | null; checks: number }) {
  if (d.settings.app_check !== "monitor" || passRate == null || checks < 20) return null;
  return passRate >= 0.95
    ? <div className="panel slim okp" role="status"><b>Ready to enforce.</b> {Math.round(passRate * 100)}% of visitor requests carried a valid App Check token. Switching to Enforce would have refused {fmt(checks - (d.app_check.verified ?? 0))} of {fmt(checks)}.</div>
    : <div className="panel slim warn" role="status"><b>Not ready to enforce yet.</b> Only {Math.round(passRate * 100)}% of visitor requests passed App Check. Some may be scripts; some may be visitors whose browsers block reCAPTCHA. They'd be asked to sign in.</div>;
}

function ProtectionSettingsPanel({ s0, onSaved }: { s0: ProtectionSettings; onSaved: (d: ProtectionData) => void }) {
  const [s, setS] = useState(s0);
  const { busy, error, run } = useAction();
  const num = (k: keyof ProtectionSettings, step = 1) => (
    <input type="number" min={0} step={step} value={s[k] as number} onChange={(e) => setS({ ...s, [k]: Number(e.target.value) })} />);
  return (
    <section className="panel">
      <ErrorBox error={error} />
      <h3 className="h3">App Check</h3>
      <p className="small muted">The chat site proves each request comes from the real Cypress website with an invisible reCAPTCHA check. Scripts calling the API directly can't. Free up to 10,000 checks a month.</p>
      <div className="choices" role="radiogroup" aria-label="App Check mode">
        {MODES.map(([m, l, hint]) => (
          <label key={m} className={`choice${s.app_check === m ? " on" : ""}`}>
            <input type="radio" name="app_check" checked={s.app_check === m} onChange={() => setS({ ...s, app_check: m })} />
            <span><b>{l}</b><span className="small muted">{hint}</span></span>
          </label>))}
      </div>
      <h3 className="h3 mt">Question limits</h3>
      <p className="small muted">Per visitor (by network address) or per signed-in person. Photo and video uploads count separately against the same limits. Visitors who reach a limit are invited to sign in.</p>
      <div className="grid2">
        <Field label="Visitors: questions per hour">{num("anon_per_hour")}</Field>
        <Field label="Visitors: questions per day">{num("anon_per_day")}</Field>
        <Field label="Signed in: questions per hour">{num("user_per_hour")}</Field>
        <Field label="Signed in: questions per day">{num("user_per_day")}</Field>
      </div>
      <h3 className="h3 mt">Daily visitor spend cap</h3>
      <div className="grid2">
        <Field label="Cap (USD per day, 0 = none)" hint="Past this, visitors are asked to sign in until midnight; members keep chatting. A question costs about $0.0075.">{num("anon_daily_usd", 0.5)}</Field>
      </div>
      <h3 className="h3 mt">Off-topic questions</h3>
      <div className="grid2">
        <Field label="Off-topic questions per hour before sign-in (0 = off)" hint="Cypress already spots off-topic questions with its cheapest model and gives a short fixed reply. A visitor who keeps at it is asked to sign in. No extra cost.">{num("off_topic_strikes")}</Field>
      </div>
      <div className="actions"><button className="btn primary" disabled={busy} onClick={() => run(async () => onSaved(await api.saveProtection(s)))}>{busy ? "Saving…" : "Save"}</button></div>
    </section>
  );
}
