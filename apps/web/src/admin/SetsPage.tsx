import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, DOC_TYPES, type DocType, type Visibility } from "./api";
import { ago, Empty, ErrorBox, Field, fmt, StatusChip, useAction, useLoad, VisChip, VisOptions } from "./ui";

export default function SetsPage() {
  const { data: sets, error, reload } = useLoad(api.sets);
  const [creating, setCreating] = useState(false);
  const { data: issues } = useLoad(api.contentIssueCount);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Document sets</h1>
          <p className="sub">Each set is a knowledge base the agent retrieves from: its sources, chunking and retrieval settings, and who may see it.</p>
        </div>
        {!creating && <button className="btn primary" onClick={() => setCreating(true)}>New set</button>}
      </header>
      {issues && issues.open > 0 && <div className="panel warn slim" role="status">
        <b>{issues.open} content inconsistenc{issues.open === 1 ? "y" : "ies"}</b>{issues.high ? ` (${issues.high} about fees, eligibility or rules)` : ""}: some documents disagree with each other.{" "}
        <Link to="/admin/checks">Review them</Link> and update the out-of-date document.</div>}
      <ErrorBox error={error} />
      {creating && <NewSet onCancel={() => setCreating(false)} />}
      {sets && sets.length === 0 && !creating && (
        <Empty title="No document sets yet">
          <p>Start with one per kind of content, for example <em>Governing documents</em> (public), <em>Park rules</em> (public) and <em>Court records</em> (board only).</p>
          <button className="btn primary" onClick={() => setCreating(true)}>Create the first set</button>
        </Empty>
      )}
      {sets && sets.length > 0 && (
        <div className="tbl">
          <table className="stack-sm">
            <thead><tr><th>Set</th><th>Visibility</th><th>Type</th><th className="num">Sources</th><th className="num">Documents</th><th className="num">Chunks</th><th>Last run</th></tr></thead>
            <tbody>
              {sets.map((s) => (
                <tr key={s.id} className={s.enabled ? "" : "disabled"}>
                  <td><Link to={`/admin/sets/${s.id}`} className="strong">{s.name}</Link><div className="mono small">{s.slug}{!s.enabled && " · disabled"}</div></td>
                  <td><VisChip v={s.visibility} /></td>
                  <td>{s.doc_type.replace("_", " ")}</td>
                  <td className="num">{fmt(s.source_count)}</td>
                  <td className="num">{fmt(s.document_count)}</td>
                  <td className="num">{fmt(s.chunk_count)}</td>
                  <td><StatusChip status={s.last_job?.status} /> <span className="small muted">{ago(s.last_job?.created_at)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <button className="btn ghost small" onClick={reload} hidden={!sets}>Refresh</button>
    </>
  );
}

const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);

function NewSet({ onCancel }: { onCancel: () => void }) {
  const nav = useNavigate();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<Visibility>("public");
  const [docType, setDocType] = useState<DocType>("general");
  const [rank, setRank] = useState(50);
  const { busy, error, run } = useAction();

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      const s = await api.createSet({ name, slug: slug || slugify(name), description, visibility, doc_type: docType, authority_rank: rank });
      nav(`/admin/sets/${s.id}`);
    });
  };

  return (
    <form className="panel" onSubmit={submit}>
      <h2>New document set</h2>
      <div className="grid2">
        <Field label="Name"><input value={name} required autoFocus maxLength={120}
          onChange={(e) => { setName(e.target.value); if (!slugTouched) setSlug(slugify(e.target.value)); }} /></Field>
        <Field label="Slug" hint="Used in citations and logs. Lowercase letters, numbers and dashes.">
          <input className="mono" value={slug} required pattern="[a-z0-9][a-z0-9\-]{1,62}"
            onChange={(e) => { setSlug(e.target.value); setSlugTouched(true); }} /></Field>
      </div>
      <Field label="Description" hint="What's in this set. The agent's router reads this to decide when to search it.">
        <textarea rows={2} value={description} maxLength={2000} onChange={(e) => setDescription(e.target.value)} /></Field>
      <div className="grid3">
        <Field label="Who can retrieve it">
          <select value={visibility} onChange={(e) => setVisibility(e.target.value as Visibility)}>
            <VisOptions />
          </select></Field>
        <Field label="Document type">
          <select value={docType} onChange={(e) => setDocType(e.target.value as DocType)}>
            {DOC_TYPES.map((t) => <option key={t} value={t}>{t.replace("_", " ")}</option>)}
          </select></Field>
        <Field label={`Authority rank: ${rank}`} hint="When sources disagree, higher rank wins (bylaws 90, FAQ 30).">
          <input type="range" min={0} max={100} step={5} value={rank} onChange={(e) => setRank(+e.target.value)} /></Field>
      </div>
      <ErrorBox error={error} />
      <div className="actions">
        <button className="btn primary" disabled={busy}>{busy ? "Creating…" : "Create set"}</button>
        <button type="button" className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
