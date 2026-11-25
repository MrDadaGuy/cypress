import { useState } from "react";
import { api, IMPORT_FIELDS, VEHICLE_FIELDS, type CasesSettings, type VehiclePreview, type Household, type HouseholdIn, type ImportPreview, type LookupData, type LookupMode, type LookupSettings, type MemberAuth, type MembersData } from "./api";
import { ago, Empty, ErrorBox, Field, fmt, useAction, useLoad } from "./ui";
import WorkbookTab from "./WorkbookTab";

type Tab = "list" | "workbook" | "import" | "vehicles" | "settings" | "lookup";
const blank: HouseholdIn = { name: "", address: "", account_ref: "", status: "active", notes: "", emails: [{ email: "", name: "" }] };

export default function MembersPage() {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("all");
  const { data, error, reload } = useLoad(() => api.members(q, status), [q, status]);
  const [tab, setTab] = useState<Tab>("list");
  if (error) return <ErrorBox error={error} />;
  return (
    <>
      <header className="page-head"><div><h1>Members</h1>
        <p className="sub">The membership list: households and the one or two email addresses on file for each. Residents who sign in with one of these addresses become members in Cypress (they can report issues and see member-only documents). Only admins can see this page. In chat, member details reach only the board, admins and security, through an audited lookup at the level you set for each.</p></div></header>
      {data && <div className="kpis">
        <div className="kpi"><div className="kpi-l">Active households</div><div className="kpi-v">{fmt(data.counts.active)}</div><div className="kpi-s">of {fmt(data.counts.households)} listed</div></div>
        <div className="kpi"><div className="kpi-l">Email addresses</div><div className="kpi-v">{fmt(data.counts.emails)}</div></div>
        <div className="kpi"><div className="kpi-l">Have signed in</div><div className="kpi-v">{fmt(data.counts.signed_in)}</div></div>
        <div className="kpi"><div className="kpi-l">Reports need sign-in</div><div className="kpi-v">{data.settings.require_for_reports ? "Yes" : "No"}</div></div>
      </div>}
      <div className="tabs" role="tablist">
        {([["list", "Households"], ["workbook", "CAM workbook"], ["import", "Import CSV"], ["vehicles", "Vehicles"], ["settings", "Sign-in settings"], ["lookup", "Member lookup"]] as [Tab, string][]).map(([t, l]) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{l}</button>))}
      </div>
      {tab === "list" && <ListTab data={data} q={q} setQ={setQ} status={status} setStatus={setStatus} reload={reload} />}
      {tab === "import" && <ImportTab onDone={() => { reload(); setTab("list"); }} />}
      {tab === "vehicles" && <VehiclesTab />}
      {tab === "workbook" && <WorkbookTab onImported={reload} />}
      {tab === "settings" && data && <SettingsTab s={data.settings} reload={reload} />}
      {tab === "lookup" && <><LookupTab /><CasesPanel /></>}
    </>
  );
}

function ListTab({ data, q, setQ, status, setStatus, reload }: {
  data: MembersData | null; q: string; setQ: (s: string) => void; status: string; setStatus: (s: string) => void; reload: () => void;
}) {
  const [edit, setEdit] = useState<{ id: number | null; form: HouseholdIn } | null>(null);
  if (edit) return <HouseholdForm edit={edit} onDone={() => { setEdit(null); reload(); }} onCancel={() => setEdit(null)} />;
  const open = (h: Household) => setEdit({ id: h.id, form: { name: h.name, address: h.address, account_ref: h.account_ref, status: h.status,
    notes: h.notes, emails: h.emails.length ? h.emails.map((e) => ({ email: e.email, name: e.name })) : [{ email: "", name: "" }] } });
  return (
    <>
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Status">
          {[["all", "All"], ["active", "Active"], ["inactive", "Inactive"]].map(([k, l]) => (
            <button key={k} className={status === k ? "on" : ""} aria-pressed={status === k} onClick={() => setStatus(k)}>{l}</button>))}
        </div>
        <input type="search" placeholder="Search name, address, email" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search households" />
        <button className="btn primary" onClick={() => setEdit({ id: null, form: blank })}>Add household</button>
      </div>
      {!data ? <p className="muted">Loading…</p> : data.households.length === 0
        ? <Empty title={q ? "No households match" : "No members yet"}>{!q && "Import the membership list (CSV) to get started."}</Empty>
        : <>
          <div className="tbl"><table className="stack-sm">
            <thead><tr><th>Household</th><th>Address</th><th>Emails</th><th>Status</th><th /></tr></thead>
            <tbody>{data.households.map((h) => (
              <tr key={h.id} className={h.status === "active" ? "" : "disabled"}>
                <td data-label="Household"><div className="strong">{h.row_color && /^[0-9A-F]{6}$/.test(h.row_color) && <span className="swatch" style={{ background: `#${h.row_color}` }} title={`Row color #${h.row_color}`} />} {h.name}</div>
                  {h.people && h.people.length > 0 && <div className="small">{h.people.join(", ")}</div>}
                  <div className="small muted">{[h.account_ref && `#${h.account_ref}`, h.member_type].filter(Boolean).join(" · ")}</div></td>
                <td data-label="Address" className="small">{h.address ?? "—"}</td>
                <td data-label="Emails">{h.emails.length ? h.emails.map((e) => (
                  <div key={e.email} className="small break">{e.email}{e.name && <span className="muted"> · {e.name}</span>}
                    {e.last_signed_in && <span className="muted"> · signed in {ago(e.last_signed_in)}</span>}</div>)) : <span className="small muted">none</span>}
                  {h.phones?.map((p) => <div key={p} className="small muted">{phone(p)}</div>)}</td>
                <td data-label="Status">{h.status === "active" ? <span className="chip st-succeeded">active</span> : <span className="chip muted">inactive</span>}</td>
                <td className="row-actions"><button className="btn ghost small" onClick={() => open(h)}>Edit</button></td>
              </tr>))}</tbody>
          </table></div>
          {data.total > data.households.length && <p className="small muted">Showing {data.households.length} of {fmt(data.total)}. Search to narrow it down.</p>}
        </>}
    </>
  );
}

function HouseholdForm({ edit, onDone, onCancel }: { edit: { id: number | null; form: HouseholdIn }; onDone: () => void; onCancel: () => void }) {
  const [f, setF] = useState<HouseholdIn>(edit.form);
  const { busy, error, run } = useAction();
  const setEmail = (i: number, k: "email" | "name", v: string) => setF({ ...f, emails: f.emails.map((e, j) => (j === i ? { ...e, [k]: v } : e)) });
  const save = () => run(async () => {
    const body = { ...f, emails: f.emails.filter((e) => e.email.trim()).map((e) => ({ email: e.email.trim().toLowerCase(), name: e.name || null })) };
    if (edit.id) await api.saveHousehold(edit.id, body); else await api.addHousehold(body);
    onDone();
  });
  const remove = () => run(async () => { if (edit.id) await api.removeHousehold(edit.id); onDone(); });
  return (
    <section className="panel">
      <h3 className="h3">{edit.id ? f.name || "Household" : "Add a household"}</h3>
      <ErrorBox error={error} />
      <div className="grid2">
        <Field label="Household name"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="e.g. Smith" /></Field>
        <Field label="Address"><input value={f.address ?? ""} onChange={(e) => setF({ ...f, address: e.target.value })} autoComplete="off" /></Field>
        <Field label="Account / member id" hint="Used to match this household on the next import"><input value={f.account_ref ?? ""} onChange={(e) => setF({ ...f, account_ref: e.target.value })} /></Field>
        <Field label="Status" hint="Inactive households can't sign in as members">
          <select value={f.status} onChange={(e) => setF({ ...f, status: e.target.value as HouseholdIn["status"] })}><option value="active">Active</option><option value="inactive">Inactive</option></select></Field>
      </div>
      <fieldset className="terms"><legend className="lbl">Email addresses (up to two)</legend>
        {f.emails.map((e, i) => (
          <div key={i} className="grid2">
            <Field label={`Email ${i + 1}`}><input type="email" inputMode="email" autoCapitalize="none" value={e.email} onChange={(ev) => setEmail(i, "email", ev.target.value)} /></Field>
            <Field label="Name (optional)"><input value={e.name ?? ""} onChange={(ev) => setEmail(i, "name", ev.target.value)} /></Field>
          </div>))}
        {f.emails.length < 2 && <button className="btn ghost small" onClick={() => setF({ ...f, emails: [...f.emails, { email: "", name: "" }] })}>Add second email</button>}
      </fieldset>
      <Field label="Notes (admins only)"><textarea rows={2} value={f.notes ?? ""} onChange={(e) => setF({ ...f, notes: e.target.value })} /></Field>
      <div className="actions">
        <button className="btn primary" disabled={busy || !f.name.trim()} onClick={save}>{busy ? "Saving…" : "Save"}</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
        {edit.id && <><span className="spacer" /><button className="btn ghost danger" disabled={busy} onClick={remove}>Delete</button></>}
      </div>
    </section>
  );
}

function ImportTab({ onDone }: { onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [p, setP] = useState<ImportPreview | null>(null);
  const [replace, setReplace] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  const pick = (f: File | null) => { setFile(f); setP(null); setResult(null); if (f) run(async () => setP(await api.previewImport(f))); };
  const remap = (key: string, col: string) => file && p && run(async () => setP(await api.previewImport(file, { ...p.mapping, [key]: col || null })));
  const go = () => file && p && run(async () => {
    const r = await api.runImport(file, p.mapping, replace);
    setResult(`Imported: ${r.created} new and ${r.updated} updated households, ${r.emails} email addresses${r.deactivated ? `, ${r.deactivated} marked inactive` : ""}${r.problems ? `. ${r.problems} rows had problems and were skipped or trimmed.` : "."}`);
    setTimeout(onDone, 2500);
  });
  return (
    <section className="panel">
      <p className="small muted">Upload the membership list as a CSV (export it from the spreadsheet). One row per household, with one or two email columns. The file is read in memory and not stored. Re-importing updates households, matched by account id or email.</p>
      <ErrorBox error={error} />
      <Field label="CSV file"><input type="file" accept=".csv,text/csv" onChange={(e) => pick(e.target.files?.[0] ?? null)} /></Field>
      {busy && !p && <p className="muted">Reading…</p>}
      {p && <>
        <h3 className="h3 mt">Columns</h3>
        <p className="small muted">We guessed which column is which. Fix anything that's wrong.</p>
        <div className="grid2">
          {IMPORT_FIELDS.map((fl) => (
            <Field key={fl.key} label={fl.label}>
              <select value={p.mapping[fl.key] ?? ""} onChange={(e) => remap(fl.key, e.target.value)}>
                <option value="">(none)</option>{p.headers.map((h) => <option key={h} value={h}>{h}</option>)}
              </select></Field>))}
        </div>
        <h3 className="h3 mt">Preview</h3>
        <p className="small">{fmt(p.rows)} rows → <b>{fmt(p.households)}</b> households, {fmt(p.emails ?? 0)} emails{p.inactive ? `, ${p.inactive} inactive` : ""}{p.no_email ? `, ${p.no_email} without an email (they can't sign in)` : ""}.</p>
        {p.sample.length > 0 && <div className="tbl"><table className="stack-sm">
          <thead><tr><th>Line</th><th>Household</th><th>Address</th><th>Emails</th><th>Status</th></tr></thead>
          <tbody>{p.sample.map((h) => (
            <tr key={h.line}><td data-label="Line">{h.line}</td><td data-label="Household">{h.name}</td><td data-label="Address" className="small">{h.address ?? "—"}</td>
              <td data-label="Emails" className="small break">{h.emails.map((e) => e.email).join(", ") || "—"}</td><td data-label="Status">{h.status}</td></tr>))}</tbody>
        </table></div>}
        {p.problems.length > 0 && <div className="panel warn slim"><b>{p.problems.length + (p.more_problems ?? 0)} problems</b> (these rows are skipped or trimmed):
          <ul className="plain small">{p.problems.map((x) => <li key={x}>{x}</li>)}</ul>{p.more_problems ? <p className="small">…and {p.more_problems} more.</p> : null}</div>}
        <label className="check"><input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> This is the complete list: mark households that aren't in it as inactive</label>
        <div className="actions"><button className="btn primary" disabled={busy || !p.households} onClick={go}>{busy ? "Importing…" : `Import ${fmt(p.households)} households`}</button></div>
      </>}
      {result && <p className="ok-text" role="status">{result}</p>}
    </section>
  );
}

const phone = (p: string) => (p.length === 10 ? `(${p.slice(0, 3)}) ${p.slice(3, 6)}-${p.slice(6)}` : p);
const car = (v: { color: string | null; year: number | null; make: string | null; model: string | null }) =>
  [v.color, v.year, v.make, v.model].filter(Boolean).join(" ") || "—";

/** Registered vehicles: the list, and the CSV import from CAM. */
function VehiclesTab() {
  const [q, setQ] = useState("");
  const [unmatched, setUnmatched] = useState(false);
  const [importing, setImporting] = useState(false);
  const { data, error, reload } = useLoad(() => api.vehicles(q, unmatched), [q, unmatched]);
  if (importing) return <VehicleImport onDone={() => { setImporting(false); reload(); }} onCancel={() => setImporting(false)} />;
  return (
    <>
      <p className="small muted">Plates registered to member households. The board, admins and security can check a plate through the audited lookup at their role's level (see Member lookup): at the yes/no level, security learns only whether it's a member's car and what it looks like, never whose it is.</p>
      <ErrorBox error={error} />
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Filter">
          <button className={!unmatched ? "on" : ""} aria-pressed={!unmatched} onClick={() => setUnmatched(false)}>All{data ? ` (${fmt(data.counts.vehicles)})` : ""}</button>
          <button className={unmatched ? "on" : ""} aria-pressed={unmatched} onClick={() => setUnmatched(true)}>No household{data ? ` (${fmt(data.counts.unmatched)})` : ""}</button>
        </div>
        <input type="search" placeholder="Search plate, car, household, address" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search vehicles" />
        <button className="btn primary" onClick={() => setImporting(true)}>Import vehicles</button>
      </div>
      {!data ? <p className="muted">Loading…</p> : data.vehicles.length === 0
        ? <Empty title={q || unmatched ? "No vehicles match" : "No vehicles yet"}>{!q && !unmatched && "Import the vehicle list (CSV) from CAM."}</Empty>
        : <>
          <div className="tbl"><table className="stack-sm">
            <thead><tr><th>Plate</th><th>Vehicle</th><th>Household</th><th>Tag</th><th /></tr></thead>
            <tbody>{data.vehicles.map((v) => (
              <tr key={v.id} className={v.status === "inactive" ? "disabled" : ""}>
                <td data-label="Plate"><span className="mono strong">{v.plate}</span>{v.state && <span className="small muted"> {v.state}</span>}</td>
                <td data-label="Vehicle" className="small">{car(v)}</td>
                <td data-label="Household" className="small">{v.household
                  ? <>{v.household}{v.status === "inactive" && <span className="chip muted">inactive</span>}<div className="muted">{v.address}</div></>
                  : <span className="chip warn">no match</span>}{!v.household && v.address && <div className="muted">{v.address}</div>}</td>
                <td data-label="Tag" className="small">{v.tag ?? "—"}</td>
                <td className="row-actions"><button className="btn ghost small danger" onClick={async () => { await api.removeVehicle(v.id); reload(); }}>Remove</button></td>
              </tr>))}</tbody>
          </table></div>
          {data.total > data.vehicles.length && <p className="small muted">Showing {data.vehicles.length} of {fmt(data.total)}. Search to narrow it down.</p>}
        </>}
    </>
  );
}

function VehicleImport({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [p, setP] = useState<VehiclePreview | null>(null);
  const [replace, setReplace] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  const pick = (f: File | null) => { setFile(f); setP(null); setResult(null); if (f) run(async () => setP(await api.previewVehicles(f))); };
  const remap = (key: string, col: string) => file && p && run(async () => setP(await api.previewVehicles(file, { ...p.mapping, [key]: col || null })));
  const go = () => file && p && run(async () => {
    const r = await api.importVehicles(file, p.mapping, replace);
    setResult(`Imported: ${r.created} new and ${r.updated} updated vehicles${r.removed ? `, ${r.removed} removed` : ""}${r.unmatched ? `. ${r.unmatched} didn't match a household; they're listed under "No household" and link automatically when a member import matches them.` : "."}`);
    setTimeout(onDone, 3500);
  });
  return (
    <section className="panel">
      <p className="small muted">Upload the vehicle list as a CSV: one row per vehicle with the plate, and an account number or address to link it to a household. The file is read in memory and not stored. Re-importing updates vehicles by plate and state.</p>
      <ErrorBox error={error} />
      <Field label="CSV file"><input type="file" accept=".csv,text/csv" onChange={(e) => pick(e.target.files?.[0] ?? null)} /></Field>
      {busy && !p && <p className="muted">Reading…</p>}
      {p && <>
        <h3 className="h3 mt">Columns</h3>
        <div className="grid2">
          {VEHICLE_FIELDS.map((fl) => (
            <Field key={fl.key} label={fl.label}>
              <select value={p.mapping[fl.key] ?? ""} onChange={(e) => remap(fl.key, e.target.value)}>
                <option value="">(none)</option>{p.headers.map((h) => <option key={h} value={h}>{h}</option>)}
              </select></Field>))}
        </div>
        <h3 className="h3 mt">Preview</h3>
        <p className="small">{fmt(p.rows)} rows → <b>{fmt(p.vehicles)}</b> vehicles, {fmt(p.matched ?? 0)} linked to a household{p.unmatched ? `, ${p.unmatched} not matched` : ""}.</p>
        {p.sample.length > 0 && <div className="tbl"><table className="stack-sm">
          <thead><tr><th>Line</th><th>Plate</th><th>Vehicle</th><th>Household</th></tr></thead>
          <tbody>{p.sample.map((v) => (
            <tr key={v.line}><td data-label="Line">{v.line}</td><td data-label="Plate" className="mono">{v.plate} {v.state}</td>
              <td data-label="Vehicle" className="small">{car(v)}</td><td data-label="Household" className="small">{v.household ?? <span className="chip warn">no match</span>}</td></tr>))}</tbody>
        </table></div>}
        {p.problems.length > 0 && <div className="panel warn slim"><b>{p.problems.length + (p.more_problems ?? 0)} notes</b>:
          <ul className="plain small">{p.problems.map((x) => <li key={x}>{x}</li>)}</ul>{p.more_problems ? <p className="small">…and {p.more_problems} more.</p> : null}</div>}
        <label className="check"><input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> This is the complete list: remove vehicles that aren't in it</label>
        <div className="actions">
          <button className="btn primary" disabled={busy || !p.vehicles} onClick={go}>{busy ? "Importing…" : `Import ${fmt(p.vehicles)} vehicles`}</button>
          <button className="btn ghost" onClick={onCancel}>Cancel</button></div>
      </>}
      {!p && <div className="actions"><button className="btn ghost" onClick={onCancel}>Cancel</button></div>}
      {result && <p className="ok-text" role="status">{result}</p>}
    </section>
  );
}

/** Who sees incident case files, and what the case builder may do. */
function CasesPanel() {
  const { data, error } = useLoad(api.casesSettings);
  const [s, setS] = useState<CasesSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const { busy, error: saveError, run } = useAction();
  const cur = s ?? data;
  if (error) return <ErrorBox error={error} />;
  if (!cur) return null;
  const toggle = (r: CasesSettings["full_roles"][number]) =>
    setS({ ...cur, full_roles: cur.full_roles.includes(r) ? cur.full_roles.filter((x) => x !== r) : [...cur.full_roles, r] });
  return (
    <section className="panel">
      <h3 className="h3">Incident case files</h3>
      <p className="small muted">When security or staff file an incident, Cypress builds a case file: it reads plates in photos, matches plates, names and addresses to households, finds earlier incidents and the rules that apply, and drafts a notice letter for the board to approve. Cypress never sends letters. It never identifies people from faces.</p>
      <ErrorBox error={saveError} />
      <p className="lbl">Who sees the full case file (household, contact details, vehicles)</p>
      <div className="row-wrap">{(["board", "admin", "security"] as const).map((r) => (
        <label key={r} className="check"><input type="checkbox" checked={cur.full_roles.includes(r) || r === "admin"} disabled={r === "admin"} onChange={() => toggle(r)} /> {r}</label>))}</div>
      <p className="small muted">Everyone else who can see the ticket only learns whether a member household matched.</p>
      <label className="check"><input type="checkbox" checked={cur.vision} onChange={(e) => setS({ ...cur, vision: e.target.checked })} /> Read plates and vehicles in attached photos (about 1¢ a photo)</label>
      <label className="check"><input type="checkbox" checked={cur.draft_letters} onChange={(e) => setS({ ...cur, draft_letters: e.target.checked })} /> Draft notice letters when a member household matches and a rule applies</label>
      <label className="lbl" htmlFor="policy-reasoning">How the rule finder reasons</label>
      <select id="policy-reasoning" value={cur.policy_reasoning ?? "linear"} onChange={(e) => setS({ ...cur, policy_reasoning: e.target.value as CasesSettings["policy_reasoning"] })}>
        <option value="linear">One pass (faster)</option>
        <option value="tot">Tree of thought: weighs several readings, keeps a runner-up (slower, more model calls)</option>
      </select>
      <p className="small muted">Compare the two in Evals → Case-file sub-agents before switching.</p>
      <div className="row-end"><span className="small muted">{saved ? "Saved" : ""}</span>
        <button className="btn primary" disabled={busy || !s} onClick={() => run(async () => { setS(await api.saveCasesSettings(cur)); setSaved(true); setTimeout(() => setSaved(false), 2500); })}>Save</button></div>
    </section>
  );
}

function SettingsTab({ s, reload }: { s: MemberAuth; reload: () => void }) {
  const [f, setF] = useState(s);
  const { busy, error, run } = useAction();
  const [saved, setSaved] = useState(false);
  return (
    <section className="panel">
      <ErrorBox error={error} />
      <label className="check"><input type="checkbox" checked={f.require_for_reports} onChange={(e) => setF({ ...f, require_for_reports: e.target.checked })} /> Residents must sign in as members to file issue reports in the web chat</label>
      <p className="small muted">When on, Cypress drafts the report and asks them to confirm their email; it's filed when they open the emailed link. Slack users are already identified.</p>
      <div className="grid2">
        <Field label="Suggest signing in after this many questions" hint="0 = never. Shown once, and can be dismissed.">
          <input type="number" min={0} max={50} value={f.nudge_after_turns} onChange={(e) => setF({ ...f, nudge_after_turns: Number(e.target.value) })} /></Field>
        <Field label="Keep drafted reports for (hours)" hint="How long the emailed link can still file the report">
          <input type="number" min={1} max={168} value={f.pending_hours} onChange={(e) => setF({ ...f, pending_hours: Number(e.target.value) })} /></Field>
      </div>
      <div className="actions"><button className="btn primary" disabled={busy} onClick={() => run(async () => { await api.saveMemberAuth(f); setSaved(true); reload(); })}>{busy ? "Saving…" : "Save"}</button>
        {saved && !busy && !error && <span className="ok-text small">Saved</span>}</div>
    </section>
  );
}

// ---------------- member lookup (board, admins, security) ----------------

const LOOKUP_ROLES: ["security" | "board" | "admin", string][] = [
  ["security", "Security"], ["board", "Board"], ["admin", "Admins"]];
const LOOKUP_MODES: [LookupMode, string, string][] = [
  ["details", "Details", "Household name, address, status and names on file"],
  ["confirm", "Yes / no", "Only confirms what they ask: an address, a name, or both"],
  ["off", "Off", "No lookups"],
];
const RESULT: Record<string, string> = {
  active_member: "Yes, active", not_found: "No match", name_mismatch: "Name not on file", matches: "Details shown", rate_limited: "Limit reached",
};

function LookupTab() {
  const { data, error, reload } = useLoad(api.memberLookup);
  const [saved, setSaved] = useState<LookupData | null>(null);
  const d = saved ?? data;
  if (error) return <ErrorBox error={error} />;
  if (!d) return <p className="muted">Loading…</p>;
  return (
    <>
      <LookupSettingsPanel key={JSON.stringify(d.settings)} s0={d.settings} onSaved={setSaved} />
      <section className="panel">
        <div className="row-between"><h3 className="h3">Lookup log</h3><button className="btn ghost small" onClick={() => { setSaved(null); reload(); }}>Refresh</button></div>
        <p className="small muted">Every lookup, by whom and what they asked. Kept so the board can check the list is used appropriately.</p>
        {d.log.length === 0 ? <Empty title="No lookups yet." /> : (
          <div className="tbl"><table className="stack-sm">
            <thead><tr><th>When</th><th>Who</th><th>Asked about</th><th>Result</th><th>Where</th></tr></thead>
            <tbody>{d.log.map((r) => (
              <tr key={r.id}>
                <td data-label="When">{ago(r.at)}</td>
                <td data-label="Who"><span className="break">{r.actor}</span> <span className="chip muted">{r.role ?? "—"}</span></td>
                <td data-label="Asked about">{[r.address_q, r.name_q, r.plate_q && `plate ${r.plate_q}`].filter(Boolean).join(" · ")}</td>
                <td data-label="Result">{r.result === "rate_limited" ? <span className="chip st-failed">{RESULT[r.result]}</span> : RESULT[r.result] ?? r.result}
                  {r.mode === "details" && r.matches > 1 && <span className="small muted"> ({r.matches})</span>}</td>
                <td data-label="Where">{r.channel === "slack" ? "Slack" : r.channel === "web" ? "Web" : r.channel}</td>
              </tr>))}</tbody>
          </table></div>)}
      </section>
    </>
  );
}

function LookupSettingsPanel({ s0, onSaved }: { s0: LookupSettings; onSaved: (d: LookupData) => void }) {
  const [s, setS] = useState(s0);
  const { busy, error, run } = useAction();
  return (
    <section className="panel">
      <ErrorBox error={error} />
      <h3 className="h3">Who can look up members</h3>
      <p className="small muted">Only the board, admins and security can ask Cypress things like "is 123 Example Lane a member household?" in chat or Slack.
        Members and the public never can. Email addresses and phone numbers are never shown. In a Slack channel, Cypress answers at the level everyone in the channel shares.</p>
      <div className="tbl"><table className="stack-sm lookup-grid">
        <thead><tr><th>Role</th>{LOOKUP_MODES.map(([m, l, hint]) => <th key={m} title={hint}>{l}</th>)}</tr></thead>
        <tbody>{LOOKUP_ROLES.map(([role, label]) => (
          <tr key={role}>
            <td data-label="Role" className="strong">{label}</td>
            {LOOKUP_MODES.map(([m, l]) => (
              <td key={m} data-label={l}>
                <label className="radio-cell"><input type="radio" name={`lk-${role}`} checked={s.access[role] === m}
                  onChange={() => setS({ ...s, access: { ...s.access, [role]: m } })} aria-label={`${label}: ${l}`} /></label>
              </td>))}
          </tr>))}</tbody>
      </table></div>
      <ul className="small muted tight">{LOOKUP_MODES.map(([m, l, hint]) => <li key={m}><b>{l}:</b> {hint}.</li>)}
        <li>With Yes / no, an address never reveals who lives there, a name never reveals an address, and a lapsed household just shows "no active member household".</li></ul>
      <div className="grid2">
        <Field label="Lookups per person per hour"><input type="number" min={1} max={500} value={s.per_hour} onChange={(e) => setS({ ...s, per_hour: Number(e.target.value) })} /></Field>
        <Field label="Lookups per person per day" hint="Limits stop anyone walking the street list"><input type="number" min={1} max={5000} value={s.per_day} onChange={(e) => setS({ ...s, per_day: Number(e.target.value) })} /></Field>
      </div>
      <div className="actions"><button className="btn primary" disabled={busy} onClick={() => run(async () => onSaved(await api.saveMemberLookup(s)))}>{busy ? "Saving…" : "Save"}</button></div>
    </section>
  );
}
