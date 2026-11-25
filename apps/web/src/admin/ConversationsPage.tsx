import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, type RatingFilter, type TurnBrief } from "./api";
import { ago, Empty, ErrorBox, fmt, useLoad } from "./ui";

const FILTERS: { v: RatingFilter; label: string }[] = [
  { v: "all", label: "All" }, { v: "down", label: "👎 Thumbs down" }, { v: "up", label: "👍 Thumbs up" }, { v: "unrated", label: "Unrated" },
];

const Thumb = ({ r }: { r: 1 | -1 | null }) => r === 1 ? <span title="Thumbs up">👍</span> : r === -1 ? <span title="Thumbs down">👎</span> : <span className="muted">·</span>;
const secs = (ms: number | null) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)}s`);
const who = (t: Pick<TurnBrief, "user_role" | "user_email">) => t.user_email || t.user_role || "public";

export function ConversationsPage() {
  const [params, setParams] = useSearchParams();
  const rating = (params.get("rating") as RatingFilter) || "all";
  const [q, setQ] = useState(params.get("q") ?? "");
  const { data, error } = useLoad(() => api.interactions({ rating, q: params.get("q") ?? undefined }), [rating, params.get("q")]);
  const set = (k: string, v: string) => { const n = new URLSearchParams(params); if (v) n.set(k, v); else n.delete(k); setParams(n); };
  const t = data?.totals_30d;
  return (
    <>
      <header className="page-head">
        <div><h1>Conversations</h1><p className="sub">Every answer Cypress gave, with ratings. Review the 👎 ones to fix content, prompts or routing.</p></div>
      </header>
      {t && <p className="stats">Last 30 days: <b>{fmt(t.turns)}</b> turns · 👍 <b>{t.up}</b> · 👎 <b>{t.down}</b> · {t.failed} errors · median {secs(t.p50_ms)}</p>}
      <div className="toolbar">
        <div className="tabs">{FILTERS.map((f) => (
          <button key={f.v} className={`tab${rating === f.v ? " on" : ""}`} onClick={() => set("rating", f.v === "all" ? "" : f.v)}>{f.label}</button>
        ))}</div>
        <form onSubmit={(e) => { e.preventDefault(); set("q", q.trim()); }}>
          <input type="search" placeholder="Search questions, answers, comments" value={q} onChange={(e) => setQ(e.target.value)} />
        </form>
      </div>
      <ErrorBox error={error} />
      {data && !data.items.length && <Empty title="Nothing here yet">Turns appear as people chat on the web and in Slack.</Empty>}
      {data && data.items.length > 0 && (
        <div className="tbl">
          <table className="stack-sm">
            <thead><tr><th></th><th>When</th><th>Question</th><th>Answer</th><th>Who</th><th>Intent</th><th className="num">Time</th></tr></thead>
            <tbody>{data.items.map((r) => (
              <tr key={r.id} className={r.failed ? "row-err" : ""}>
                <td><Thumb r={r.rating} /></td>
                <td className="small" title={r.at}>{ago(r.at)}<div className="muted">{r.channel}</div></td>
                <td><Link to={`/admin/conversations/${r.id}`}>{r.question}</Link>
                  {r.feedback_comment && <div className="small fb-note">“{r.feedback_comment}”</div>}</td>
                <td className="small muted clip">{r.answer_preview}</td>
                <td className="small">{who(r)}</td>
                <td><span className="chip">{r.intent ?? "—"}</span></td>
                <td className="num small">{secs(r.latency_ms)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}

export function ConversationDetail() {
  const id = Number(useParams().id);
  const { data: t, error } = useLoad(() => api.interaction(id), [id]);
  if (error) return <ErrorBox error={error} />;
  if (!t) return <p className="muted">Loading…</p>;
  const r = t.retrieval ?? {};
  return (
    <>
      <nav className="crumbs"><Link to="/admin/conversations">Conversations</Link> / <span>Turn #{t.id}</span></nav>
      <header className="page-head">
        <div>
          <h1 className="q">{t.question}</h1>
          <p className="stats small">{new Date(t.at).toLocaleString()} · {t.channel} · {who(t)} · intent <b>{t.intent ?? "—"}</b> · {secs(t.latency_ms)}</p>
        </div>
        <div className="big-thumb"><Thumb r={t.rating} /></div>
      </header>
      {(t.rating || t.feedback_comment) && (
        <div className={`panel ${t.rating === -1 ? "warn" : ""}`}>
          <b>Feedback</b> <Thumb r={t.rating} /> {t.feedback_comment ? <>“{t.feedback_comment}”</> : <span className="muted">no comment</span>}
          <div className="small muted">{t.feedback_by} · {ago(t.feedback_at)}</div>
        </div>
      )}
      {t.error && <ErrorBox error={t.error} />}
      <div className="doc-layout">
        <div>
          <h3>Answer</h3>
          <pre className="markdown">{t.answer}</pre>
          {t.citations.length > 0 && <><h3>Cited</h3><ol className="plain">{t.citations.map((c) => (
            <li key={c.n}>[{c.n}] <a href={c.uri} target="_blank" rel="noreferrer">{c.title}</a> <span className="small muted">{c.set}</span></li>))}</ol></>}
          {t.thread.length > 1 && <><h3>Conversation</h3><ol className="plain thread">{t.thread.map((x) => (
            <li key={x.id} className={x.id === t.id ? "here" : ""}>
              <Link to={`/admin/conversations/${x.id}`}><b>{x.question}</b></Link> <Thumb r={x.rating} />
              <div className="small muted clip">{x.answer}</div>
            </li>))}</ol></>}
        </div>
        <aside className="panel slim">
          <h3>How it answered</h3>
          {r.queries && r.queries.length > 0 && <><h4>Searches</h4><ol className="plain small">{r.queries.map((q, i) => <li key={i}>{q}</li>)}</ol></>}
          {r.grade && "sufficient" in r.grade && <p className="small">Grader: {r.grade.sufficient ? "passages answered it" : "passages fell short"}</p>}
          {r.passages && r.passages.length > 0 && <><h4>Passages retrieved</h4><ol className="plain small">{r.passages.map((p, i) => (
            <li key={i}>{p.title}{p.section && p.section !== p.title ? ` — ${p.section}` : ""} <span className="muted mono">{p.set_slug} {p.score?.toFixed?.(3)}</span></li>))}</ol></>}
          {r.tool_calls && r.tool_calls.length > 0 && <><h4>Tool calls</h4><ul className="plain small">{r.tool_calls.map((c, i) => (
            <li key={i}><span className="mono">{c.tool}</span> {c.ok ? "✓" : "✗"} <span className="muted mono break">{JSON.stringify(c.args)}</span></li>))}</ul></>}
          <h4>Models</h4><ul className="plain small">{Object.entries(t.models).map(([n, m]) => <li key={n}>{n}: <span className="mono">{m}</span></li>)}</ul>
          {t.trace_url && <p className="small"><a href={t.trace_url} target="_blank" rel="noreferrer">Open the full trace in Google Cloud →</a><br />
            <span className="muted">Every step, model call and tool call with timings. Needs access to the GCP project.</span></p>}
        </aside>
      </div>
    </>
  );
}
