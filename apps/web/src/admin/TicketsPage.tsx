import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, photoUrl, type CaseLetter, type TicketDetail, type TicketPriority, type TicketStatus } from "./api";
import { ago, Empty, ErrorBox, useAction, useLoad } from "./ui";

const FILTERS = [{ v: "open", label: "Open" }, { v: "new", label: "New" }, { v: "resolved", label: "Resolved" }, { v: "all", label: "All" }];
const STATUS: Record<TicketStatus, string> = { new: "New", acknowledged: "Acknowledged", in_progress: "In progress", resolved: "Resolved", closed: "Closed" };
const PRIORITY_EMOJI: Record<TicketPriority, string> = { urgent: "🚨", high: "🔴", normal: "🟡", low: "⚪" };
const who = (role: string | null) => role ?? "resident (anonymous)";

export function TicketsPage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "open";
  const { data, error } = useLoad(() => api.tickets(status), [status]);
  return (
    <>
      <header className="page-head"><div><h1>Tickets</h1>
        <p className="sub">Issues reported through Cypress on the web and in Slack. New tickets are also posted to Slack.</p></div></header>
      <div className="toolbar"><div className="tabs">{FILTERS.map((f) => (
        <button key={f.v} className={status === f.v ? "on" : ""} onClick={() => setParams(f.v === "open" ? {} : { status: f.v })}>{f.label}</button>
      ))}</div></div>
      <ErrorBox error={error} />
      {data && !data.length && <Empty title="No tickets here">Residents can report problems by telling Cypress what's wrong and where.</Empty>}
      {data && data.length > 0 && (
        <div className="tbl"><table className="stack-sm">
          <thead><tr><th>#</th><th>Issue</th><th>Status</th><th>Category</th><th>Assignee</th><th>Reported</th></tr></thead>
          <tbody>{data.map((t) => (
            <tr key={t.id}>
              <td className="mono">{t.id}</td>
              <td><span title={t.priority}>{PRIORITY_EMOJI[t.priority]}</span> <Link to={`/admin/tickets/${t.id}`}>{t.title}</Link>
                {t.location && <div className="small muted">📍 {t.location}</div>}</td>
              <td><span className={`chip tk-${t.status}`}>{STATUS[t.status]}</span></td>
              <td className="small">{t.category}</td>
              <td className="small">{t.assignee ?? <span className="muted">—</span>}</td>
              <td className="small" title={t.created_at}>{ago(t.created_at)}<div className="muted">{t.channel} · {who(t.reporter_role)}</div></td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
    </>
  );
}

function describe(e: TicketDetail["events"][number]): string {
  const d = e.data as { from?: string; to?: string; channel?: string };
  switch (e.kind) {
    case "created": return `reported via ${d.channel ?? "?"}`;
    case "status": return `${STATUS[d.from as TicketStatus] ?? d.from} → ${STATUS[d.to as TicketStatus] ?? d.to}`;
    case "assigned": return d.to ? `assigned to ${d.to}` : "unassigned";
    case "priority": return `priority ${d.from} → ${d.to}`;
    default: return "commented";
  }
}

export function TicketDetailPage() {
  const id = Number(useParams().id);
  const { data, error, reload } = useLoad(() => api.ticket(id), [id]);
  const [t, setT] = useState<TicketDetail | null>(null);
  const [note, setNote] = useState("");
  const action = useAction();
  const cur = t ?? data;
  if (error) return <ErrorBox error={error} />;
  if (!cur) return <p className="muted">Loading…</p>;
  const patch = (p: Parameters<typeof api.patchTicket>[1]) => action.run(async () => { setT(await api.patchTicket(id, p)); reload(); });
  return (
    <>
      <nav className="crumbs"><Link to="/admin/tickets">Tickets</Link> / <span>#{cur.id}</span></nav>
      <header className="page-head">
        <div>
          <h1>{PRIORITY_EMOJI[cur.priority]} {cur.title}</h1>
          <p className="stats small">#{cur.id} · {cur.category} · reported {ago(cur.created_at)} via {cur.channel} by {who(cur.reporter_role)}
            {cur.location && <> · 📍 {cur.location}</>}</p>
        </div>
        <span className={`chip tk-${cur.status}`}>{STATUS[cur.status]}</span>
      </header>
      <ErrorBox error={action.error} />
      <div className="doc-layout">
        <div>
          <div className="panel"><p className="pre">{cur.description}</p>
            {cur.reporter_contact && <p className="small">Reporter contact: <b>{cur.reporter_contact}</b></p>}
            {cur.photos && cur.photos.length > 0 && <div className="ticket-photos">{cur.photos.map((id) => <Photo key={id} id={id} />)}</div>}
            {cur.videos && cur.videos.length > 0 && <div className="ticket-media">{cur.videos.map((id) => <Video key={id} id={id} />)}</div>}
            {cur.removed && (cur.removed.photos > 0 || cur.removed.videos > 0) && (
              <p className="small muted">📎 {[cur.removed.photos && `${cur.removed.photos} photo${cur.removed.photos === 1 ? "" : "s"}`,
                cur.removed.videos && `${cur.removed.videos} video${cur.removed.videos === 1 ? "" : "s"}`].filter(Boolean).join(" and ")} removed
                by the retention policy{cur.removed.at ? ` ${ago(cur.removed.at)}` : ""}.</p>)}</div>
          {cur.case && <CasePanel t={cur} reload={reload} onChange={(c) => setT({ ...cur, case: c })} />}
          <h3>History</h3>
          <ol className="plain thread">{cur.events.map((e, i) => (
            <li key={i}><b>{e.actor}</b> <span className="small muted">{describe(e)} · {ago(e.at)}</span>
              {e.body && <div className="pre">{e.body}</div>}</li>
          ))}</ol>
          <form className="panel slim" onSubmit={(e) => { e.preventDefault(); if (note.trim()) action.run(async () => { setT(await api.commentTicket(id, note)); setNote(""); }); }}>
            <textarea rows={3} value={note} maxLength={4000} placeholder="Add a note for the board and staff" onChange={(e) => setNote(e.target.value)} />
            <div className="row-end"><button className="btn" disabled={action.busy || !note.trim()}>Add note</button></div>
          </form>
        </div>
        <aside className="panel slim">
          <h3>Status</h3>
          <div className="stack">{(Object.keys(STATUS) as TicketStatus[]).map((s) => (
            <button key={s} className={`btn small${cur.status === s ? " primary" : ""}`} disabled={action.busy || cur.status === s} onClick={() => patch({ status: s })}>{STATUS[s]}</button>
          ))}</div>
          <h3>Priority</h3>
          <select value={cur.priority} disabled={action.busy} onChange={(e) => patch({ priority: e.target.value as TicketPriority })}>
            {(["urgent", "high", "normal", "low"] as TicketPriority[]).map((p) => <option key={p} value={p}>{PRIORITY_EMOJI[p]} {p}</option>)}
          </select>
          <h3>Assignee</h3>
          <AssigneeField value={cur.assignee ?? ""} busy={action.busy} onSave={(a) => patch({ assignee: a })} />
          <HoldBox t={cur} onChange={(hold) => setT({ ...cur, hold })} />
        </aside>
      </div>
    </>
  );
}

const AGENT_LABEL: Record<string, string> = { plate_reader: "Plate reader (photos)", matcher: "Matcher (plate & member lookup)",
  history: "History", policy: "Rules & bylaws" };
const CASE_STATUS: Record<string, string> = { queued: "Queued", running: "Building…", awaiting_board: "Waiting for the board",
  approved: "Approved", dismissed: "Dismissed", failed: "Couldn't be built" };
const phoneFmt = (p: string) => (p.length === 10 ? `(${p.slice(0, 3)}) ${p.slice(3, 6)}-${p.slice(6)}` : p);

/** The incident case file: built by a supervisor and sub-agents, then decided by the board. */
function CasePanel({ t, reload, onChange }: { t: TicketDetail; reload: () => void; onChange: (c: TicketDetail["case"]) => void }) {
  const c = t.case!;
  const [edit, setEdit] = useState<CaseLetter | null>(null);
  const [note, setNote] = useState("");
  const [confirm, setConfirm] = useState<"approve" | "dismiss" | null>(null);
  const { busy, error, run } = useAction();
  useEffect(() => {
    if (c.status !== "queued" && c.status !== "running") return;
    const h = setInterval(reload, 6000);
    return () => clearInterval(h);
  }, [c.status, reload]);
  const head = <h3>🗂️ Case file <span className={`chip ${c.status === "awaiting_board" ? "warn" : c.status === "approved" ? "st-succeeded" : "muted"}`}>{CASE_STATUS[c.status]}</span></h3>;
  if (!c.full) return (
    <div className="panel slim">{head}
      <p className="small">{c.status === "queued" || c.status === "running" ? "Cypress is building a case file for the board."
        : c.matched === undefined ? "The case file is with the board." : c.matched ? "A member household matched. The board has the details." : "No member household matched."}</p></div>);
  if (c.status === "queued" || c.status === "running") return (
    <div className="panel slim">{head}<p className="small muted">The supervisor and sub-agents are reading photos, checking plates and the rules. This takes about a minute.</p></div>);
  if (c.status === "failed") return <div className="panel warn slim">{head}<p className="small">{c.error}</p></div>;
  const f = c.findings ?? {}, p = c.profile;
  const letter = edit ?? c.letter ?? null;
  const done = (r: { case: TicketDetail["case"] }) => { onChange(r.case); setConfirm(null); setEdit(null); reload(); };
  const decide = (action: "approve" | "dismiss") => run(async () => done(await api.decideCase(t.id, action, note || undefined)));
  const v = c.vote;
  const voteOpen = v?.status === "open";
  return (
    <div className={`panel case${c.status === "awaiting_board" ? " warn" : ""}`}>
      {head}
      <ErrorBox error={error} />
      {p && <><p>{p.summary}</p>
        {p.assessment && <p className="small"><b>Identification:</b> {p.assessment}</p>}
        {p.next_step && <p className="small"><b>Suggested next step:</b> {p.next_step}</p>}</>}
      {p && p.households.length > 0 && <>
        <h4>Matched {p.households.length === 1 ? "household" : "households"}</h4>
        {p.households.map((h, i) => (
          <div key={i} className="case-hh">
            <div><b>{h.name}</b> <span className={`chip ${h.confidence === "high" ? "st-succeeded" : "warn"}`}>{h.confidence} confidence</span>
              {h.status === "inactive" && <span className="chip muted">inactive</span>}</div>
            <div className="small">{h.address}</div>
            <div className="small muted">{h.basis}</div>
            {h.people && h.people.length > 0 && <div className="small">People on file: {h.people.join(", ")}</div>}
            {(h.emails?.length || h.phones?.length) ? <div className="small break">{[...(h.emails ?? []), ...(h.phones ?? []).map(phoneFmt)].join(" · ")}</div> : null}
            {h.vehicles && h.vehicles.length > 0 && <div className="small">Vehicles: {h.vehicles.map((v) => `${v.car} (${v.plate})`).join("; ")}</div>}
          </div>))}</>}
      {f.history && f.history.earlier.length > 0 && <>
        <h4>Earlier incidents</h4>
        <ul className="plain small">{f.history.earlier.map((e) => <li key={e.ticket_id}><Link to={`/admin/tickets/${e.ticket_id}`}>#{e.ticket_id} {e.title}</Link> <span className="muted">· {e.status} · {ago(e.at)}</span></li>)}</ul></>}
      {f.policy && f.policy.rules.length > 0 && <>
        <h4>Rules that may apply</h4>
        <ul className="plain small">{f.policy.rules.map((r, i) => {
          const s = f.policy!.sources.find((x) => x.n === Number(r.source));
          return <li key={i}>“{r.rule}” {s && <a href={s.uri} target="_blank" rel="noreferrer">({s.title})</a>}<div className="muted">{r.why}</div></li>; })}</ul>
        {f.policy.process && <p className="small"><b>Process:</b> {f.policy.process}</p>}</>}
      {letter && <>
        <h4>Draft notice letter {c.status === "approved" && <span className="chip st-succeeded">approved</span>}</h4>
        {edit ? <div className="stack">
            <input value={edit.subject} maxLength={200} onChange={(e) => setEdit({ ...edit, subject: e.target.value })} aria-label="Letter subject" />
            <textarea rows={12} value={edit.body} maxLength={5000} onChange={(e) => setEdit({ ...edit, body: e.target.value })} aria-label="Letter text" /></div>
          : <div className="letter"><b>{letter.subject}</b><p className="pre">{letter.body}</p></div>}
</>}
      <details className="small"><summary>How Cypress built this</summary>
        {c.plan && <p>Supervisor ran: {c.plan.agents.map((a) => AGENT_LABEL[a] ?? a).join(", ")}{c.plan.reason ? ` — ${c.plan.reason}` : ""}</p>}
        {f.plate_reader && <p>Plate reader: {f.plate_reader.photos_read} photo{f.plate_reader.photos_read === 1 ? "" : "s"} read
          {f.plate_reader.plates.length > 0 && <> · plates {f.plate_reader.plates.map((x) => `${x.text}${x.state ? " " + x.state : ""} (${x.confidence})`).join(", ")}</>}
          {f.plate_reader.notes && <> · {f.plate_reader.notes}</>}</p>}
        {f.matcher && <p>Matcher: {f.matcher.summary}{f.matcher.lookups.length > 0 && <> ({f.matcher.lookups.length} lookup{f.matcher.lookups.length === 1 ? "" : "s"}, all logged)</>}</p>}
        {c.trace_url && <p><a href={c.trace_url} target="_blank" rel="noreferrer">Open the trace in Google Cloud →</a></p>}
      </details>
      {v && <div className="vote-box">
        <h4>🗳️ Board vote {v.status !== "open" && <span className={`chip ${v.status === "passed" ? "st-succeeded" : "muted"}`}>{v.status}</span>}</h4>
        <p className="small"><b>{v.yes} approve · {v.no} reject</b> — {v.needed} of {v.seats} needed{voteOpen && <> · closes {new Date(v.closes_at).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</>}</p>
        {v.votes.length > 0 && <p className="small muted">{v.votes.map((x) => `${x.name} ${x.vote === "yes" ? "✅" : "❌"}`).join(" · ")}</p>}
        {voteOpen && (v.can_vote
          ? <div className="row-wrap">
              <button className={`btn small${v.my_vote === "yes" ? " primary" : ""}`} disabled={busy} onClick={() => run(async () => done(await api.castVote(t.id, "yes")))}>Approve{v.my_vote === "yes" ? " ✓" : ""}</button>
              <button className={`btn small${v.my_vote === "no" ? " danger" : ""}`} disabled={busy} onClick={() => run(async () => done(await api.castVote(t.id, "no")))}>Reject{v.my_vote === "no" ? " ✓" : ""}</button>
              <span className="small muted">You can change your vote while it's open. Board members can also vote in Slack.</span></div>
          : <p className="small muted">Only seated board members vote (an active board term in Users &amp; roles).</p>)}
        {voteOpen && c.is_admin && <button className="btn small ghost" disabled={busy} onClick={() => run(async () => done(await api.cancelVote(t.id)))}>Cancel vote (admin)</button>}
        {v.status === "passed" && <p className="small">The letter is ready for CAM. Cypress doesn't send it.</p>}
      </div>}
      {c.status === "awaiting_board" && !voteOpen && <>
        {c.vote_rule && c.vote_rule.seated === 0 && letter && <p className="small warn-text">No board terms are entered yet, so nobody can vote. Add board members and their terms in Users &amp; roles (link their Slack accounts to vote in Slack).</p>}
        <input value={note} maxLength={500} placeholder="Note for the record (optional)" onChange={(e) => setNote(e.target.value)} aria-label="Decision note" />
        {edit
          ? <div className="row-wrap"><button className="btn small primary" disabled={busy} onClick={() => run(async () => done(await api.editLetter(t.id, edit)))}>Save letter</button>
              <button className="btn small ghost" onClick={() => setEdit(null)}>Cancel</button></div>
          : confirm
          ? <div className="row-wrap"><span className="small">{confirm === "approve" ? "Close out the case file?" : "Dismiss this case file?"}</span>
              <button className={`btn small ${confirm === "approve" ? "primary" : "danger"}`} disabled={busy} onClick={() => decide(confirm)}>Yes</button>
              <button className="btn small ghost" onClick={() => setConfirm(null)}>Cancel</button></div>
          : <div className="row-wrap">
              {letter
                ? <button className="btn small primary" disabled={busy} onClick={() => run(async () => done(await api.openVote(t.id)))}>Start board vote ({c.vote_rule?.needed} of {c.vote_rule?.seats})</button>
                : <button className="btn small primary" onClick={() => setConfirm("approve")}>Close out</button>}
              {letter && <button className="btn small" onClick={() => setEdit({ ...letter })}>Edit letter</button>}
              <button className="btn small ghost" onClick={() => setConfirm("dismiss")}>Dismiss</button></div>}</>}
      {c.decision && <p className="small muted">{c.status === "approved" ? (letter ? "Approved" : "Closed out") : "Dismissed"} by {c.decided_by}{c.decided_at ? `, ${ago(c.decided_at)}` : ""}{c.decision.note ? `: ${c.decision.note}` : ""}</p>}
    </div>
  );
}

/** Legal hold: board and admins can hold a ticket so its attachments are kept until released. */
function HoldBox({ t, onChange }: { t: TicketDetail; onChange: (h: TicketDetail["hold"]) => void }) {
  const [reason, setReason] = useState("");
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const { busy, error, run } = useAction();
  const closed = t.status === "resolved" || t.status === "closed";
  return (
    <>
      <h3>Legal hold</h3>
      <ErrorBox error={error} />
      {t.hold ? (
        <div className="hold-box">
          <span className="chip warn">🔒 On hold</span>
          <p className="small">{t.hold.reason}<br /><span className="muted">{t.hold.set_by}, {ago(t.hold.set_at)}</span></p>
          {t.can_hold && (confirm
            ? <div className="row-wrap"><button className="btn small danger" disabled={busy} onClick={() => run(async () => { onChange((await api.setHold(t.id, false)).hold); setConfirm(false); })}>Yes, release</button>
                <button className="btn small ghost" onClick={() => setConfirm(false)}>Cancel</button></div>
            : <button className="btn small" onClick={() => setConfirm(true)}>Release hold</button>)}
        </div>
      ) : t.hold_all ? (
        <p className="small"><span className="chip warn">🔒 Everything on hold</span><br /><span className="muted">An admin has paused all cleanup.</span></p>
      ) : t.can_hold ? (open
        ? <form className="fb-form stack" onSubmit={(e) => { e.preventDefault(); run(async () => { onChange((await api.setHold(t.id, true, reason)).hold); setOpen(false); setReason(""); }); }}>
            <input value={reason} required minLength={3} maxLength={500} placeholder="Why (matter, or who asked)" onChange={(e) => setReason(e.target.value)} autoFocus />
            <div className="row-wrap"><button className="btn small primary" disabled={busy || reason.trim().length < 3}>Place hold</button>
              <button type="button" className="btn small ghost" onClick={() => setOpen(false)}>Cancel</button></div>
          </form>
        : <button className="btn small" onClick={() => setOpen(true)}>Place on hold</button>
      ) : <p className="small muted">Not on hold</p>}
      {!t.hold && !t.hold_all && (t.keep_days ?? 0) > 0 && (
        <p className="small muted">Photos and videos are removed {t.keep_days} days after the ticket {closed ? "was closed" : "closes"}.</p>)}
    </>
  );
}

function AssigneeField({ value, busy, onSave }: { value: string; busy: boolean; onSave: (v: string) => void }) {
  const [v, setV] = useState(value);
  const { data } = useLoad(api.assignees);
  return (
    <form className="fb-form" onSubmit={(e) => { e.preventDefault(); onSave(v.trim()); }}>
      <input value={v} maxLength={200} placeholder="Unassigned" list="assignees" onChange={(e) => setV(e.target.value)} />
      <datalist id="assignees">
        {data?.people.map((n) => <option key={`p-${n}`} value={n} />)}
        {data?.groups.map((n) => <option key={`g-${n}`} value={n} />)}
      </datalist>
      <button className="btn small" disabled={busy || v.trim() === value}>Save</button>
    </form>
  );
}

function Video({ id }: { id: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => { api.videoLink(id).then((r) => setUrl(r.url)).catch(() => setUrl("")); }, [id]);
  if (url === null) return <span className="small muted">Loading video…</span>;
  if (!url) return <span className="small muted">Video unavailable</span>;
  return <video src={url} controls playsInline preload="metadata" />;
}

function Photo({ id }: { id: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let u: string | null = null;
    photoUrl(id).then((x) => { u = x; setUrl(x); }).catch(() => setUrl(""));
    return () => { if (u) URL.revokeObjectURL(u); };
  }, [id]);
  if (url === null) return <span className="small muted">Loading photo…</span>;
  if (!url) return <span className="small muted">Photo unavailable</span>;
  return <a href={url} target="_blank" rel="noreferrer"><img src={url} alt="Attached photo" /></a>;
}
