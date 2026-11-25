import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, DOC_TYPES, type DocSet, type DocType, type Source, type Visibility } from "./api";
import { ago, Empty, ErrorBox, Field, fmt, StatusChip, useAction, useLoad, usePoll, VisChip, VisOptions } from "./ui";
import JobsTable from "./JobsTable";

type Tab = "sources" | "documents" | "jobs" | "settings";

export default function SetDetail() {
  const id = Number(useParams().id);
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "sources";
  const { data: set, error, reload } = useLoad(() => api.set(id), [id]);
  const busyJobs = !!set?.sources.some((s) => s.last_job && ["queued", "running"].includes(s.last_job.status));
  usePoll(busyJobs, reload);
  const action = useAction();

  if (error) return <ErrorBox error={error} />;
  if (!set) return <p className="muted">Loading…</p>;

  return (
    <>
      <nav className="crumbs"><Link to="/admin/sets">Document sets</Link> / <span>{set.name}</span></nav>
      <header className="page-head">
        <div>
          <h1>{set.name} <VisChip v={set.visibility} />{!set.enabled && <span className="chip muted">disabled</span>}</h1>
          <p className="sub">{set.description || <span className="muted">No description.</span>}</p>
          <p className="stats mono small">{set.slug} · {set.doc_type.replace("_", " ")} · rank {set.authority_rank} · {fmt(set.document_count)} documents · {fmt(set.chunk_count)} chunks</p>
        </div>
        <button className="btn primary" disabled={action.busy || set.sources.length === 0}
          onClick={() => action.run(async () => { await api.runSet(id); reload(); })}>
          Run all sources
        </button>
      </header>
      <ErrorBox error={action.error} />
      <div className="tabs" role="tablist">
        {(["sources", "documents", "jobs", "settings"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setParams({ tab: t })}>
            {t[0].toUpperCase() + t.slice(1)}{t === "sources" && ` (${set.sources.length})`}{t === "documents" && ` (${set.document_count})`}
          </button>
        ))}
      </div>
      {tab === "sources" && <Sources set={set} reload={reload} />}
      {tab === "documents" && <Documents setId={id} />}
      {tab === "jobs" && <JobsTable setId={id} />}
      {tab === "settings" && <Settings set={set} reload={reload} />}
    </>
  );
}

// ---------------- sources ----------------

function Sources({ set, reload }: { set: DocSet & { sources: Source[] }; reload: () => void }) {
  const [adding, setAdding] = useState(false);
  const action = useAction();
  return (
    <section>
      <ErrorBox error={action.error} />
      {set.sources.length === 0 && !adding && (
        <Empty title="No sources yet">
          <p>Add a website to crawl or a link to a PDF. Each run converts what it finds to Markdown, then chunks and embeds it.</p>
        </Empty>
      )}
      {set.sources.length > 0 && (
        <div className="tbl">
          <table className="stack-sm">
            <thead><tr><th>Source</th><th>Kind</th><th>Settings</th><th>Schedule</th><th>Last run</th><th></th></tr></thead>
            <tbody>
              {set.sources.map((s) => (
                <tr key={s.id} className={s.enabled ? "" : "disabled"}>
                  <td>
                    {s.label && <div className="strong">{s.label}</div>}
                    <a href={s.url ?? "#"} target="_blank" rel="noreferrer" className="mono small break">{s.url}</a>
                  </td>
                  <td><span className="chip">{s.kind === "web" ? "Web crawl" : s.kind.toUpperCase()}</span></td>
                  <td className="small">{s.kind === "web"
                    ? <>depth {s.config.max_depth}, ≤{s.config.max_pages} pages{s.config.follow_pdfs && ", follows PDFs"}<div className="muted mono">{s.config.allow_domains?.join(", ")}</div></>
                    : <>OCR: {s.config.ocr}</>}</td>
                  <td>
                    <select value={s.schedule} aria-label="Schedule"
                      onChange={(e) => action.run(async () => { await api.updateSource(s.id, { schedule: e.target.value as Source["schedule"] }); reload(); })}>
                      <option value="manual">Manual</option><option value="daily">Daily</option><option value="weekly">Weekly</option>
                    </select>
                    {s.next_check && <div className="small muted">Next check {nextCheck(s.next_check)}</div>}
                  </td>
                  <td>
                    <StatusChip status={s.last_job?.status} /> <span className="small muted">{ago(s.last_job?.created_at)}</span>
                    {s.last_job?.error && <div className="small err-text" title={s.last_job.error}>{s.last_job.error.slice(0, 80)}</div>}
                  </td>
                  <td className="row-actions">
                    <button className="btn small" disabled={action.busy || !s.enabled}
                      onClick={() => action.run(async () => { await api.runSource(s.id); reload(); })}>Run</button>
                    <button className="btn small ghost" disabled={action.busy}
                      onClick={() => action.run(async () => { await api.updateSource(s.id, { enabled: !s.enabled }); reload(); })}>
                      {s.enabled ? "Disable" : "Enable"}</button>
                    <button className="btn small ghost danger" disabled={action.busy}
                      onClick={() => confirm(`Remove ${s.url}? Documents already ingested stay until the set is re-run.`) &&
                        action.run(async () => { await api.deleteSource(s.id); reload(); })}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {adding ? <AddSource setId={set.id} onDone={() => { setAdding(false); reload(); }} onCancel={() => setAdding(false)} />
        : <button className="btn" onClick={() => setAdding(true)}>Add source</button>}
    </section>
  );
}

/** "tonight", "tomorrow night" or "Tue, Nov 24" for the nightly re-crawl slot (early morning). */
function nextCheck(iso: string) {
  const d = new Date(iso), now = new Date();
  const nights = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 864e5);
  // The run is at 3:30 AM, so "tonight" means tomorrow's date (or today's, if it's past midnight).
  if (nights <= 0 || (nights === 1 && now.getHours() >= 4)) return "tonight";
  if (nights === 2 && now.getHours() >= 4) return "tomorrow night";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

const lines = (s: string) => s.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

function AddSource({ setId, onDone, onCancel }: { setId: number; onDone: () => void; onCancel: () => void }) {
  const [kind, setKind] = useState<"web" | "pdf">("web");
  const [url, setUrl] = useState("");
  const [label, setLabel] = useState("");
  const [schedule, setSchedule] = useState("manual");
  const [depth, setDepth] = useState(2);
  const [maxPages, setMaxPages] = useState(200);
  const [domains, setDomains] = useState("");
  const [include, setInclude] = useState("");
  const [exclude, setExclude] = useState("");
  const [followPdfs, setFollowPdfs] = useState(true);
  const [ocr, setOcr] = useState("auto");
  const { busy, error, run } = useAction();

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const config = kind === "web"
      ? { max_depth: depth, max_pages: maxPages, allow_domains: lines(domains), include: lines(include), exclude: lines(exclude), follow_pdfs: followPdfs }
      : { ocr };
    run(async () => { await api.createSource(setId, { kind, url, label, schedule, config }); onDone(); });
  };

  return (
    <form className="panel" onSubmit={submit}>
      <h2>Add source</h2>
      <div className="seg" role="radiogroup">
        <button type="button" className={kind === "web" ? "on" : ""} onClick={() => setKind("web")}>Website crawl</button>
        <button type="button" className={kind === "pdf" ? "on" : ""} onClick={() => setKind("pdf")}>PDF link</button>
      </div>
      <div className="grid2">
        <Field label={kind === "web" ? "Start URL" : "PDF URL"}>
          <input type="url" className="mono" required value={url} autoFocus
            placeholder={kind === "web" ? "https://docs.lakehillsca.org/" : "https://lakehillsca.org/…/rental-agreement.pdf"}
            onChange={(e) => setUrl(e.target.value)} /></Field>
        <Field label="Label (optional)"><input value={label} maxLength={200} onChange={(e) => setLabel(e.target.value)} /></Field>
      </div>
      {kind === "web" ? (
        <>
          <div className="grid3">
            <Field label={`Link depth: ${depth}`} hint="0 = only the start page.">
              <input type="range" min={0} max={6} value={depth} onChange={(e) => setDepth(+e.target.value)} /></Field>
            <Field label="Max pages"><input type="number" min={1} max={2000} value={maxPages} onChange={(e) => setMaxPages(+e.target.value)} /></Field>
            <Field label="Schedule"><ScheduleSelect value={schedule} onChange={setSchedule} /></Field>
          </div>
          <details>
            <summary>Crawl filters</summary>
            <div className="grid3">
              <Field label="Allowed domains" hint="One per line. Empty = the start URL's host.">
                <textarea rows={3} className="mono" value={domains} onChange={(e) => setDomains(e.target.value)} /></Field>
              <Field label="Only paths matching" hint="Prefixes or globs, e.g. /documents/*">
                <textarea rows={3} className="mono" value={include} onChange={(e) => setInclude(e.target.value)} /></Field>
              <Field label="Skip paths matching" hint="e.g. /cart, /login, *?replytocom=*">
                <textarea rows={3} className="mono" value={exclude} onChange={(e) => setExclude(e.target.value)} /></Field>
            </div>
            <label className="check"><input type="checkbox" checked={followPdfs} onChange={(e) => setFollowPdfs(e.target.checked)} /> Download and ingest linked PDFs</label>
          </details>
        </>
      ) : (
        <div className="grid3">
          <Field label="OCR" hint="Auto runs OCR only on pages with little or no text, such as scanned court records.">
            <select value={ocr} onChange={(e) => setOcr(e.target.value)}>
              <option value="auto">Auto</option><option value="always">Always</option><option value="never">Never</option>
            </select></Field>
          <Field label="Schedule"><ScheduleSelect value={schedule} onChange={setSchedule} /></Field>
        </div>
      )}
      <ErrorBox error={error} />
      <div className="actions">
        <button className="btn primary" disabled={busy}>{busy ? "Adding…" : "Add source"}</button>
        <button type="button" className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function ScheduleSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return <select value={value} onChange={(e) => onChange(e.target.value)}>
    <option value="manual">Manual</option><option value="daily">Daily (re-embeds only changed pages)</option><option value="weekly">Weekly</option>
  </select>;
}

// ---------------- documents ----------------

function Documents({ setId }: { setId: number }) {
  const [q, setQ] = useState("");
  const { data: docs, error } = useLoad(() => api.documents(setId, q), [setId, q]);
  return (
    <section>
      <input type="search" className="search" placeholder="Filter by title or URL" value={q} onChange={(e) => setQ(e.target.value)} />
      <ErrorBox error={error} />
      {docs && docs.length === 0 && <Empty title={q ? "No matches" : "No documents yet"}><p>{!q && "Run a source to fetch documents."}</p></Empty>}
      {docs && docs.length > 0 && (
        <div className="tbl">
          <table className="stack-sm">
            <thead><tr><th>Document</th><th>Status</th><th className="num">Chunks</th><th className="num">Size</th><th className="num">Version</th><th>Fetched</th></tr></thead>
            <tbody>
              {docs.map((d) => (
                <tr key={d.id} className={d.status === "excluded" ? "disabled" : ""}>
                  <td><Link to={`/admin/documents/${d.id}`} className="strong">{d.title || d.uri}</Link><div className="mono small muted break">{d.uri}</div></td>
                  <td><span className={`chip doc-${d.status}`}>{d.status.replace("_", " ")}</span></td>
                  <td className="num">{fmt(d.chunks)}</td>
                  <td className="num">{fmt(Math.round(d.chars / 1000))}k</td>
                  <td className="num">v{d.version}</td>
                  <td className="small">{ago(d.fetched_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ---------------- settings ----------------

function Settings({ set, reload }: { set: DocSet; reload: () => void }) {
  const nav = useNavigate();
  const [f, setF] = useState({
    name: set.name, description: set.description, visibility: set.visibility, doc_type: set.doc_type,
    authority_rank: set.authority_rank, enabled: set.enabled, settings: { ...set.settings },
  });
  const [saved, setSaved] = useState(false);
  const { busy, error, run } = useAction();
  const s = f.settings;
  const setS = (k: keyof typeof s, v: number | boolean) => setF({ ...f, settings: { ...s, [k]: v } });

  const save = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => { await api.updateSet(set.id, f); setSaved(true); reload(); });
  };

  return (
    <form className="panel" onSubmit={save} onChange={() => setSaved(false)}>
      <h2>Set details</h2>
      <div className="grid2">
        <Field label="Name"><input value={f.name} required onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Who can retrieve it">
          <select value={f.visibility} onChange={(e) => setF({ ...f, visibility: e.target.value as Visibility })}>
            <VisOptions />
          </select></Field>
      </div>
      <Field label="Description" hint="The agent's router reads this to decide when to search the set.">
        <textarea rows={2} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <div className="grid3">
        <Field label="Document type">
          <select value={f.doc_type} onChange={(e) => setF({ ...f, doc_type: e.target.value as DocType })}>
            {DOC_TYPES.map((t) => <option key={t} value={t}>{t.replace("_", " ")}</option>)}
          </select></Field>
        <Field label={`Authority rank: ${f.authority_rank}`}>
          <input type="range" min={0} max={100} step={5} value={f.authority_rank} onChange={(e) => setF({ ...f, authority_rank: +e.target.value })} /></Field>
        <Field label="Status">
          <label className="check"><input type="checkbox" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> Enabled for retrieval</label></Field>
      </div>

      <h3>Chunking</h3>
      <p className="small muted">Changes apply to the next run. Re-run the set to re-chunk existing documents.</p>
      <div className="grid3">
        <Field label="Chunk size (tokens)"><input type="number" min={100} max={4000} step={50} value={s.chunk_tokens} onChange={(e) => setS("chunk_tokens", +e.target.value)} /></Field>
        <Field label="Overlap (tokens)"><input type="number" min={0} max={1000} step={10} value={s.chunk_overlap} onChange={(e) => setS("chunk_overlap", +e.target.value)} /></Field>
      </div>

      <h3>Retrieval</h3>
      <div className="grid3">
        <Field label="Top K"><input type="number" min={1} max={50} value={s.top_k} onChange={(e) => setS("top_k", +e.target.value)} /></Field>
        <Field label={`Minimum score: ${s.min_score.toFixed(2)}`} hint="Drop weaker matches after reranking.">
          <input type="range" min={0} max={1} step={0.05} value={s.min_score} onChange={(e) => setS("min_score", +e.target.value)} /></Field>
        <Field label="Rerank"><label className="check"><input type="checkbox" checked={s.rerank} onChange={(e) => setS("rerank", e.target.checked)} /> Vertex Ranking API</label></Field>
      </div>

      <ErrorBox error={error} />
      <div className="actions">
        <button className="btn primary" disabled={busy}>{busy ? "Saving…" : "Save changes"}</button>
        {saved && <span className="ok-text">Saved</span>}
        <span className="spacer" />
        <button type="button" className="btn ghost danger" disabled={busy}
          onClick={() => confirm(`Delete "${set.name}" with all its sources, documents and chunks? This can't be undone.`) &&
            run(async () => { await api.deleteSet(set.id); nav("/admin/sets"); })}>Delete set</button>
      </div>
    </form>
  );
}
