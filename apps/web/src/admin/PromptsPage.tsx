import { useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, AUDIENCES, type AgentConfig, type Audience, type PromptInfo, type PromptsData, type TryResult } from "./api";
import { ago, Empty, ErrorBox, Field, useAction, useLoad } from "./ui";
import { Rich } from "../chat/ChatPage";

const TITLES: Record<string, string> = { about: "About Cypress", route: "Understand the message", grade: "Check search results",
  answer: "Write the answer", act: "Use tools", route_ticket: "Suggest ticket owners", ocr: "Read scanned pages" };
const VAR_RE = /\{\{\s*([a-z_]+)\s*\}\}/g;

export default function PromptsPage() {
  const { data, error, reload } = useLoad(api.prompts);
  const [override, setOverride] = useState<PromptsData | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const d = override ?? data;
  if (error) return <ErrorBox error={error} />;
  if (!d) return <p className="muted">Loading…</p>;
  const p = d.prompts.find((x) => x.key === open);
  return (
    <>
      <header className="page-head"><div><h1>Prompts</h1>
        <p className="sub">The instructions behind each step of Cypress. Edit the wording, try it on a real question, then save. Every save is a new version you can restore, and a prompt can differ for residents and the team. A short safety block is always added and can't be edited.</p></div></header>
      {p ? <Editor p={p} vars={d.variables} onSaved={(n) => setOverride(n)} onClose={() => { setOpen(null); setOverride(null); reload(); }} />
        : <div className="prompt-list">{d.prompts.map((x) => <PromptCard key={x.key} p={x} onOpen={() => setOpen(x.key)} />)}</div>}
    </>
  );
}

function PromptCard({ p, onOpen }: { p: PromptInfo; onOpen: () => void }) {
  const status = AUDIENCES.map((a) => ({ a, c: p.configs[a.value] })).filter(({ c }) => c?.prompt);
  return (
    <button className="prompt-card" onClick={onOpen}>
      <div className="row-end"><b>{TITLES[p.key] ?? p.key}</b><span className="spacer" /><span className="mono small muted">{p.key}</span></div>
      <div className="small muted">{p.description}</div>
      <div className="small">{status.length ? status.map(({ a, c }) => <span key={a.value} className="chip st-running">{a.label}: custom v{c!.version}</span>)
        : <span className="chip muted">built-in</span>}</div>
    </button>
  );
}

function Editor({ p, vars, onSaved, onClose }: { p: PromptInfo; vars: Record<string, string>; onSaved: (d: PromptsData) => void; onClose: () => void }) {
  const [aud, setAud] = useState<Audience>("all");
  const [text, setText] = useState(p.effective.all);
  const [note, setNote] = useState("");
  const [view, setView] = useState<"edit" | "history" | "try">("edit");
  const { busy, error, run } = useAction();
  const [saved, setSaved] = useState<string | null>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  const nav = useNavigate();
  const live = p.configs[aud];
  const current = p.effective[aud];
  const dirty = text !== current;
  const unknown = useMemo(() => [...new Set([...text.matchAll(VAR_RE)].map((m) => m[1]))].filter((v) => !p.variables.includes(v)), [text, p.variables]);
  const pick = (a: Audience) => { setAud(a); setText(p.effective[a]); setSaved(null); };
  const insert = (v: string) => {
    const el = area.current;
    if (!el) return;
    const s = el.selectionStart, e = el.selectionEnd, tok = `{{${v}}}`;
    setText(text.slice(0, s) + tok + text.slice(e));
    requestAnimationFrame(() => { el.focus(); el.setSelectionRange(s + tok.length, s + tok.length); });
  };
  const save = (value: string | null) => run(async () => {
    const r = await api.savePrompt(p.key, aud, value, note || null);
    onSaved(r); setNote(""); setSaved(`Saved as version ${r.saved.version}.`);
    const np = r.prompts.find((x) => x.key === p.key)!;
    setText(np.effective[aud]);
  });
  const inherited = aud !== "all" && !live?.prompt;
  return (
    <section className="panel">
      <div className="row-end wrap"><h3 className="h3">{TITLES[p.key] ?? p.key} <span className="mono small muted">{p.key}</span></h3><span className="spacer" />
        <button className="btn ghost small" onClick={onClose}>All prompts</button></div>
      <p className="small muted">{p.description}</p>
      {p.key !== "about" && p.key !== "route_ticket" && p.key !== "ocr" && <div className="seg" role="group" aria-label="Audience">
        {AUDIENCES.map((a) => <button key={a.value} className={aud === a.value ? "on" : ""} aria-pressed={aud === a.value} onClick={() => pick(a.value)} title={a.who}>{a.label}</button>)}
      </div>}
      <div className="tabs" role="tablist">
        {([["edit", "Edit"], ["try", "Try it"], ["history", "History"]] as const).map(([k, l]) => (
          <button key={k} role="tab" aria-selected={view === k} className={view === k ? "on" : ""} onClick={() => setView(k)}>{l}</button>))}
      </div>
      <ErrorBox error={error} />
      {view === "edit" && <>
        <p className="small muted">{live ? `Live: version ${live.version}${live.prompt ? "" : " (built-in prompt)"}, saved by ${live.created_by} ${ago(live.created_at)}.` : "Using the built-in prompt."}
          {inherited && " This audience uses the Everyone prompt until you save one for it."}</p>
        {p.variables.length > 0 && <div className="var-chips"><span className="small muted">Insert:</span>
          {p.variables.map((v) => <button key={v} type="button" className="chip linkish" title={vars[v]} onClick={() => insert(v)}>{`{{${v}}}`}</button>)}</div>}
        <textarea ref={area} className="prompt-text mono" value={text} onChange={(e) => { setText(e.target.value); setSaved(null); }} spellCheck aria-label="Prompt text" />
        {unknown.length > 0 && <p className="small err-text">Unknown variable{unknown.length > 1 ? "s" : ""}: {unknown.map((u) => `{{${u}}}`).join(", ")}</p>}
        {p.guardrail && <details className="guard"><summary className="small">Always added (can't be edited)</summary><pre className="pre small">{p.guardrail}</pre></details>}
        <Field label="Change note" hint="Why you're changing it; shows in the history.">
          <input value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Friendlier tone for residents" /></Field>
        <div className="actions wrap">
          <button className="btn primary" disabled={busy || !dirty || unknown.length > 0} onClick={() => save(text)}>{busy ? "Saving…" : "Save new version"}</button>
          {p.tryable && <button className="btn" disabled={busy} onClick={() => setView("try")}>Try it first</button>}
          {p.tryable && <button className="btn" disabled={busy || unknown.length > 0} title="Run the whole test set with this draft (not saved)"
            onClick={() => run(async () => { const r = await api.startEval({ label: `Draft ${p.key} prompt${note ? `: ${note}` : ""}`, prompts: { [p.key]: text } }); nav(`/admin/evals/${r.id}`); })}>Run evals</button>}
          {dirty && <button className="btn ghost" onClick={() => setText(current)}>Discard changes</button>}
          <span className="spacer" />
          {live?.prompt && <button className="btn ghost small" disabled={busy} onClick={() => save(null)}>Reset to built-in</button>}
        </div>
        {saved && <p className="ok-text small" role="status">{saved} It applies within 30 seconds.</p>}
      </>}
      {view === "try" && (p.tryable ? <TryIt p={p} text={text} dirty={dirty} /> : <Empty title="Try it isn't available for this prompt">It runs outside the chat (tickets or document ingest).</Empty>)}
      {view === "history" && <PromptHistory p={p} aud={aud} onRestore={(t) => { setText(t); setView("edit"); }} />}
    </section>
  );
}

function TryIt({ p, text, dirty }: { p: PromptInfo; text: string; dirty: boolean }) {
  const [q, setQ] = useState(p.key === "act" ? "Is 3106 Edgewater Dr inside the deeded access boundary?" : "What are the pool hours?");
  const [as, setAs] = useState("public");
  const [r, setR] = useState<TryResult | null>(null);
  const { busy, error, run } = useAction();
  return (
    <>
      <p className="small muted">Runs one real question through Cypress with {dirty ? "your unsaved draft" : "this prompt"} (nothing is saved). It's a normal paid call, usually under a cent.</p>
      <ErrorBox error={error} />
      <div className="grid2">
        <Field label="Question"><input value={q} onChange={(e) => setQ(e.target.value)} /></Field>
        <Field label="Ask as"><select value={as} onChange={(e) => setAs(e.target.value)}>
          <option value="public">A resident (not signed in)</option><option value="member">A signed-in member</option>
          <option value="staff">Staff</option><option value="security">Security</option><option value="board">Board</option></select></Field>
      </div>
      <button className="btn primary" disabled={busy || q.trim().length < 2} onClick={() => run(async () => setR(await api.tryPrompt(p.key, text, q, as)))}>{busy ? "Asking…" : "Ask"}</button>
      {r && <div className="try-result">
        <div className="bubble"><Rich text={r.answer} citations={r.citations.map((c) => ({ ...c, section: "", set: "" }))} /></div>
        {r.citations.length > 0 && <ol className="sources small">{r.citations.map((c) => <li key={c.n} value={c.n}><a href={c.uri} target="_blank" rel="noreferrer">{c.title}</a></li>)}</ol>}
        <p className="small muted">Intent: {r.intent ?? "—"} · {r.calls} model calls · ${r.cost.toFixed(4)}{r.steps ? ` · ${r.steps}` : ""}</p>
      </div>}
    </>
  );
}

function PromptHistory({ p, aud, onRestore }: { p: PromptInfo; aud: Audience; onRestore: (text: string) => void }) {
  const { data, error } = useLoad(() => api.configHistory(p.key, aud), [p.key, aud]);
  const [show, setShow] = useState<number | null>(null);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p className="muted">Loading…</p>;
  if (!data.versions.length) return <Empty title="No saved versions">This audience uses the built-in prompt.</Empty>;
  const vs = data.versions;
  return (
    <ol className="plain versions">{vs.map((v: AgentConfig, i) => {
      const prev = vs[i + 1];
      const text = v.prompt ?? p.builtin, before = prev ? prev.prompt ?? p.builtin : p.builtin;
      return (
        <li key={v.id}>
          <div className="row-end wrap"><b>v{v.version}</b>{i === 0 && <span className="chip st-succeeded">live</span>}
            <span className="small muted">{v.created_by} · {ago(v.created_at)}{v.prompt ? "" : " · built-in prompt"}</span><span className="spacer" />
            <button className="linkish small" onClick={() => setShow(show === v.id ? null : v.id)}>{show === v.id ? "Hide changes" : "Show changes"}</button>
            {i > 0 && <button className="btn ghost small" onClick={() => onRestore(text)}>Load into editor</button>}</div>
          {v.note && <div className="small">{v.note}</div>}
          {show === v.id && <Diff a={before} b={text} />}
        </li>);
    })}</ol>
  );
}

/** Line diff (LCS); prompts are short, so the quadratic table is fine. */
function Diff({ a, b }: { a: string; b: string }) {
  const rows = useMemo(() => {
    const x = a.split("\n"), y = b.split("\n");
    const t = Array.from({ length: x.length + 1 }, () => new Array<number>(y.length + 1).fill(0));
    for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--)
      t[i][j] = x[i] === y[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]);
    const out: { k: " " | "-" | "+"; s: string }[] = [];
    let i = 0, j = 0;
    while (i < x.length && j < y.length) {
      if (x[i] === y[j]) { out.push({ k: " ", s: x[i] }); i++; j++; }
      else if (t[i + 1][j] >= t[i][j + 1]) out.push({ k: "-", s: x[i++] });
      else out.push({ k: "+", s: y[j++] });
    }
    while (i < x.length) out.push({ k: "-", s: x[i++] });
    while (j < y.length) out.push({ k: "+", s: y[j++] });
    return out;
  }, [a, b]);
  if (rows.every((r) => r.k === " ")) return <p className="small muted">No wording changes (model or settings only).</p>;
  return <pre className="diff small">{rows.map((r, i) => <div key={i} className={r.k === "+" ? "add" : r.k === "-" ? "del" : "same"}>{r.k} {r.s || " "}</div>)}</pre>;
}
