import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type ChecksSettings, type ContentIssue, type DocChange } from "./api";
import { ago, Empty, ErrorBox, Field, useAction, useLoad, usePoll } from "./ui";

const SEV: Record<string, string> = { high: "Fees, eligibility or rules", medium: "Hours, dates or procedures", low: "Minor" };

export default function ContentChecksPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "changes" ? "changes" : "conflicts";
  return (
    <>
      <div className="toolbar"><div className="tabs" role="tablist">
        {([["conflicts", "Conflicts"], ["changes", "Changes"]] as const).map(([k, l]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setParams(k === "changes" ? { tab: k } : {})}>{l}</button>))}
      </div></div>
      {tab === "changes" ? <Changes /> : <Conflicts />}
    </>
  );
}

function Conflicts() {
  const [status, setStatus] = useState("open");
  const { data, error, reload } = useLoad(() => api.contentIssues(status), [status]);
  const { busy, error: aErr, run } = useAction();
  const scanning = !!data?.scans.some((s) => s.status === "queued" || s.status === "running");
  usePoll(scanning, reload, 5000);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p className="muted">Loading…</p>;
  const last = data.scans.find((s) => s.status === "done" || s.status === "failed");
  return (
    <>
      <header className="page-head"><div><h1>Content checks</h1>
        <p className="sub">Places where LHCA documents disagree, such as a fee in the bylaws that doesn't match the website, two sections of one document that contradict each other, or a rule one document allows and another forbids. Cypress keeps answering by its usual priority (bylaws first) and mentions the conflict; the fix is to update the out-of-date document, then re-run its ingest. Checks run automatically after each ingest.</p></div>
        <button className="btn" disabled={busy || scanning} onClick={() => run(async () => { await api.scanContent(); reload(); })}>{scanning ? "Scanning…" : "Scan now"}</button>
      </header>
      <ErrorBox error={aErr} />
      <p className="small muted">{scanning ? "A scan is running; this page updates when it's done. " : ""}
        {last ? `Last scan ${ago(last.finished_at ?? last.created_at)}: ${last.status === "failed" ? `failed (${last.error})` : `${last.stats.pairs_checked ?? 0} passage pairs checked, ${last.stats.new ?? 0} new, ${last.stats.auto_resolved ?? 0} resolved by document changes.`}` : "No scans yet."}</p>
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Show">
          {[["open", `Open (${data.counts.open})`], ["dismissed", "Not conflicts"], ["resolved", "Fixed"], ["all", "All"]].map(([k, l]) => (
            <button key={k} className={status === k ? "on" : ""} aria-pressed={status === k} onClick={() => setStatus(k)}>{l}</button>))}
        </div>
      </div>
      {data.issues.length === 0
        ? <Empty title={status === "open" ? "No inconsistencies found" : "Nothing here"}>{status === "open" && "The documents agree with each other, as far as the last scan could tell."}</Empty>
        : <div className="issues">{data.issues.map((i) => <Issue key={i.id} i={i} reload={reload} />)}</div>}
      <Settings s={data.settings} reload={reload} />
    </>
  );
}

const KIND: Record<DocChange["kind"], string> = { changed: "Updated", new: "New page", gone: "No longer on the site" };

/** What changed in documents since their previous version, so the board can see it at a glance. */
function Changes() {
  const [status, setStatus] = useState("new");
  const { data, error, reload } = useLoad(() => api.contentChanges(status), [status]);
  const { busy, error: aErr, run } = useAction();
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p className="muted">Loading…</p>;
  return (
    <>
      <header className="page-head"><div><h1>What changed</h1>
        <p className="sub">When a re-crawl finds an updated page or PDF, Cypress compares it with the previous version and lists the facts that changed. New pages and pages that disappeared show here too. Cypress already answers from the latest version; this is so the board knows what's different.</p></div>
        {data.unreviewed > 0 && <button className="btn" disabled={busy} onClick={() => run(async () => { await api.allChangesReviewed(); reload(); })}>Mark all reviewed</button>}
      </header>
      <ErrorBox error={aErr} />
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Show">
          {[["new", `To review (${data.unreviewed})`], ["reviewed", "Reviewed"], ["all", "All"]].map(([k, l]) => (
            <button key={k} className={status === k ? "on" : ""} aria-pressed={status === k} onClick={() => setStatus(k)}>{l}</button>))}
        </div>
      </div>
      {data.changes.length === 0
        ? <Empty title={status === "new" ? "Nothing new to review" : "Nothing here"}>Changes appear after a re-crawl finds updated documents.</Empty>
        : <div className="issues">{data.changes.map((c) => <Change key={c.id} c={c} reload={reload} />)}</div>}
    </>
  );
}

function Change({ c, reload }: { c: DocChange; reload: () => void }) {
  const { busy, error, run } = useAction();
  const set = (s: "new" | "reviewed") => run(async () => { await api.setChangeStatus(c.id, s); reload(); });
  const facts = [...(c.summary ?? [])].sort((a, b) => Number(b.important) - Number(a.important));
  return (
    <article className={`issue${facts.some((f) => f.important) ? " sev-high" : ""}`}>
      <header className="issue-head"><b className="break">{c.uri ? <a href={c.uri} target="_blank" rel="noreferrer">{c.title || "Untitled"}</a> : c.title || "Untitled"}</b>
        <span className={`chip${c.kind === "gone" ? " warn" : ""}`}>{KIND[c.kind]}</span>
        <span className="spacer" /><span className="small muted">{ago(c.at)}</span></header>
      {facts.length > 0 && <ul className="facts">{facts.map((f, i) => (
        <li key={i}><b>{f.what}</b>{f.important && <span className="chip st-failed">Important</span>}
          <div className="fact-vals">{f.before ? <del>{f.before}</del> : <span className="muted">(not there before)</span>}
            <span aria-hidden="true"> → </span>{f.after ? <ins>{f.after}</ins> : <span className="muted">(removed)</span>}</div></li>))}</ul>}
      {c.kind === "changed" && c.trivial && <p className="small muted">Only minor edits, such as formatting or a date stamped on the page.</p>}
      {c.kind === "changed" && !c.trivial && c.summary === null && <p className="small muted">Not summarized (summaries are off, or this crawl changed more documents than the limit). The exact changes are below.</p>}
      {c.kind === "changed" && !c.trivial && c.summary !== null && facts.length === 0 && <p className="small muted">No specific facts changed.</p>}
      {c.diff && <details><summary className="small">Exact changes ({c.removed} line{c.removed === 1 ? "" : "s"} out, {c.added} in)</summary>
        <pre className="diff">{c.diff.split("\n").filter((l) => !l.startsWith("---") && !l.startsWith("+++")).map((l, i) => (
          <span key={i} className={l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : ""}>{l.startsWith("@@") ? "⋯" : l}{"\n"}</span>))}</pre></details>}
      {c.status === "reviewed" && c.reviewed_by && <p className="small muted">Reviewed by {c.reviewed_by} {ago(c.reviewed_at)}</p>}
      <ErrorBox error={error} />
      <div className="actions wrap">{c.status === "new"
        ? <button className="btn small primary" disabled={busy} onClick={() => set("reviewed")}>Mark reviewed</button>
        : <button className="btn small ghost" disabled={busy} onClick={() => set("new")}>Back to review</button>}</div>
    </article>
  );
}

function Issue({ i, reload }: { i: ContentIssue; reload: () => void }) {
  const [note, setNote] = useState("");
  const [asking, setAsking] = useState(false);
  const { busy, error, run } = useAction();
  const set = (s: string) => run(async () => { await api.setIssueStatus(i.id, s, note || undefined); reload(); });
  return (
    <article className={`issue sev-${i.severity}`}>
      <header className="issue-head"><b>{i.topic}</b><span className={`chip ${i.severity === "high" ? "st-failed" : i.severity === "medium" ? "st-running" : "muted"}`}>{SEV[i.severity]}</span>
        <span className="spacer" /><span className="small muted">found {ago(i.found_at)}</span></header>
      <div className="sides">
        <Side title={i.a_title} uri={i.a_uri} says={i.a_says} excerpt={i.a_excerpt} />
        <Side title={i.b_title} uri={i.b_uri} says={i.b_says} excerpt={i.b_excerpt} />
      </div>
      {i.suggestion && <p className="small"><b>Suggestion:</b> {i.suggestion}</p>}
      {i.status !== "open" && <p className="small muted">{i.status === "resolved" ? "Fixed" : "Marked not a conflict"} by {i.closed_by} {ago(i.closed_at)}{i.note ? `: ${i.note}` : ""}</p>}
      <ErrorBox error={error} />
      {i.status === "open" ? <div className="actions wrap">
        {asking && <input value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="Note (optional), e.g. Bylaws amendment on the agenda" aria-label="Note" />}
        <button className="btn small primary" disabled={busy} onClick={() => (asking ? set("resolved") : setAsking(true))}>{asking ? "Save as fixed" : "Mark fixed"}</button>
        <button className="btn small ghost" disabled={busy} onClick={() => set("dismissed")}>Not a conflict</button>
      </div> : <button className="btn small ghost" disabled={busy} onClick={() => set("open")}>Reopen</button>}
    </article>
  );
}

function Side({ title, uri, says, excerpt }: { title: string; uri: string | null; says: string; excerpt: string | null }) {
  return (
    <div className="side-doc">
      <div className="small muted">{uri ? <a href={uri} target="_blank" rel="noreferrer">{title}</a> : title}</div>
      <div className="says">{says}</div>
      {excerpt && <details><summary className="small">Passage</summary><p className="small muted">{excerpt}</p></details>}
    </div>
  );
}

function Settings({ s, reload }: { s: ChecksSettings; reload: () => void }) {
  const [f, setF] = useState(s);
  const { busy, error, run } = useAction();
  return (
    <details className="panel slim mt-lg"><summary className="strong">Settings</summary>
      <ErrorBox error={error} />
      <label className="check"><input type="checkbox" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> Check documents after each ingest</label>
      <label className="check"><input type="checkbox" checked={f.same_document} onChange={(e) => setF({ ...f, same_document: e.target.checked })} /> Also compare sections within the same document</label>
      <label className="check"><input type="checkbox" checked={f.rules} onChange={(e) => setF({ ...f, rules: e.target.checked })} /> Also check rules without numbers (who may, what's allowed or required)</label>
      <label className="check"><input type="checkbox" checked={f.verify} onChange={(e) => setF({ ...f, verify: e.target.checked })} /> Double-check each conflict found with a stronger model (fewer false alarms; a few cents per scan)</label>
      <div className="grid2">
        <Field label="How similar passages must be to compare (0.5–0.99)" hint="Higher = fewer, closer pairs (cheaper, may miss some)">
          <input type="number" min={0.5} max={0.99} step={0.01} value={f.min_similarity} onChange={(e) => setF({ ...f, min_similarity: Number(e.target.value) })} /></Field>
        <Field label="Most pairs per scan" hint="Caps the cost of a scan (about $0.0006 per pair; a full scan of today’s documents is about $0.15)">
          <input type="number" min={10} max={2000} value={f.max_pairs} onChange={(e) => setF({ ...f, max_pairs: Number(e.target.value) })} /></Field>
        <Field label="Passages to compare each one with (1–10)" hint="More catches conflicts worded differently; each extra one adds pairs">
          <input type="number" min={1} max={10} value={f.neighbors} onChange={(e) => setF({ ...f, neighbors: Number(e.target.value) })} /></Field>
      </div>
      <p className="small muted">The models and wording are the “consistency” and “consistency_verify” steps in <Link to="/admin/models">Models</Link> and <Link to="/admin/prompts">Prompts</Link>.</p>
      <h3>What changed</h3>
      <label className="check"><input type="checkbox" checked={f.track_changes} onChange={(e) => setF({ ...f, track_changes: e.target.checked })} /> List what changed when a re-crawl updates a document</label>
      <label className="check"><input type="checkbox" checked={f.summarize_changes} disabled={!f.track_changes} onChange={(e) => setF({ ...f, summarize_changes: e.target.checked })} /> Summarize the changed facts (otherwise just the exact lines)</label>
      <Field label="Most summaries per crawl" hint="Caps the cost of a crawl that changes many documents (about $0.001 per document); the rest still list their exact changes">
        <input type="number" min={1} max={200} value={f.max_changed_docs} onChange={(e) => setF({ ...f, max_changed_docs: Number(e.target.value) })} /></Field>
      <p className="small muted">The model and wording for summaries are the “change_summary” step.</p>
      <button className="btn small" disabled={busy} onClick={() => run(async () => { await api.saveChecksSettings(f); reload(); })}>{busy ? "Saving…" : "Save settings"}</button>
    </details>
  );
}
