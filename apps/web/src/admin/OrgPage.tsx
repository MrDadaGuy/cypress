import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type RetentionSettings, type RetentionView, type VoteSettings } from "./api";
import { ago, ErrorBox, Field, useAction, useLoad } from "./ui";

export default function OrgPage() {
  const { data, error } = useLoad(api.org);
  const action = useAction();
  const [members, setMembers] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!data) return;
    setMembers(data.board_members.join("\n")); setName(data.board_contact.name); setEmail(data.board_contact.email);
  }, [data]);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p className="muted">Loading…</p>;
  const save = () => action.run(async () => {
    await api.saveOrg({ board_members: members.split("\n").map((m) => m.trim()).filter(Boolean), board_contact: { name, email } });
    setSaved(true); setTimeout(() => setSaved(false), 2500);
  });
  return (
    <>
      <header className="page-head"><div><h1>Board &amp; contacts</h1>
        <p className="sub">When someone asks about a board member or how to reach the board, Cypress reminds them the board are volunteers and points them to this contact. Changes apply within a few minutes.</p></div></header>
      <ErrorBox error={action.error} />
      <form className="panel" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <div className="grid2">
          {data.board_members_source === "terms"
            ? <Field label="Board members" hint="From board terms in Users & roles. Update terms there.">
                <ul className="plain roster">{data.board_members.map((m) => <li key={m}>{m}</li>)}</ul>
                <a className="small" href="/admin/people">Edit in Users &amp; roles →</a></Field>
            : <Field label="Board members" hint="One name per line. Once board terms are entered in Users & roles, the roster comes from there instead.">
                <textarea rows={9} value={members} onChange={(e) => setMembers(e.target.value)} /></Field>}
          <div>
            <Field label="Reach the board through"><input value={name} required onChange={(e) => setName(e.target.value)} /></Field>
            <Field label="Contact email"><input type="email" value={email} required onChange={(e) => setEmail(e.target.value)} /></Field>
          </div>
        </div>
        <div className="row-end"><span className="small muted">{saved ? "Saved" : ""}</span>
          <button className="btn primary" disabled={action.busy}>Save</button></div>
      </form>
      <Suggestions initial={data.chat_suggestions ?? []} />
      <Attachments initial={data.uploads ?? { videos: true, max_video_mb: 100 }} />
      <Votes />
      <Retention />
    </>
  );
}

/** Suggested questions on the resident chat's welcome screen. */
function Suggestions({ initial }: { initial: string[] }) {
  const [text, setText] = useState(initial.join("\n"));
  const [saved, setSaved] = useState(false);
  const { busy, error, run } = useAction();
  const items = text.split("\n").map((s) => s.trim()).filter(Boolean);
  return (
    <form className="panel" onSubmit={(e) => { e.preventDefault(); run(async () => { await api.saveSuggestions(items); setSaved(true); setTimeout(() => setSaved(false), 2500); }); }}>
      <h2>Suggested questions</h2>
      <p className="sub">The buttons residents see when they open the chat. One per line, up to 8. Keep them general: don't feature anyone's home address.</p>
      <ErrorBox error={error} />
      <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} aria-label="Suggested questions" />
      <div className="row-end"><span className="small muted">{saved ? "Saved; the chat shows them within a few minutes" : `${items.length} of 8`}</span>
        <button className="btn primary" disabled={busy || items.length > 8}>Save</button></div>
    </form>
  );
}

/** What residents can attach in the chat. */
function Attachments({ initial }: { initial: { videos: boolean; max_video_mb: number } }) {
  const [f, setF] = useState(initial);
  const [saved, setSaved] = useState(false);
  const { busy, error, run } = useAction();
  return (
    <form className="panel" onSubmit={(e) => { e.preventDefault(); run(async () => { await api.saveUploads(f); setSaved(true); setTimeout(() => setSaved(false), 2500); }); }}>
      <h2>Chat attachments</h2>
      <p className="sub">Photos are always accepted (up to 4 per message). Videos upload straight to private storage and play in the ticket for staff and the board. Storage is free up to 5 GB, then about 2¢ per GB a month.</p>
      <ErrorBox error={error} />
      <label className="check"><input type="checkbox" checked={f.videos} onChange={(e) => setF({ ...f, videos: e.target.checked })} /> Accept videos</label>
      <Field label="Largest video (MB)" hint="About 1 minute of phone video is 50–150 MB"><input type="number" min={5} max={500} value={f.max_video_mb} onChange={(e) => setF({ ...f, max_video_mb: Number(e.target.value) })} /></Field>
      <div className="row-end"><span className="small muted">{saved ? "Saved" : ""}</span><button className="btn primary" disabled={busy}>Save</button></div>
    </form>
  );
}

/** How the board votes on notice letters before they go to CAM. */
function Votes() {
  const { data, error } = useLoad(api.voteSettings);
  const [s, setS] = useState<VoteSettings | null>(null);
  const [ready, setReady] = useState(data?.ready ?? null);
  const [saved, setSaved] = useState(false);
  const action = useAction();
  useEffect(() => { if (data) { setS(data.settings); setReady(data.ready); } }, [data]);
  if (error) return <ErrorBox error={error} />;
  if (!s || !ready) return null;
  const seats = ready.seated || s.seats;
  const needed = s.rule === "majority" ? Math.floor(seats / 2) + 1 : Math.min(s.count, seats);
  const save = () => action.run(async () => { const r = await api.saveVoteSettings(s); setS(r.settings); setReady(r.ready); setSaved(true); setTimeout(() => setSaved(false), 2500); });
  return (
    <form className="panel" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <h2>Board votes on notice letters</h2>
      <p className="sub">When an incident case file includes a draft notice letter, Cypress opens a vote in the board channel and on the ticket. The letter is marked ready for CAM only if the vote passes. Only seated board members vote: people with an active board term in Users &amp; roles (link their Slack account to vote from Slack).</p>
      <ErrorBox error={action.error} />
      <p className="small"><b>Passes with {needed} of {seats}</b>{ready.seated ? ` seated board members (${ready.on_slack} linked to Slack)` : " seats"}.
        {!ready.seated && <span className="warn-text"> No board terms are entered yet, so nobody can vote until they are.</span>}</p>
      <div className="grid3">
        <Field label="Rule">
          <select value={s.rule} onChange={(e) => setS({ ...s, rule: e.target.value as VoteSettings["rule"] })}>
            <option value="majority">Majority of the board</option><option value="count">A set number of votes</option></select></Field>
        {s.rule === "count"
          ? <Field label="Approvals needed"><input type="number" min={1} max={25} value={s.count} onChange={(e) => setS({ ...s, count: Number(e.target.value) })} /></Field>
          : <Field label="Board seats" hint={ready.seated ? "Board terms are used once entered" : "Used until board terms are entered"}>
              <input type="number" min={1} max={25} value={s.seats} disabled={ready.seated > 0} onChange={(e) => setS({ ...s, seats: Number(e.target.value) })} /></Field>}
        <Field label="Vote stays open (days)" hint="Then it closes as not passed"><input type="number" min={1} max={30} value={s.days} onChange={(e) => setS({ ...s, days: Number(e.target.value) })} /></Field>
      </div>
      <label className="check"><input type="checkbox" checked={s.auto_start} onChange={(e) => setS({ ...s, auto_start: e.target.checked })} /> Start the vote as soon as the case file is ready (otherwise a board member starts it, after editing the letter if needed)</label>
      <div className="row-end"><span className="small muted">{saved ? "Saved" : ""}</span><button className="btn primary" disabled={action.busy}>Save</button></div>
    </form>
  );
}

const mb = (b: number) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`);
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

function lastRunText(r: RetentionView["last_run"]): string {
  if (!r) return "No cleanup has run yet";
  const s = r.summary;
  if (s.hold_all) return `Last run ${ago(r.at)}: skipped, everything was on hold`;
  const bits = [s.photos && plural(s.photos, "photo"), s.videos && plural(s.videos, "video"),
    s.unattached && plural(s.unattached, "unattached upload"), s.lookups && `${s.lookups} log entr${s.lookups === 1 ? "y" : "ies"}`].filter(Boolean);
  return `Last run ${ago(r.at)}: ${bits.length ? `removed ${bits.join(", ")}` : "nothing was due"}${s.failed ? ` · ${s.failed} couldn't be removed` : ""}`;
}

/** How long attachments and the lookup log are kept, and the org-wide legal hold. */
function Retention() {
  const { data, error } = useLoad(api.retention);
  const [view, setView] = useState<RetentionView | null>(null);
  const [f, setF] = useState<RetentionSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const action = useAction();
  useEffect(() => { if (data) { setView(data); setF(data.settings); } }, [data]);
  if (error) return <ErrorBox error={error} />;
  if (!view || !f) return null;
  const set = (p: Partial<RetentionSettings>) => setF({ ...f, ...p });
  const save = () => action.run(async () => { const v = await api.saveRetention(f); setView(v); setF(v.settings); setSaved(true); setTimeout(() => setSaved(false), 2500); });
  const st = view.stored;
  return (
    <form className={`panel${view.settings.hold_all ? " warn" : ""}`} onSubmit={(e) => { e.preventDefault(); save(); }}>
      <h2>Retention &amp; legal hold</h2>
      <p className="sub">Cypress cleans up each night at 3:30 AM. Open tickets are never touched, and a ticket's text and history are always kept; only its photos and videos are removed.</p>
      <p className="small">{plural(st.photos, "photo")} and {plural(st.videos, "video")} stored ({mb(st.bytes)}){st.held ? ` · ${st.held} on hold` : ""}<br />
        <span className="muted">{lastRunText(view.last_run)}</span></p>
      <ErrorBox error={action.error} />
      <div className="grid3">
        <Field label="Uploads never attached to a report" hint="Hours. Photos or videos from chats that didn't become a report">
          <input type="number" min={1} max={720} value={f.unattached_hours} onChange={(e) => set({ unattached_hours: Number(e.target.value) })} /></Field>
        <Field label="Attachments on closed tickets" hint="Days after the ticket is resolved or closed">
          <input type="number" min={1} max={3650} disabled={!f.closed_ticket_days} value={f.closed_ticket_days || ""} placeholder="Kept"
            onChange={(e) => set({ closed_ticket_days: Number(e.target.value) })} />
          <label className="check small"><input type="checkbox" checked={!f.closed_ticket_days} onChange={(e) => set({ closed_ticket_days: e.target.checked ? 0 : 180 })} /> Keep forever</label></Field>
        <Field label="Member lookup log" hint="Days. The log records which addresses staff asked about">
          <input type="number" min={1} max={3650} disabled={!f.lookup_log_days} value={f.lookup_log_days || ""} placeholder="Kept"
            onChange={(e) => set({ lookup_log_days: Number(e.target.value) })} />
          <label className="check small"><input type="checkbox" checked={!f.lookup_log_days} onChange={(e) => set({ lookup_log_days: e.target.checked ? 0 : 365 })} /> Keep forever</label></Field>
      </div>
      <h3>Legal hold</h3>
      <p className="sub">Holding a ticket keeps its attachments until the hold is released. Board members and admins can hold a single ticket from the ticket page. Holding everything stops all cleanup, including the lookup log. Held files are also locked in storage, so the 2-year storage rule can't remove them either.</p>
      <label className="check"><input type="checkbox" checked={f.hold_all} onChange={(e) => set({ hold_all: e.target.checked })} /> Hold everything</label>
      {f.hold_all && <Field label="Why" hint="Required. For example, the matter or who asked for the hold">
        <input value={f.hold_all_reason ?? ""} maxLength={500} required minLength={3} onChange={(e) => set({ hold_all_reason: e.target.value })} /></Field>}
      {view.holds.length > 0 && <>
        <p className="small"><b>Tickets on hold</b></p>
        <ul className="plain tight">{view.holds.map((h) => (
          <li key={h.ticket_id} className="small">🔒 <Link to={`/admin/tickets/${h.ticket_id}`}>#{h.ticket_id} {h.title}</Link>
            <span className="muted"> · {h.reason} · {h.set_by}, {ago(h.set_at)}</span></li>
        ))}</ul></>}
      <div className="row-end"><span className="small muted">{saved ? "Saved" : ""}</span><button className="btn primary" disabled={action.busy}>Save</button></div>
    </form>
  );
}
