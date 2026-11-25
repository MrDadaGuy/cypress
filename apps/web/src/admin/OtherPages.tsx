import { Link, useParams } from "react-router-dom";
import { api } from "./api";
import { ago, ErrorBox, fmt, useAction, useLoad } from "./ui";
import JobsTable from "./JobsTable";

export function JobsPage() {
  return (
    <>
      <header className="page-head"><div><h1>Ingest runs</h1><p className="sub">Every crawl and PDF run across all document sets.</p></div></header>
      <JobsTable />
    </>
  );
}

export function DocumentPage() {
  const id = Number(useParams().id);
  const { data: d, error, reload } = useLoad(() => api.document(id), [id]);
  const action = useAction();
  if (error) return <ErrorBox error={error} />;
  if (!d) return <p className="muted">Loading…</p>;
  const excluded = d.status === "excluded";
  return (
    <>
      <nav className="crumbs"><Link to="/admin/sets">Document sets</Link> / <Link to={`/admin/sets/${d.set_id}?tab=documents`}>Set</Link> / <span>Document</span></nav>
      <header className="page-head">
        <div>
          <h1>{d.title || "Untitled"}</h1>
          <p className="mono small break"><a href={d.uri} target="_blank" rel="noreferrer">{d.uri}</a></p>
          <p className="stats small">{d.mime} · v{d.version} · fetched {ago(d.fetched_at)} · {fmt(d.markdown.length)} characters</p>
        </div>
        <button className={`btn ${excluded ? "primary" : "ghost danger"}`} disabled={action.busy}
          onClick={() => action.run(async () => { await api.setDocumentStatus(id, excluded ? "active" : "excluded"); reload(); })}>
          {excluded ? "Include in retrieval" : "Exclude from retrieval"}
        </button>
      </header>
      <ErrorBox error={action.error} />
      <div className="doc-layout">
        <pre className="markdown">{d.markdown}</pre>
        <aside className="panel slim">
          <h3>Versions</h3>
          <ul className="plain">{d.versions.length ? d.versions.map((v) => (
            <li key={v.version}><b>v{v.version}</b> <span className="small muted">{ago(v.fetched_at)}</span><div className="mono small muted">{v.content_hash.slice(0, 12)}</div></li>
          )) : <li className="muted small">Only the current version.</li>}</ul>
          {Object.keys(d.meta).length > 0 && <><h3>Metadata</h3><pre className="mono small">{JSON.stringify(d.meta, null, 2)}</pre></>}
        </aside>
      </div>
    </>
  );
}

export function AuditPage() {
  const { data: rows, error } = useLoad(api.audit);
  return (
    <>
      <header className="page-head"><div><h1>Audit log</h1><p className="sub">Every admin change, who made it and when.</p></div></header>
      <ErrorBox error={error} />
      {rows && (
        <div className="tbl">
          <table className="stack-sm">
            <thead><tr><th>When</th><th>Who</th><th>Action</th><th>What</th><th>Detail</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.id}>
                <td className="small" title={r.at}>{ago(r.at)}</td>
                <td className="small">{r.actor}</td>
                <td><span className="chip">{r.action}</span></td>
                <td className="small">{r.entity.replace("_", " ")} <span className="mono">#{r.entity_id}</span></td>
                <td className="mono small break">{JSON.stringify(r.detail)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}
