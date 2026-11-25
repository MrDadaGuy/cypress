import { useEffect, useState } from "react";
import { api, type LegendColor, type MemberTypes, type SyncView, type TabKind, type WorkbookChange, type WorkbookPreview } from "./api";
import { ago, ErrorBox, Field, fmt, useAction, useLoad } from "./ui";

const KINDS: [TabKind, string][] = [["households", "Members"], ["former", "Former members (inactive)"], ["golf_carts", "Golf carts"],
  ["vendors", "Vendor gate cards"], ["ignore", "Don't import"]];
const FALLBACK_TYPES: MemberTypes = { types: [{ key: "full", label: "Full member" }], default: "full" };
const swatch = (hex: string) => (/^[0-9A-F]{6}$/.test(hex) ? `#${hex}` : "transparent");

/** CAM's membership workbook: where it lives, the nightly sync, uploads, the color legend and membership types. */
function norm(v: SyncView): SyncView { return { ...v, types: v.types?.types ? v.types : FALLBACK_TYPES, colors: v.colors ?? {} }; }

export default function WorkbookTab({ onImported }: { onImported: () => void }) {
  const { data, error, reload } = useLoad(api.memberSync);
  const [v, setV] = useState<SyncView | null>(null);
  useEffect(() => { if (data) setV(norm(data)); }, [data]);
  if (error) return <ErrorBox error={error} />;
  if (!v) return <p className="muted">Loading…</p>;
  return (
    <>
      <p className="small muted">CAM's membership workbook is the record Cypress uses: households and member names, up to three vehicles each, gate cards, golf carts, vendor cards, and the cell colors CAM uses as part of the record. Tabs are recognized by their columns, not their names, so next year's file works without setup. Each import says what changed since the last one, so you only need to check with CAM when something does.</p>
      <SyncPanel v={v} setV={(x) => setV(norm(x))} onImported={onImported} />
      <UploadPanel tabKinds={v.settings.tab_kinds} onDone={() => { reload(); onImported(); }} />
      <LegendPanel v={v} setV={(x) => setV(norm(x))} />
      <TypesPanel v={v} setV={(x) => setV(norm(x))} />
      <RunsPanel v={v} />
    </>
  );
}

function SyncPanel({ v, setV, onImported }: { v: SyncView; setV: (v: SyncView) => void; onImported: () => void }) {
  const s = v.settings;
  const [file, setFile] = useState(s.file_url ?? "");
  const [contacts, setContacts] = useState(s.contacts_file_url ?? "");
  const [enabled, setEnabled] = useState(s.enabled);
  const [kinds, setKinds] = useState<Record<string, TabKind>>(s.tab_kinds ?? {});
  const [p, setP] = useState<(WorkbookPreview & { file_id: string }) | null>(null);
  const { busy, error, run } = useAction();
  const changed = file !== (s.file_url ?? "");
  const contactsChanged = contacts !== (s.contacts_file_url ?? "");
  return (
    <section className="panel">
      <h3 className="h3">The workbook in Google Drive</h3>
      <p className="small muted">Share the file with <span className="mono break">{v.service_account}</span> as a <b>Viewer</b> (and leave “viewers can download” on). Cypress reads it each night at 3:30 AM. When CAM starts a new file for a new year, paste its link here and press Check.</p>
      <ErrorBox error={error} />
      <Field label="File link" hint={s.file_name ? `Now: ${s.file_name}` : "A Google Sheets or Google Drive link"}>
        <input value={file} placeholder="https://docs.google.com/spreadsheets/d/…" onChange={(e) => { setFile(e.target.value); setP(null); }} /></Field>
      <Field label="Contacts file (optional)" hint={s.contacts_file_name ? `Now: ${s.contacts_file_name}` : "If the email addresses are in a separate file. Emails are matched to households by Vantaca account, else address."}>
        <input value={contacts} placeholder="https://docs.google.com/spreadsheets/d/…" onChange={(e) => setContacts(e.target.value)} /></Field>
      <div className="row-wrap">
        <button className="btn" disabled={busy || !file.trim()} onClick={() => run(async () => setP(await api.checkSync(file)))}>Check</button>
        {contacts.trim() && <button className="btn" disabled={busy} onClick={() => run(async () => setP(await api.checkSync(contacts)))}>Check contacts</button>}
        <label className="check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Sync every night</label>
        <button className="btn primary" disabled={busy || (changed && !p)} title={changed && !p ? "Check the new file first" : ""}
          onClick={() => run(async () => setV(await api.saveSync({ file: changed ? file : null, contacts_file: contactsChanged ? contacts.trim() : null, enabled, replace: s.replace, min_fraction: s.min_fraction, tab_kinds: kinds })))}>Save</button>
        {s.enabled && <button className="btn ghost" disabled={busy} onClick={() => run(async () => { setV(await api.runSync()); onImported(); })}>Sync now</button>}
      </div>
      {p && <Preview p={p} kinds={kinds} setKinds={setKinds} />}
    </section>
  );
}

function UploadPanel({ tabKinds, onDone }: { tabKinds: Record<string, TabKind>; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [p, setP] = useState<WorkbookPreview | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  const pick = (f: File | null) => { setFile(f); setP(null); setResult(null); if (f) run(async () => setP(await api.previewWorkbook(f))); };
  return (
    <section className="panel">
      <h3 className="h3">Upload a copy</h3>
      <p className="small muted">For a one-off import, upload the workbook as .xlsx (in Google Sheets: File → Download → Microsoft Excel). It's read in memory and not stored.</p>
      <ErrorBox error={error} />
      <Field label="Workbook (.xlsx)"><input type="file" accept=".xlsx" onChange={(e) => pick(e.target.files?.[0] ?? null)} /></Field>
      {busy && !p && <p className="muted">Reading…</p>}
      {p && <>
        <Preview p={p} kinds={tabKinds} />
        <div className="actions"><button className="btn primary" disabled={busy} onClick={() => file && run(async () => {
          const r = await api.importWorkbook(file, true);
          const h = r.stats.households;
          setResult(`Imported: ${h?.created ?? 0} new and ${h?.updated ?? 0} updated households${h?.deactivated ? `, ${h.deactivated} marked inactive` : ""}, ${fmt(r.stats.vehicles ? r.stats.vehicles.created + r.stats.vehicles.updated : 0)} vehicles.${r.warnings.length ? " " + r.warnings.join(" ") : ""}`);
          onDone();
        })}>{busy ? "Importing…" : "Import"}</button></div></>}
      {result && <p className="ok-text" role="status">{result}</p>}
    </section>
  );
}

function Changes({ list }: { list: WorkbookChange[] }) {
  if (!list.length) return <p className="small ok-text">Nothing new since the last import.</p>;
  return <ul className="plain small">{list.map((c, i) => <li key={i}>{c.level === "alert" ? "⚠️" : "ℹ️"} {c.what}</li>)}</ul>;
}

function Preview({ p, kinds, setKinds }: { p: WorkbookPreview; kinds: Record<string, TabKind>; setKinds?: (k: Record<string, TabKind>) => void }) {
  const c = p.counts;
  return (
    <div className="stack mt">
      {p.file_name && <p className="small"><b>{p.file_name}</b>{p.modified && <span className="muted"> · changed {ago(p.modified)}</span>}</p>}
      {c.emails ? <p className="small">{fmt(c.emails)} email addresses for {fmt(c.contacts ?? 0)} households.</p> : null}
      <p className="small">{fmt(c.households)} member households ({fmt(c.people)} names), {fmt(c.inactive)} former, {fmt(c.vehicles)} vehicles, {fmt(c.gate_cards)} gate cards, {fmt(c.golf_carts)} golf carts{c.golf_carts_unmatched ? ` (${c.golf_carts_unmatched} not matched to a household)` : ""}, {fmt(c.vendor_cards)} vendor cards.</p>
      <div className="tbl"><table className="stack-sm">
        <thead><tr><th>Tab</th><th>Rows</th><th>Read as</th></tr></thead>
        <tbody>{p.tabs.map((t) => (
          <tr key={t.name}><td data-label="Tab">{t.name}</td><td data-label="Rows">{fmt(t.rows)}</td>
            <td data-label="Read as">{setKinds
              ? <select value={kinds[t.name] ?? t.kind} onChange={(e) => setKinds({ ...kinds, [t.name]: e.target.value as TabKind })}>
                  {KINDS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
              : KINDS.find(([k]) => k === t.kind)?.[1]}</td></tr>))}</tbody>
      </table></div>
      <h4>What changed</h4><Changes list={p.changes} />
    </div>
  );
}

function LegendPanel({ v, setV }: { v: SyncView; setV: (v: SyncView) => void }) {
  const [rows, setRows] = useState<Record<string, LegendColor>>(v.colors);
  const { busy, error, run } = useAction();
  useEffect(() => setRows(v.colors), [v.colors]);
  const keys = Object.keys(rows).sort((a, b) => (rows[b].count ?? 0) - (rows[a].count ?? 0));
  const set = (k: string, p: Partial<LegendColor>) => setRows({ ...rows, [k]: { ...rows[k], ...p } });
  return (
    <section className="panel">
      <h3 className="h3">Color legend</h3>
      <p className="small muted">The colors CAM uses in the workbook and what each means. New colors appear here automatically (and in the morning digest) with no meaning until you set one. A row color can also mark a household inactive or set its membership type.</p>
      <ErrorBox error={error} />
      {!keys.length ? <p className="small muted">No colors yet. Import the workbook to see them.</p> : <div className="tbl"><table className="stack-sm">
        <thead><tr><th>Color</th><th>Name</th><th>Meaning</th><th>Effect</th><th>Seen in</th></tr></thead>
        <tbody>{keys.map((k) => { const c = rows[k]; return (
          <tr key={k}>
            <td data-label="Color"><span className="swatch" style={{ background: swatch(k) }} /> <span className="mono small">#{k}</span>
              {c.new && <span className="chip warn">new</span>}{c.aliases && c.aliases.length > 0 && <div className="small muted">also {c.aliases.map((a) => "#" + a).join(", ")}</div>}</td>
            <td data-label="Name"><input value={c.name} maxLength={40} onChange={(e) => set(k, { name: e.target.value })} aria-label={`Name for #${k}`} /></td>
            <td data-label="Meaning"><input value={c.meaning ?? ""} maxLength={300} placeholder="Ask CAM" onChange={(e) => set(k, { meaning: e.target.value || null })} aria-label={`Meaning of #${k}`} /></td>
            <td data-label="Effect"><select value={c.effect ?? ""} onChange={(e) => set(k, { effect: e.target.value || null })} aria-label={`Effect of #${k}`}>
              <option value="">None (shown only)</option><option value="inactive">Household inactive</option>
              {v.types.types.map((t) => <option key={t.key} value={`type:${t.key}`}>Type: {t.label}</option>)}</select></td>
            <td data-label="Seen in" className="small muted">{c.count ? `${fmt(c.count)} cells · ` : ""}{(c.where ?? []).join(", ")}</td>
          </tr>); })}</tbody>
      </table></div>}
      {keys.length > 0 && <div className="actions"><button className="btn primary" disabled={busy} onClick={() => run(async () => setV(await api.saveLegend(
        Object.fromEntries(keys.map((k) => [k, { name: rows[k].name, meaning: rows[k].meaning, effect: rows[k].effect }])))))}>Save legend</button>
        <span className="small muted">Applies on the next import or sync.</span></div>}
    </section>
  );
}

function TypesPanel({ v, setV }: { v: SyncView; setV: (v: SyncView) => void }) {
  const [t, setT] = useState<MemberTypes>(v.types);
  const { busy, error, run } = useAction();
  const upd = (i: number, p: Partial<MemberTypes["types"][number]>) => setT({ ...t, types: t.types.map((x, j) => (j === i ? { ...x, ...p } : x)) });
  return (
    <section className="panel">
      <h3 className="h3">Membership types</h3>
      <p className="small muted">Business rules will differ by type. A household gets a type from a legend color (above), otherwise the default.</p>
      <ErrorBox error={error} />
      {t.types.map((x, i) => (
        <div key={i} className="grid3">
          <Field label="Key"><input value={x.key} maxLength={30} onChange={(e) => upd(i, { key: e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "") })} /></Field>
          <Field label="Label"><input value={x.label} maxLength={60} onChange={(e) => upd(i, { label: e.target.value })} /></Field>
          <Field label="Notes"><input value={x.notes ?? ""} maxLength={500} placeholder="e.g. dues, privileges" onChange={(e) => upd(i, { notes: e.target.value || null })} /></Field>
        </div>))}
      <div className="row-wrap">
        <button className="btn ghost small" onClick={() => setT({ ...t, types: [...t.types, { key: "", label: "" }] })}>Add type</button>
        <Field label="Default type"><select value={t.default} onChange={(e) => setT({ ...t, default: e.target.value })}>
          {t.types.filter((x) => x.key).map((x) => <option key={x.key} value={x.key}>{x.label || x.key}</option>)}</select></Field>
        <button className="btn primary" disabled={busy || t.types.some((x) => !x.key || !x.label)} onClick={() => run(async () => setV(await api.saveTypes(t)))}>Save types</button>
      </div>
    </section>
  );
}

function RunsPanel({ v }: { v: SyncView }) {
  if (!v.runs.length) return null;
  return (
    <section className="panel">
      <h3 className="h3">Recent imports</h3>
      <ul className="plain">{v.runs.map((r) => (
        <li key={r.id} className="small">
          <span className={`chip ${r.status === "ok" ? "st-succeeded" : r.status === "failed" ? "warn" : "muted"}`}>{r.status}</span> {r.trigger} · {ago(r.at)}
          {r.stats.households && <> · {r.stats.households.created + r.stats.households.updated} households{r.stats.households.deactivated ? `, ${r.stats.households.deactivated} marked inactive` : ""}</>}
          {r.error && <div className="warn-text">{r.error}</div>}
          {(r.stats.warnings?.length || r.stats.changes?.length) ? <details><summary>What changed</summary>
            {r.stats.warnings?.map((w, i) => <p key={i} className="warn-text">{w}</p>)}<Changes list={r.stats.changes ?? []} /></details> : null}
        </li>))}</ul>
    </section>
  );
}
