import { useMemo, useState } from "react";
import { api, OFFICER_TITLES, ROLE_INFO, type Term, type Group, type GroupIn, type PeopleData, type Person, type PersonIn, type Role, type SlackSync, type SlackChannel } from "./api";
import { ago, Empty, ErrorBox, Field, useAction, useLoad } from "./ui";

type Tab = "people" | "groups" | "slack";
const blankPerson: PersonIn = { email: "", name: "", role: "staff", active: true, slack_user_id: "", notes: "", phone: "", responsibilities: "", groups: [], terms: [] };
const blankTerm = (): Term => {
  const start = new Date().toISOString().slice(0, 10);
  const end = new Date(Date.now() + 2 * 365 * 864e5).toISOString().slice(0, 10);   // placeholder; set from the bylaws
  return { title: "Director", seat: null, how: "elected", start_date: start, end_date: end, ended_on: null, end_reason: null, notes: null };
};
const fmtDate = (d: string | null) => (d ? new Date(d + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "");
const blankGroup: GroupIn = { name: "", description: "", ticket_categories: [], members: [] };

export default function PeoplePage() {
  const { data, error, reload } = useLoad(api.people);
  const [tab, setTab] = useState<Tab>("people");
  const [editing, setEditing] = useState<{ id: number | null; form: PersonIn } | null>(null);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p className="muted">Loading…</p>;
  const addFromSlack = (u: SlackSync["unlisted"][number]) => {
    setEditing({ id: null, form: { ...blankPerson, email: u.email ?? "", name: u.name, slack_user_id: u.slack_user_id } });
    setTab("people");
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  return (
    <>
      <header className="page-head"><div><h1>Users &amp; roles</h1>
        <p className="sub">Board, staff and security volunteers, their Slack accounts, and groups. A role decides what Cypress shares and which tools it offers, on the web and in Slack. Changes apply within a minute. This is not the member roster.</p></div></header>
      <div className="tabs" role="tablist">
        {(["people", "groups", "slack"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
            {t === "people" ? `People (${data.people.length})` : t === "groups" ? `Groups (${data.groups.length})` : "Slack"}
          </button>))}
      </div>
      {tab === "people" && <PeopleTab data={data} reload={reload} editing={editing} setEditing={setEditing} />}
      {tab === "groups" && <GroupsTab data={data} reload={reload} />}
      {tab === "slack" && <SlackTab data={data} reload={reload} onAdd={addFromSlack} />}
    </>
  );
}

function RoleChip({ role, active = true, lapsed = false }: { role: Role | null; active?: boolean; lapsed?: boolean }) {
  if (!active) return <span className="chip muted">inactive</span>;
  if (lapsed) return <span className="chip muted" title="Their board term has ended, so the board role no longer applies">term ended</span>;
  return role ? <span className={`chip role-${role}`} title={ROLE_INFO[role]}>{role}</span> : <span className="chip muted">no role</span>;
}

function PeopleTab({ data, reload, editing, setEditing }: {
  data: PeopleData; reload: () => void;
  editing: { id: number | null; form: PersonIn } | null; setEditing: (e: { id: number | null; form: PersonIn } | null) => void;
}) {
  const [q, setQ] = useState("");
  const [only, setOnly] = useState<"all" | "board" | "staff">("all");
  const groupName = useMemo(() => new Map(data.groups.map((g) => [g.id, g.name])), [data.groups]);
  const shown = data.people
    .filter((p) => only === "all" || (only === "board" ? p.on_board : ["staff", "security"].includes(p.effective_role ?? "")))
    .filter((p) => !q || `${p.name ?? ""} ${p.email} ${p.role ?? ""} ${p.board_title ?? ""} ${p.slack_name ?? ""} ${p.responsibilities ?? ""}`.toLowerCase().includes(q.toLowerCase()));
  const edit = (p: Person) => setEditing({ id: p.id, form: { email: p.email, name: p.name, role: p.role, active: p.active, slack_user_id: p.slack_user_id,
    notes: p.notes, phone: p.phone, responsibilities: p.responsibilities, groups: p.groups, terms: p.terms } });
  const nBoard = data.people.filter((p) => p.on_board).length;
  return (
    <>
      {editing
        ? <PersonForm data={data} editing={editing} onDone={() => { setEditing(null); reload(); }} onCancel={() => setEditing(null)} />
        : <div className="toolbar">
            <div className="seg" role="group" aria-label="Show">
              {([["all", "Everyone"], ["board", `Board (${nBoard})`], ["staff", "Staff & security"]] as const).map(([k, l]) => (
                <button key={k} className={only === k ? "on" : ""} aria-pressed={only === k} onClick={() => setOnly(k)}>{l}</button>))}
            </div>
            <input type="search" placeholder="Search names, roles, responsibilities" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search people" />
            <button className="btn primary" onClick={() => setEditing({ id: null, form: blankPerson })}>Add person</button>
          </div>}
      {shown.length === 0 ? <Empty title={q ? "No one matches" : "No one listed yet"} /> : (
        <div className="tbl"><table className="stack-sm">
          <thead><tr><th>Person</th><th>Role</th><th>Board</th><th>Responsibilities</th><th>Slack</th><th>Last seen</th><th /></tr></thead>
          <tbody>{shown.map((p) => (
            <tr key={p.id} className={p.active ? "" : "disabled"}>
              <td data-label="Person"><div className="strong">{p.name || p.email}</div>{p.name && <div className="small muted break">{p.email}</div>}</td>
              <td data-label="Role"><RoleChip role={p.effective_role} active={p.active} lapsed={p.role === "board" && !p.effective_role && p.terms.length > 0} /></td>
              <td data-label="Board" className="small">{p.on_board ? <><div>{p.board_title}</div><div className="muted">until {fmtDate(p.term_end)}</div></> : <span className="muted">—</span>}</td>
              <td data-label="Responsibilities" className="small" title={p.responsibilities ?? ""}>
                <div className="clip">{p.responsibilities || <span className="muted">—</span>}</div>
                {p.groups.length > 0 && <div className="muted">Groups: {p.groups.map((g) => groupName.get(g)).filter(Boolean).join(", ")}</div>}</td>
              <td data-label="Slack" className="small">{p.slack_user_id ? <span title={p.slack_user_id}>✓ {p.slack_name ?? p.slack_user_id}</span> : <span className="muted">not linked</span>}</td>
              <td data-label="Last seen" className="small muted">{ago(maxDate(p.last_seen_web, p.last_seen_slack))}</td>
              <td className="row-actions"><button className="btn small" onClick={() => edit(p)}>Edit</button></td>
            </tr>))}</tbody>
        </table></div>)}
    </>
  );
}

const maxDate = (a: string | null, b: string | null) => (!a ? b : !b ? a : a > b ? a : b);

function PersonForm({ data, editing, onDone, onCancel }: {
  data: PeopleData; editing: { id: number | null; form: PersonIn }; onDone: () => void; onCancel: () => void;
}) {
  const [f, setF] = useState<PersonIn>(editing.form);
  const action = useAction();
  const set = (patch: Partial<PersonIn>) => setF((x) => ({ ...x, ...patch }));
  const body = (): PersonIn => ({ ...f, name: f.name?.trim() || null, slack_user_id: f.slack_user_id?.trim() || null, notes: f.notes?.trim() || null,
    phone: f.phone?.trim() || null, responsibilities: f.responsibilities?.trim() || null });
  const save = () => action.run(async () => {
    if (editing.id === null) await api.addPerson(body()); else await api.savePerson(editing.id, body());
    onDone();
  });
  const remove = () => action.run(async () => { await api.removePerson(editing.id!); onDone(); });
  return (
    <form className="panel" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <h2>{editing.id === null ? "Add person" : `Edit ${editing.form.name || editing.form.email}`}</h2>
      <ErrorBox error={action.error} />
      <div className="grid2">
        <Field label="Email" hint="The Google account they sign in with. Also used to find them in Slack.">
          <input type="email" required value={f.email} autoComplete="off" onChange={(e) => set({ email: e.target.value })} /></Field>
        <Field label="Name"><input value={f.name ?? ""} maxLength={120} onChange={(e) => set({ name: e.target.value })} /></Field>
        <Field label="Cypress access" hint={(f.role ? ROLE_INFO[f.role] : "Treated like the public") + (f.terms.length ? ". An active board term gives board access; it ends with the term." : "")}>
          <select value={f.role ?? ""} onChange={(e) => set({ role: (e.target.value || null) as Role | null })}>
            {data.roles.map((r) => <option key={r} value={r}>{r}</option>)}
            <option value="">no role</option>
          </select></Field>
        <Field label="Slack user id" hint={data.slack_connected ? "Leave blank to look it up by email when you save." : "Slack isn't connected."}>
          <input value={f.slack_user_id ?? ""} placeholder="U0123ABCD" maxLength={20} className="mono" onChange={(e) => set({ slack_user_id: e.target.value })} /></Field>
      </div>
      <div className="grid2">
        <Field label="Phone" hint="Optional. Only admins see this; Cypress never shares it.">
          <input type="tel" value={f.phone ?? ""} maxLength={40} autoComplete="off" onChange={(e) => set({ phone: e.target.value })} /></Field>
        <div />
      </div>
      <Field label="Responsibilities" hint="What this person looks after, in plain words. Cypress reads this to suggest an owner for new tickets, e.g. “Pool: gate, chemicals, lifeguards. Clubhouse rentals.”">
        <textarea rows={3} maxLength={2000} value={f.responsibilities ?? ""} onChange={(e) => set({ responsibilities: e.target.value })} /></Field>
      <TermsEditor terms={f.terms} onChange={(terms) => set({ terms })} />
      {data.groups.length > 0 && <fieldset className="checks"><legend className="lbl">Groups</legend>
        {data.groups.map((g) => <label key={g.id} className="check"><input type="checkbox" checked={f.groups.includes(g.id)}
          onChange={(e) => set({ groups: e.target.checked ? [...f.groups, g.id] : f.groups.filter((x) => x !== g.id) })} />{g.name}</label>)}
      </fieldset>}
      <Field label="Notes" hint="Only admins see this."><textarea rows={2} maxLength={1000} value={f.notes ?? ""} onChange={(e) => set({ notes: e.target.value })} /></Field>
      <label className="check"><input type="checkbox" checked={f.active} onChange={(e) => set({ active: e.target.checked })} />Active (untick to suspend access without removing them)</label>
      <div className="row-end wrap">
        {editing.id !== null && <button type="button" className="btn danger" disabled={action.busy} onClick={remove}>Remove</button>}
        <span className="spacer" />
        <button type="button" className="btn ghost" onClick={onCancel}>Cancel</button>
        <button className="btn primary" disabled={action.busy}>{action.busy ? "Saving…" : "Save"}</button>
      </div>
    </form>
  );
}

function TermsEditor({ terms, onChange }: { terms: Term[]; onChange: (t: Term[]) => void }) {
  const upd = (i: number, patch: Partial<Term>) => onChange(terms.map((t, j) => (j === i ? { ...t, ...patch } : t)));
  return (
    <fieldset className="terms">
      <legend className="lbl">Board terms</legend>
      {terms.length === 0 && <p className="small muted">Not on the board. Add a term for board members: it gives them board access until the term ends, and lists them on the board roster.</p>}
      {terms.map((t, i) => (
        <div key={i} className="term">
          <div className="grid3">
            <Field label="Position">
              <input list="officer-titles" value={t.title} maxLength={60} required onChange={(e) => upd(i, { title: e.target.value })} /></Field>
            <Field label="Term starts"><input type="date" required value={t.start_date} onChange={(e) => upd(i, { start_date: e.target.value })} /></Field>
            <Field label="Term ends"><input type="date" required value={t.end_date} min={t.start_date} onChange={(e) => upd(i, { end_date: e.target.value })} /></Field>
            <Field label="How">
              <select value={t.how} onChange={(e) => upd(i, { how: e.target.value as Term["how"] })}>
                <option value="elected">Elected</option><option value="appointed">Appointed to a vacancy</option></select></Field>
            <Field label="Seat (optional)"><input value={t.seat ?? ""} maxLength={40} onChange={(e) => upd(i, { seat: e.target.value || null })} /></Field>
            <Field label="Left early">
              <select value={t.end_reason ?? ""} onChange={(e) => upd(i, e.target.value
                ? { end_reason: e.target.value as Term["end_reason"], ended_on: t.ended_on ?? new Date().toISOString().slice(0, 10) }
                : { end_reason: null, ended_on: null })}>
                <option value="">No, serving the full term</option><option value="resigned">Resigned</option>
                <option value="removed">Removed</option><option value="other">Other</option></select></Field>
            {t.end_reason && <Field label="Left on"><input type="date" required value={t.ended_on ?? ""} min={t.start_date} onChange={(e) => upd(i, { ended_on: e.target.value })} /></Field>}
          </div>
          <div className="row-end"><button type="button" className="btn small danger" onClick={() => onChange(terms.filter((_, j) => j !== i))}>Remove term</button></div>
        </div>))}
      <datalist id="officer-titles">{OFFICER_TITLES.map((o) => <option key={o} value={o} />)}</datalist>
      <button type="button" className="btn small" onClick={() => onChange([blankTerm(), ...terms])}>Add term</button>
    </fieldset>
  );
}

function GroupsTab({ data, reload }: { data: PeopleData; reload: () => void }) {
  const [editing, setEditing] = useState<{ id: number | null; form: GroupIn } | null>(null);
  const name = useMemo(() => new Map(data.people.map((p) => [p.id, p.name || p.email])), [data.people]);
  const edit = (g: Group) => setEditing({ id: g.id, form: { name: g.name, description: g.description, ticket_categories: g.ticket_categories, members: g.members } });
  return (
    <>
      <p className="sub">Teams such as a pool committee or the security patrol. When a new ticket comes in for a category a group handles, Cypress @-mentions the group's members in Slack.</p>
      {editing
        ? <GroupForm data={data} editing={editing} onDone={() => { setEditing(null); reload(); }} onCancel={() => setEditing(null)} />
        : <div className="toolbar"><span /><button className="btn primary" onClick={() => setEditing({ id: null, form: blankGroup })}>Add group</button></div>}
      {data.groups.length === 0 ? <Empty title="No groups yet"><p>Create one, add people, and pick the ticket categories it should hear about.</p></Empty> : (
        <div className="tbl"><table className="stack-sm">
          <thead><tr><th>Group</th><th>Ticket categories</th><th>Members</th><th /></tr></thead>
          <tbody>{data.groups.map((g) => (
            <tr key={g.id}>
              <td data-label="Group"><div className="strong">{g.name}</div>{g.description && <div className="small muted">{g.description}</div>}</td>
              <td data-label="Tickets">{g.ticket_categories.length ? g.ticket_categories.map((c) => <span key={c} className="chip">{c}</span>) : <span className="small muted">none</span>}</td>
              <td data-label="Members" className="small">{g.members.map((m) => name.get(m)).filter(Boolean).join(", ") || <span className="muted">—</span>}</td>
              <td className="row-actions"><button className="btn small" onClick={() => edit(g)}>Edit</button></td>
            </tr>))}</tbody>
        </table></div>)}
    </>
  );
}

function GroupForm({ data, editing, onDone, onCancel }: {
  data: PeopleData; editing: { id: number | null; form: GroupIn }; onDone: () => void; onCancel: () => void;
}) {
  const [f, setF] = useState<GroupIn>(editing.form);
  const action = useAction();
  const toggle = (k: "ticket_categories" | "members", v: never, on: boolean) =>
    setF((x) => ({ ...x, [k]: on ? [...(x[k] as never[]), v] : (x[k] as never[]).filter((y) => y !== v) }));
  const save = () => action.run(async () => {
    const body = { ...f, name: f.name.trim(), description: f.description?.trim() || null };
    if (editing.id === null) await api.addGroup(body); else await api.saveGroup(editing.id, body);
    onDone();
  });
  const remove = () => action.run(async () => { await api.removeGroup(editing.id!); onDone(); });
  const active = data.people.filter((p) => p.active || f.members.includes(p.id));
  return (
    <form className="panel" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <h2>{editing.id === null ? "Add group" : `Edit ${editing.form.name}`}</h2>
      <ErrorBox error={action.error} />
      <div className="grid2">
        <Field label="Name"><input required minLength={2} maxLength={80} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Description"><input maxLength={500} value={f.description ?? ""} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      </div>
      <fieldset className="checks"><legend className="lbl">Mention on new tickets about</legend>
        {data.categories.map((c) => <label key={c} className="check"><input type="checkbox" checked={f.ticket_categories.includes(c)}
          onChange={(e) => toggle("ticket_categories", c as never, e.target.checked)} />{c}</label>)}
      </fieldset>
      <fieldset className="checks"><legend className="lbl">Members</legend>
        {active.length === 0 && <span className="small muted">Add people first.</span>}
        {active.map((p) => <label key={p.id} className="check"><input type="checkbox" checked={f.members.includes(p.id)}
          onChange={(e) => toggle("members", p.id as never, e.target.checked)} />{p.name || p.email}{!p.slack_user_id && <span className="small muted"> (no Slack)</span>}</label>)}
      </fieldset>
      <div className="row-end wrap">
        {editing.id !== null && <button type="button" className="btn danger" disabled={action.busy} onClick={remove}>Delete group</button>}
        <span className="spacer" />
        <button type="button" className="btn ghost" onClick={onCancel}>Cancel</button>
        <button className="btn primary" disabled={action.busy}>{action.busy ? "Saving…" : "Save"}</button>
      </div>
    </form>
  );
}

function SlackTab({ data, reload, onAdd }: { data: PeopleData; reload: () => void; onAdd: (u: SlackSync["unlisted"][number]) => void }) {
  const [def, setDef] = useState<string>(data.slack_default_role ?? "");
  const [result, setResult] = useState<SlackSync | null>(null);
  const action = useAction();
  const sync = () => action.run(async () => { setResult(await api.slackSync()); reload(); });
  const saveDef = () => action.run(async () => { await api.saveSlackDefault((def || null) as Role | null); reload(); });
  if (!data.slack_connected) return <Empty title="Slack isn't connected" />;
  return (
    <>
      <ErrorBox error={action.error} />
      <div className="panel">
        <h2>People not listed here</h2>
        <p className="sub">What a full member of the Slack workspace gets when they aren't in the People list. Slack guests always get public access.</p>
        <div className="inline-form">
          <select value={def} onChange={(e) => setDef(e.target.value)} aria-label="Default role for unlisted Slack members">
            {data.roles.map((r) => <option key={r} value={r}>{r}</option>)}
            <option value="">no role (public)</option>
          </select>
          <button className="btn" disabled={action.busy || def === (data.slack_default_role ?? "")} onClick={saveDef}>Save</button>
        </div>
        {data.slack_default_role === "board" && <p className="small muted">Currently everyone in the workspace is treated as board. Once everyone who needs a role is listed, consider “member” or “no role”.</p>}
      </div>
      <div className="panel">
        <h2>Match with Slack</h2>
        <p className="sub">Links listed people to their Slack accounts by email, and shows workspace members who aren't listed yet.</p>
        <button className="btn primary" disabled={action.busy} onClick={sync}>{action.busy ? "Checking Slack…" : "Sync with Slack"}</button>
        {result && <div className="sync-result">
          <p className="small">{result.workspace_members} people in the workspace. {result.linked.length ? `Newly linked: ${result.linked.join(", ")}.` : "No new links."}</p>
          {result.unlisted.length > 0 && <div className="tbl"><table className="stack-sm">
            <thead><tr><th>Slack member</th><th>Email</th><th /></tr></thead>
            <tbody>{result.unlisted.map((u) => (
              <tr key={u.slack_user_id}>
                <td data-label="Member"><span className="strong">{u.name}</span> {u.guest && <span className="chip muted">guest</span>}</td>
                <td data-label="Email" className="small break">{u.email ?? <span className="muted">hidden</span>}</td>
                <td className="row-actions"><button className="btn small" onClick={() => onAdd(u)}>Add…</button></td>
              </tr>))}</tbody>
          </table></div>}
        </div>}
      </div>
      <ChannelsPanel />
    </>
  );
}

const ROLE_LABEL: Record<string, string> = { board: "Board", security: "Security", staff: "Staff", member: "Members", admin: "Board" };
const levelLabel = (r: string | null) => (r ? ROLE_LABEL[r] ?? r : "Public");

/** Which Slack channels Cypress is in, and the access level it answers at in each. */
function ChannelsPanel() {
  const [chans, setChans] = useState<SlackChannel[] | null>(null);
  const action = useAction();
  const load = () => action.run(async () => setChans((await api.slackChannels()).channels));
  const setCeiling = (c: SlackChannel, max: string) => action.run(async () => {
    await api.saveSlackChannel(c.channel_id, { name: c.name, max_role: max || null, note: c.note });
    setChans((await api.slackChannels()).channels);
  });
  return (
    <div className="panel">
      <h2>Channels</h2>
      <p className="sub">In a channel, everyone sees Cypress's answers, so it only shares what <b>every</b> member of that channel may see. If the security guard is in #security, answers there use security-level access, even when a board member asks. People who aren't listed above count as public. DMs use the asker's own role. You can also set a ceiling to keep a channel lower.</p>
      <ErrorBox error={action.error} />
      {!chans ? <button className="btn" disabled={action.busy} onClick={load}>{action.busy ? "Checking Slack…" : "Show channels"}</button>
        : chans.length === 0 ? <p className="small muted">Cypress isn't in any channels yet. Invite it with /invite @Cypress.</p>
        : <div className="tbl"><table className="stack-sm">
          <thead><tr><th>Channel</th><th>Members</th><th>Answers at</th><th>Ceiling</th></tr></thead>
          <tbody>{chans.map((c) => {
            const lowest = c.members.filter((m) => !m.role || m.guest);
            return (
              <tr key={c.channel_id}>
                <td data-label="Channel"><span className="strong">#{c.name}</span>{c.private && <span className="chip muted">private</span>}</td>
                <td data-label="Members" className="small">{c.members_known ? <>{c.members.length} people
                  {lowest.length > 0 && <div className="muted">Public: {lowest.map((m) => m.name ?? m.slack_user_id).join(", ")}</div>}</>
                  : <span className="warn-text">Can't see members (public channel), so answers are public. Make it private, or add the channels:read scope.</span>}</td>
                <td data-label="Answers at"><span className={`chip role-${c.effective_role ?? "member"}`}>{levelLabel(c.effective_role)}</span>
                  {c.max_role && c.auto_role !== c.effective_role && <div className="small muted">capped (members allow {levelLabel(c.auto_role)})</div>}</td>
                <td data-label="Ceiling">
                  <select value={c.max_role ?? ""} disabled={action.busy} onChange={(e) => setCeiling(c, e.target.value)} aria-label={`Ceiling for #${c.name}`}>
                    <option value="">No ceiling</option><option value="board">Board</option><option value="security">Security</option>
                    <option value="staff">Staff</option><option value="member">Members</option><option value="public">Public</option>
                  </select></td>
              </tr>);
          })}</tbody>
        </table></div>}
    </div>
  );
}
