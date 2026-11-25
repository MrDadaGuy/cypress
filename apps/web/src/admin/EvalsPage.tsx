import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type EvalCase, type EvalCaseIn, type EvalResult, type EvalRun, type EvalRunSummary, type ModelsData } from "./api";
import { ago, Empty, ErrorBox, Field, useAction, useLoad, usePoll } from "./ui";
import { SubagentRun, SubagentsTab } from "./SubagentEvals";

type Tab = "runs" | "cases" | "sub";
const ROLES: [string, string][] = [["public", "Resident (not signed in)"], ["member", "Member"], ["staff", "Staff"], ["security", "Security"], ["board", "Board"]];
const usd = (n: number | null | undefined) => (n == null ? "—" : `$${n < 0.1 ? n.toFixed(4) : n.toFixed(2)}`);
const secs = (ms: number | null | undefined) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)}s`);
const pct = (a: number, b: number) => (b ? Math.round((a / b) * 100) : 0);

export default function EvalsPage() {
  const { runId, subId } = useParams();
  const { data, error, reload } = useLoad(api.evals);
  const [tab, setTab] = useState<Tab>(() => { try { return (sessionStorage.getItem("evals-tab") as Tab) || "runs"; } catch { return "runs"; } });
  const pick = (t: Tab) => { setTab(t); try { sessionStorage.setItem("evals-tab", t); } catch { /* private mode */ } };
  const running = !!data?.runs.some((r) => r.status === "running");
  usePoll(running && !runId, reload, 4000);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p className="muted">Loading…</p>;
  if (subId) return <SubagentRun id={Number(subId)} />;
  if (runId) return <RunDetail id={Number(runId)} runs={data.runs} />;
  return (
    <>
      <header className="page-head"><div><h1>Evals</h1>
        <p className="sub">A test set of real questions with what a good answer needs. Run it before changing a prompt or model to see whether answers get better or worse, and what they cost. Runs use real models (about $0.10 each) but never file tickets or send anything.</p></div></header>
      {data.seeded > 0 && <div className="panel slim">Added a starter set of {data.seeded} test questions built from the LHCA website. Edit them under Test questions.</div>}
      <div className="tabs" role="tablist">
        {([["runs", `Runs (${data.runs.length})`], ["cases", `Test questions (${data.cases.filter((c) => c.active).length})`], ["sub", "Case-file sub-agents"]] as [Tab, string][]).map(([t, l]) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => pick(t)}>{l}</button>))}
      </div>
      {tab === "runs" ? <RunsTab runs={data.runs} tags={data.tags} reload={reload} /> : tab === "cases" ? <CasesTab cases={data.cases} reload={reload} /> : <SubagentsTab />}
    </>
  );
}

function RunsTab({ runs, tags, reload }: { runs: EvalRunSummary[]; tags: string[]; reload: () => void }) {
  const [label, setLabel] = useState("");
  const [tag, setTag] = useState("");
  const [models, setModels] = useState<ModelsData | null>(null);
  const [over, setOver] = useState<Record<string, string>>({});
  const { busy, error, run } = useAction();
  const nav = useNavigate();
  useEffect(() => { api.models().then(setModels, () => undefined); }, []);
  const start = () => run(async () => {
    const auto = Object.entries(over).filter(([, v]) => v).map(([n, m]) => `${n}: ${models?.models.find((x) => x.id === m)?.label ?? m}`).join(", ");
    const r = await api.startEval({ label: label || auto || "Current setup", models: Object.fromEntries(Object.entries(over).filter(([, v]) => v)), tag: tag || null });
    reload(); nav(`/admin/evals/${r.id}`);
  });
  return (
    <>
      <section className="panel">
        <h3 className="h3">New run</h3>
        <ErrorBox error={error} />
        <div className="grid2">
          <Field label="Label"><input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Baseline before tone change" /></Field>
          <Field label="Questions"><select value={tag} onChange={(e) => setTag(e.target.value)}>
            <option value="">All active questions</option>{tags.map((t) => <option key={t} value={t}>Tagged “{t}”</option>)}</select></Field>
        </div>
        {models && <details><summary className="small">Try different models (optional)</summary>
          <p className="small muted">Leave a step on “Current” to use its live setting. To try a draft prompt, use “Run evals” in the prompt editor.</p>
          <div className="grid2">{models.nodes.filter((n) => !["ocr", "eval_judge"].includes(n.node)).map((n) => (
            <Field key={n.node} label={n.node}>
              <select value={over[n.node] ?? ""} onChange={(e) => setOver({ ...over, [n.node]: e.target.value })}>
                <option value="">Current ({models.models.find((m) => m.id === n.effective.all)?.label})</option>
                {models.models.filter((m) => m.kind === "chat" && m.enabled && (!n.gemini_only || m.provider === "gemini")).map((m) =>
                  <option key={m.id} value={m.id}>{m.label}</option>)}
              </select></Field>))}</div></details>}
        <button className="btn primary" disabled={busy} onClick={start}>{busy ? "Starting…" : "Run evals"}</button>
      </section>
      {runs.length === 0 ? <Empty title="No runs yet">Start with a baseline of the current setup.</Empty> : (
        <div className="tbl"><table className="stack-sm">
          <thead><tr><th>Run</th><th>Passed</th><th className="num">Score</th><th className="num">Cost</th><th className="num">Speed (p50)</th></tr></thead>
          <tbody>{runs.map((r) => (
            <tr key={r.id}>
              <td data-label="Run"><Link to={`/admin/evals/${r.id}`} className="strong">{r.label}</Link>
                <div className="small muted">{r.started_by} · {ago(r.started_at)}{overrideText(r)}</div></td>
              <td data-label="Passed">{r.status === "running"
                ? <><div className="meter"><span style={{ width: `${pct(r.finished, r.cases)}%` }} /></div><span className="small muted">{r.finished} of {r.cases} done</span></>
                : <><b>{r.passed}</b> / {r.cases} <span className="small muted">({pct(r.passed, r.cases)}%)</span>{r.status === "cancelled" && <span className="chip muted">cancelled</span>}</>}</td>
              <td data-label="Score" className="num">{r.avg_score ?? "—"}</td>
              <td data-label="Cost" className="num">{usd(r.cost)}</td>
              <td data-label="Speed" className="num">{secs(r.p50_ms)}</td>
            </tr>))}</tbody>
        </table></div>)}
    </>
  );
}

function overrideText(r: EvalRunSummary) {
  const p = Object.keys(r.overrides?.prompts ?? {}), m = Object.entries(r.overrides?.models ?? {});
  if (!p.length && !m.length) return " · live setup";
  return ` · ${[...p.map((k) => `draft ${k} prompt`), ...m.map(([n, id]) => `${n} on ${id}`)].join(", ")}`;
}

function RunDetail({ id, runs }: { id: number; runs: EvalRunSummary[] }) {
  const { data: r, error, reload } = useLoad(() => api.evalRun(id), [id]);
  usePoll(r?.status === "running", reload, 3000);
  const [cmpId, setCmpId] = useState<number | "">("");
  const [cmp, setCmp] = useState<EvalRun | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [onlyFail, setOnlyFail] = useState(false);
  const { run, busy } = useAction();
  useEffect(() => { if (cmpId) api.evalRun(cmpId).then(setCmp); else setCmp(null); }, [cmpId]);
  if (error) return <ErrorBox error={error} />;
  if (!r) return <p className="muted">Loading…</p>;
  const before = new Map((cmp?.results ?? []).map((x) => [x.case_id, x]));
  const changed = cmp ? r.results.filter((x) => before.has(x.case_id) && before.get(x.case_id)!.passed !== x.passed && x.status !== "pending") : [];
  const shown = r.results.filter((x) => !onlyFail || x.passed === false);
  return (
    <>
      <header className="page-head"><div><p className="crumbs"><Link to="/admin/evals">Evals</Link></p><h1>{r.label}</h1>
        <p className="sub">{r.started_by} · {ago(r.started_at)}{overrideText(r)}</p></div></header>
      <div className="kpis">
        <div className="kpi"><div className="kpi-l">Passed</div><div className="kpi-v">{r.passed} / {r.cases}</div>
          <div className="kpi-s">{r.status === "running" ? `${r.finished} done, running…` : `${pct(r.passed, r.cases)}%`}{cmp ? ` (was ${cmp.passed})` : ""}</div></div>
        <div className="kpi"><div className="kpi-l">Judge score</div><div className="kpi-v">{r.avg_score ?? "—"}</div><div className="kpi-s">out of 5{cmp?.avg_score ? ` (was ${cmp.avg_score})` : ""}</div></div>
        <div className="kpi"><div className="kpi-l">Cost of run</div><div className="kpi-v">{usd(r.cost)}</div><div className="kpi-s">{cmp ? `was ${usd(cmp.cost)}` : `${usd(r.cases ? r.cost / Math.max(r.finished, 1) : 0)} per question`}</div></div>
        <div className="kpi"><div className="kpi-l">Speed (p50)</div><div className="kpi-v">{secs(r.p50_ms)}</div><div className="kpi-s">{cmp ? `was ${secs(cmp.p50_ms)}` : "per question, incl. judging"}</div></div>
      </div>
      <div className="toolbar">
        <Field label="Compare with"><select value={cmpId} onChange={(e) => setCmpId(e.target.value ? Number(e.target.value) : "")}>
          <option value="">(none)</option>{runs.filter((x) => x.id !== id).map((x) => <option key={x.id} value={x.id}>{x.label} · {ago(x.started_at)}</option>)}</select></Field>
        <label className="check"><input type="checkbox" checked={onlyFail} onChange={(e) => setOnlyFail(e.target.checked)} /> Only failures</label>
        {r.status === "running" && <button className="btn ghost small" disabled={busy} onClick={() => run(async () => { await api.cancelEval(id); reload(); })}>Cancel run</button>}
      </div>
      {cmp && <div className={`panel slim ${changed.some((x) => !x.passed) ? "warn" : ""}`}>
        {changed.length === 0 ? "Same pass/fail results as the compared run." : <>
          {changed.filter((x) => x.passed).length > 0 && <div><b className="ok-text">Now passing:</b> {changed.filter((x) => x.passed).map((x) => x.question).join(" · ")}</div>}
          {changed.filter((x) => !x.passed).length > 0 && <div><b className="crit-text">Now failing:</b> {changed.filter((x) => !x.passed).map((x) => x.question).join(" · ")}</div>}</>}
      </div>}
      <div className="tbl"><table className="stack-sm">
        <thead><tr><th>Question</th><th>Result</th><th className="num">Score</th><th className="num">Cost</th><th className="num">Time</th></tr></thead>
        <tbody>{shown.map((x) => <ResultRow key={x.case_id} x={x} prev={before.get(x.case_id)} open={open === x.case_id} toggle={() => setOpen(open === x.case_id ? null : x.case_id)} />)}</tbody>
      </table></div>
    </>
  );
}

function ResultRow({ x, prev, open, toggle }: { x: EvalResult; prev?: EvalResult; open: boolean; toggle: () => void }) {
  const failed = Object.entries(x.checks ?? {}).filter(([, c]) => !c.ok);
  return (
    <>
      <tr className="clickable" onClick={toggle}>
        <td data-label="Question"><button className="linkish strong" aria-expanded={open}>{x.question}</button>
          <div className="small muted">{ROLES.find((r) => r[0] === x.as_role)?.[1]}{x.tags.length ? ` · ${x.tags.join(", ")}` : ""}</div></td>
        <td data-label="Result">{x.status === "pending" ? <span className="chip muted">running</span>
          : x.passed ? <span className="chip st-succeeded">pass</span> : <span className="chip st-failed">{x.status === "error" ? "error" : "fail"}</span>}
          {prev && prev.passed !== x.passed && x.status !== "pending" && <span className="small muted"> (was {prev.passed ? "pass" : "fail"})</span>}
          {failed.length > 0 && <div className="small crit-text">{failed.map(([k]) => k).join(", ")}</div>}</td>
        <td data-label="Score" className="num">{x.judge?.score ?? "—"}</td>
        <td data-label="Cost" className="num">{usd(x.cost_usd)}</td>
        <td data-label="Time" className="num">{secs(x.latency_ms)}</td>
      </tr>
      {open && <tr className="detail"><td colSpan={5}>
        <div className="eval-detail">
          {x.error && <div className="error">{x.error}</div>}
          <div className="bubble">{(x.answer ?? "").split("\n").filter(Boolean).map((l, i) => <p key={i}>{l}</p>)}</div>
          {x.citations.length > 0 && <p className="small muted">Cited: {x.citations.map((c) => c.title).join(", ")}</p>}
          <p className="small muted">Intent: {x.intent ?? "—"}{x.tools.length ? ` · tools: ${x.tools.join(", ")}` : ""}</p>
          <ul className="plain small">{Object.entries(x.checks ?? {}).map(([k, c]) => <li key={k}><span className={c.ok ? "ok-text" : "crit-text"}>{c.ok ? "✓" : "✗"} {k}</span> <span className="muted">{c.detail}</span></li>)}</ul>
          {x.judge && <p className="small"><b>Judge ({x.judge.score}/5):</b> {x.judge.reason}</p>}
          {x.expect.ideal && <p className="small muted"><b>A good answer:</b> {x.expect.ideal}</p>}
          {prev && prev.answer !== x.answer && <details><summary className="small">Answer in the compared run</summary><div className="bubble muted">{prev.answer}</div></details>}
        </div></td></tr>}
    </>
  );
}

// ---------------- test questions ----------------

const blank: EvalCaseIn = { question: "", as_role: "public", expect: {}, tags: [], notes: null, active: true };
const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

function CasesTab({ cases, reload }: { cases: EvalCase[]; reload: () => void }) {
  const [edit, setEdit] = useState<{ id: number | null; c: EvalCaseIn } | null>(null);
  const [q, setQ] = useState("");
  if (edit) return <CaseForm edit={edit} onDone={() => { setEdit(null); reload(); }} onCancel={() => setEdit(null)} />;
  const shown = cases.filter((c) => !q || `${c.question} ${c.tags.join(" ")}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <div className="toolbar">
        <input type="search" placeholder="Search questions and tags" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search test questions" />
        <button className="btn primary" onClick={() => setEdit({ id: null, c: blank })}>Add question</button>
      </div>
      <div className="tbl"><table className="stack-sm">
        <thead><tr><th>Question</th><th>Asked as</th><th>Checks</th><th>Last run</th><th /></tr></thead>
        <tbody>{shown.map((c) => (
          <tr key={c.id} className={c.active ? "" : "disabled"}>
            <td data-label="Question"><div className="strong">{c.question}</div><div className="small muted">{c.tags.join(", ")}</div></td>
            <td data-label="Asked as" className="small">{ROLES.find((r) => r[0] === c.as_role)?.[1]}</td>
            <td data-label="Checks" className="small">{describe(c)}</td>
            <td data-label="Last run">{c.last_passed == null ? <span className="small muted">—</span> : c.last_passed ? <span className="chip st-succeeded">pass</span> : <span className="chip st-failed">fail</span>}</td>
            <td className="row-actions"><button className="btn ghost small" onClick={() => setEdit({ id: c.id, c: { question: c.question, as_role: c.as_role, expect: c.expect, tags: c.tags, notes: c.notes, active: c.active } })}>Edit</button></td>
          </tr>))}</tbody>
      </table></div>
    </>
  );
}

function describe(c: EvalCase) {
  const e = c.expect, bits = [];
  if (e.intent) bits.push(`intent ${e.intent}`);
  if (e.must_include?.length) bits.push(`${e.must_include.length} must-include`);
  if (e.must_not_include?.length) bits.push("must-not-include");
  if (e.cite?.length) bits.push(`cites ${e.cite.join("/")}`);
  if (e.tool) bits.push(`uses ${e.tool}`);
  if (e.ideal) bits.push("judge guide");
  return bits.join(" · ") || "judge only";
}

function CaseForm({ edit, onDone, onCancel }: { edit: { id: number | null; c: EvalCaseIn }; onDone: () => void; onCancel: () => void }) {
  const e0 = edit.c.expect;
  const [f, setF] = useState({ question: edit.c.question, as_role: edit.c.as_role, intent: e0.intent ?? "", tool: e0.tool ?? "",
    must: (e0.must_include ?? []).map((g) => g.join(", ")).join("\n"), mustNot: (e0.must_not_include ?? []).join(", "),
    cite: (e0.cite ?? []).join(", "), ideal: e0.ideal ?? "", tags: edit.c.tags.join(", "), notes: edit.c.notes ?? "", active: edit.c.active });
  const { busy, error, run } = useAction();
  const body = (): EvalCaseIn => ({ question: f.question, as_role: f.as_role, tags: list(f.tags), notes: f.notes || null, active: f.active,
    expect: { intent: f.intent || undefined, tool: f.tool || undefined, ideal: f.ideal || undefined, cite: list(f.cite), must_not_include: list(f.mustNot),
      must_include: f.must.split("\n").map(list).filter((g) => g.length) } });
  const save = () => run(async () => { if (edit.id) await api.saveEvalCase(edit.id, body()); else await api.addEvalCase(body()); onDone(); });
  return (
    <section className="panel">
      <h3 className="h3">{edit.id ? "Edit test question" : "Add a test question"}</h3>
      <ErrorBox error={error} />
      <Field label="Question"><input value={f.question} onChange={(e) => setF({ ...f, question: e.target.value })} /></Field>
      <div className="grid2">
        <Field label="Asked as"><select value={f.as_role} onChange={(e) => setF({ ...f, as_role: e.target.value })}>{ROLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></Field>
        <Field label="Tags" hint="Comma separated, e.g. pool, facts"><input value={f.tags} onChange={(e) => setF({ ...f, tags: e.target.value })} /></Field>
        <Field label="Expected intent (optional)"><select value={f.intent} onChange={(e) => setF({ ...f, intent: e.target.value })}>
          <option value="">(any)</option>{["question", "greeting", "off_topic", "report_issue", "booking", "deeded_access", "tickets", "board_contact", "member_lookup"].map((i) => <option key={i}>{i}</option>)}</select></Field>
        <Field label="Expected tool (optional)"><select value={f.tool} onChange={(e) => setF({ ...f, tool: e.target.value })}>
          <option value="">(none)</option>{["check_deeded_access", "report_issue", "list_tickets", "update_ticket"].map((t) => <option key={t}>{t}</option>)}</select></Field>
      </div>
      <Field label="Answer must include" hint="One line per requirement; separate alternatives with commas (any one counts). E.g. “$250, 250”">
        <textarea rows={3} value={f.must} onChange={(e) => setF({ ...f, must: e.target.value })} /></Field>
      <div className="grid2">
        <Field label="Must not include" hint="Comma separated"><input value={f.mustNot} onChange={(e) => setF({ ...f, mustNot: e.target.value })} /></Field>
        <Field label="Should cite a source like" hint="Comma separated title fragments, any one"><input value={f.cite} onChange={(e) => setF({ ...f, cite: e.target.value })} /></Field>
      </div>
      <Field label="What a good answer looks like (for the judge)"><textarea rows={2} value={f.ideal} onChange={(e) => setF({ ...f, ideal: e.target.value })} /></Field>
      <label className="check"><input type="checkbox" checked={f.active} onChange={(e) => setF({ ...f, active: e.target.checked })} /> Include in runs</label>
      <div className="actions">
        <button className="btn primary" disabled={busy || f.question.trim().length < 2} onClick={save}>{busy ? "Saving…" : "Save"}</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
        {edit.id && <><span className="spacer" /><button className="btn ghost danger" disabled={busy} onClick={() => run(async () => { await api.removeEvalCase(edit.id!); onDone(); })}>Delete</button></>}
      </div>
    </section>
  );
}

