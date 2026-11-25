import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type ModelsData, type SubNode, type SubResult, type SubRubric } from "./api";
import { ago, Empty, ErrorBox, Field, useAction, useLoad, usePoll } from "./ui";

const LABEL: Record<string, string> = { case_plan: "Supervisor", plate_read: "Plate reader", case_match: "Matcher", case_policy: "Policy", case_writer: "Writer" };
const usd = (n: number | null | undefined) => (n == null ? "—" : `$${n < 0.1 ? n.toFixed(4) : n.toFixed(2)}`);
const secs = (ms: number | null | undefined) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)}s`);
const pct = (a: number, b: number) => (b ? Math.round((a / b) * 100) : 0);

export function SubagentsTab() {
  const { data, error, reload } = useLoad(api.subEvals);
  const [models, setModels] = useState<ModelsData | null>(null);
  useEffect(() => { api.models().then(setModels, () => undefined); }, []);
  usePoll(!!data?.runs.some((r) => r.status === "running"), reload, 4000);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p className="muted">Loading…</p>;
  const label = (id: string) => (id === "current" ? "Current setup" : models?.models.find((m) => m.id === id)?.label ?? id);
  return (
    <>
      <p className="sub">Each specialist in the security case file, tested on its own against fixed fictional cases (Example Lane households, ZZ plates, drawn photos), across several models side by side. Automatic checks always run; the judge scores against your rubric once a sub-agent has one. Nothing is filed or looked up for real.</p>
      <NewRun nodes={data.nodes} models={models} onStarted={reload} />
      <Rubrics nodes={data.nodes} rubrics={data.rubrics} cases={data.cases} onSaved={reload} />
      <h3 className="h3">Runs</h3>
      {data.runs.length === 0 ? <Empty title="No sub-agent runs yet">Start with every sub-agent on the current setup plus one or two models to compare.</Empty> : (
        <div className="tbl"><table className="stack-sm">
          <thead><tr><th>Run</th><th>Passed</th><th className="num">Cost</th></tr></thead>
          <tbody>{data.runs.map((r) => (
            <tr key={r.id}>
              <td data-label="Run"><Link to={`/admin/evals/sub/${r.id}`} className="strong">{r.label}</Link>
                <div className="small muted">{r.started_by} · {ago(r.started_at)} · {r.plan.nodes.map((n) => LABEL[n] ?? n).join(", ")} · {r.plan.models.map(label).join(" vs ")}</div></td>
              <td data-label="Passed">{r.status === "running"
                ? <><div className="meter"><span style={{ width: `${pct(r.finished, r.total)}%` }} /></div><span className="small muted">{r.finished} of {r.total} done</span></>
                : <><b>{r.passed}</b> / {r.total} <span className="small muted">({pct(r.passed, r.total)}%)</span>{r.status === "cancelled" && <span className="chip muted">cancelled</span>}</>}</td>
              <td data-label="Cost" className="num">{usd(r.cost)}</td>
            </tr>))}</tbody>
        </table></div>)}
    </>
  );
}

function NewRun({ nodes, models, onStarted }: { nodes: SubNode[]; models: ModelsData | null; onStarted: () => void }) {
  const [label, setLabel] = useState("");
  const [pick, setPick] = useState<string[]>(nodes.map((n) => n.node));
  const [mods, setMods] = useState<string[]>(["current"]);
  const { busy, error, run } = useAction();
  const nav = useNavigate();
  const chat = (models?.models ?? []).filter((m) => m.kind === "chat" && m.enabled);
  const toggle = (xs: string[], x: string) => (xs.includes(x) ? xs.filter((y) => y !== x) : [...xs, x]);
  const jobs = nodes.filter((n) => pick.includes(n.node)).reduce((t, n) =>
    t + n.cases * mods.filter((m) => m === "current" || !n.gemini_only || chat.find((c) => c.id === m)?.provider === "gemini").length, 0);
  const start = () => run(async () => {
    const r = await api.startSubEval({ label: label || `${mods.length > 1 ? "Compare" : "Check"}: ${mods.map((m) => m === "current" ? "current" : m).join(" vs ")}`, nodes: pick, models: mods });
    onStarted(); nav(`/admin/evals/sub/${r.id}`);
  });
  return (
    <section className="panel">
      <h3 className="h3">New sub-agent run</h3>
      <ErrorBox error={error} />
      <Field label="Label"><input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Flash vs Flash-Lite for every sub-agent" /></Field>
      <fieldset className="checks"><legend className="small strong">Sub-agents</legend>
        {nodes.map((n) => <label key={n.node} className="check"><input type="checkbox" checked={pick.includes(n.node)} onChange={() => setPick(toggle(pick, n.node))} />
          {LABEL[n.node] ?? n.node} <span className="small muted">({n.cases} cases)</span></label>)}
      </fieldset>
      <fieldset className="checks"><legend className="small strong">Models to compare</legend>
        <label className="check"><input type="checkbox" checked={mods.includes("current")} onChange={() => setMods(toggle(mods, "current"))} /> Current setup <span className="small muted">(each step's live model)</span></label>
        {chat.map((m) => <label key={m.id} className="check"><input type="checkbox" checked={mods.includes(m.id)} onChange={() => setMods(toggle(mods, m.id))} />
          {m.label}{m.provider !== "gemini" && <span className="small muted"> (skips plate reader and matcher: they need Gemini)</span>}</label>)}
      </fieldset>
      <p className="small muted">{jobs} test runs. Real model calls, usually a few cents in total; results show the cost.</p>
      <button className="btn primary" disabled={busy || !pick.length || !mods.length || !jobs} onClick={start}>{busy ? "Starting…" : "Run sub-agent evals"}</button>
    </section>
  );
}

function Rubrics({ nodes, rubrics, cases, onSaved }: { nodes: SubNode[]; rubrics: Record<string, SubRubric>; cases: { key: string; node: string; title: string }[]; onSaved: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <section className="panel">
      <h3 className="h3">Rubrics</h3>
      <p className="small muted">What a good output looks like for each sub-agent, in your words. The judge scores every case against it (1–5); a case passes when all checks pass and the score meets the pass mark. Without a rubric, only the automatic checks count.</p>
      <ul className="plain">{nodes.map((n) => {
        const r = rubrics[n.node];
        return (
          <li key={n.node} className="rubric-row">
            <div className="row-between">
              <div><b>{LABEL[n.node] ?? n.node}</b> <span className="small muted">· {n.about}</span>
                <div className="small">{r?.rubric?.trim() ? <span className="ok-text">Rubric set · pass at {r.pass_score}/5</span> : <span className="muted">No rubric yet</span>}</div></div>
              <button className="btn ghost small" aria-expanded={open === n.node} onClick={() => setOpen(open === n.node ? null : n.node)}>{open === n.node ? "Close" : r?.rubric ? "Edit" : "Add rubric"}</button>
            </div>
            {open === n.node && <RubricForm node={n.node} r={r} cases={cases.filter((c) => c.node === n.node)} onSaved={() => { setOpen(null); onSaved(); }} />}
          </li>);
      })}</ul>
    </section>
  );
}

function RubricForm({ node, r, cases, onSaved }: { node: string; r?: SubRubric; cases: { key: string; title: string }[]; onSaved: () => void }) {
  const [text, setText] = useState(r?.rubric ?? "");
  const [pass, setPass] = useState(r?.pass_score ?? 4);
  const { busy, error, run } = useAction();
  return (
    <div className="rubric-form">
      <ErrorBox error={error} />
      <Field label="Rubric" hint="Criteria the judge scores, one per line works well, e.g. “Accuracy: … / Calibration: … / Privacy: …”">
        <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} /></Field>
      <Field label="Pass mark"><select value={pass} onChange={(e) => setPass(Number(e.target.value))}>{[3, 4, 5].map((n) => <option key={n} value={n}>{n} / 5</option>)}</select></Field>
      <details><summary className="small">The {cases.length} test cases</summary><ul className="small">{cases.map((c) => <li key={c.key}>{c.title}</li>)}</ul></details>
      <button className="btn primary" disabled={busy} onClick={() => run(async () => { await api.saveRubric(node, { rubric: text, pass_score: pass }); onSaved(); })}>{busy ? "Saving…" : "Save rubric"}</button>
    </div>
  );
}

export function SubagentRun({ id }: { id: number }) {
  const { data: r, error, reload } = useLoad(() => api.subEvalRun(id), [id]);
  usePoll(r?.status === "running", reload, 3000);
  const [models, setModels] = useState<ModelsData | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [onlyFail, setOnlyFail] = useState(false);
  const { run, busy } = useAction();
  useEffect(() => { api.models().then(setModels, () => undefined); }, []);
  if (error) return <ErrorBox error={error} />;
  if (!r) return <p className="muted">Loading…</p>;
  const label = (mid: string) => (mid === "current" ? "Current setup" : models?.models.find((m) => m.id === mid)?.label ?? mid);
  const mids = r.plan.models;
  const nodes = r.plan.nodes;
  const cell = (n: string, m: string) => r.scoreboard.find((s) => s.node === n && s.model === m);
  const shown = r.results.filter((x) => !onlyFail || x.passed === false);
  return (
    <>
      <header className="page-head"><div><p className="crumbs"><Link to="/admin/evals">Evals</Link> · Case-file sub-agents</p><h1>{r.label}</h1>
        <p className="sub">{r.started_by} · {ago(r.started_at)} · {r.status === "running" ? `${r.finished} of ${r.total} done, running…` : `${r.passed} of ${r.total} passed`} · {usd(r.cost)}</p></div></header>
      <h3 className="h3">Scoreboard</h3>
      <div className="tbl"><table className="scoreboard">
        <thead><tr><th>Sub-agent</th>{mids.map((m) => <th key={m}>{label(m)}</th>)}</tr></thead>
        <tbody>{nodes.map((n) => (
          <tr key={n}><th scope="row">{LABEL[n] ?? n}</th>
            {mids.map((m) => { const s = cell(n, m); return (
              <td key={m} data-label={label(m)}>{!s ? <span className="small muted">n/a</span> : <>
                <div><b className={s.finished < s.cases ? "" : s.passed === s.cases ? "ok-text" : s.passed < s.cases / 2 ? "crit-text" : ""}>{s.passed}/{s.cases}</b> passed
                  {s.finished < s.cases && <span className="small muted"> · {s.cases - s.finished} running</span>}</div>
                <div className="small muted">{s.avg_score != null ? `judge ${s.avg_score} · ` : ""}{usd(s.cost)} · {secs(s.p50_ms)}{s.errors ? ` · ${s.errors} errors` : ""}</div></>}</td>); })}
          </tr>))}</tbody>
      </table></div>
      <div className="toolbar">
        <label className="check"><input type="checkbox" checked={onlyFail} onChange={(e) => setOnlyFail(e.target.checked)} /> Only failures</label>
        {r.status === "running" && <button className="btn ghost small" disabled={busy} onClick={() => run(async () => { await api.cancelSubEval(id); reload(); })}>Cancel run</button>}
      </div>
      <div className="tbl"><table className="stack-sm">
        <thead><tr><th>Case</th><th>Model</th><th>Result</th><th className="num">Score</th><th className="num">Cost</th><th className="num">Time</th></tr></thead>
        <tbody>{shown.map((x) => { const k = `${x.case_key}|${x.model}`; return <Row key={k} x={x} label={label(x.model)} open={open === k} toggle={() => setOpen(open === k ? null : k)} />; })}</tbody>
      </table></div>
    </>
  );
}

function Row({ x, label, open, toggle }: { x: SubResult; label: string; open: boolean; toggle: () => void }) {
  const failed = Object.entries(x.checks ?? {}).filter(([, c]) => !c.ok);
  return (
    <>
      <tr className="clickable" onClick={toggle}>
        <td data-label="Case"><button className="linkish strong" aria-expanded={open}>{x.title}</button><div className="small muted">{LABEL[x.node] ?? x.node}</div></td>
        <td data-label="Model" className="small">{label}</td>
        <td data-label="Result">{x.status === "pending" ? <span className="chip muted">running</span>
          : x.passed ? <span className="chip st-succeeded">pass</span> : <span className="chip st-failed">{x.status === "error" ? "error" : "fail"}</span>}
          {failed.length > 0 && <div className="small crit-text">{failed.map(([k]) => k).join(", ")}</div>}</td>
        <td data-label="Score" className="num">{x.judge?.score ?? "—"}</td>
        <td data-label="Cost" className="num">{usd(x.cost_usd)}</td>
        <td data-label="Time" className="num">{secs(x.latency_ms)}</td>
      </tr>
      {open && <tr className="detail"><td colSpan={6}>
        <div className="eval-detail">
          {x.error && <div className="error">{x.error}</div>}
          <ul className="plain small">{Object.entries(x.checks ?? {}).map(([k, c]) => <li key={k}><span className={c.ok ? "ok-text" : "crit-text"}>{c.ok ? "✓" : "✗"} {k}</span> <span className="muted">{c.detail}</span></li>)}</ul>
          {x.judge && <p className="small"><b>Judge ({x.judge.score}/5):</b> {x.judge.reason}
            {x.judge.criteria && Object.keys(x.judge.criteria).length > 0 && <span className="muted"> · {Object.entries(x.judge.criteria).map(([k, v]) => `${k} ${v}`).join(", ")}</span>}</p>}
          {x.output != null && <details><summary className="small">Output</summary><pre className="json">{JSON.stringify(x.output, null, 2)}</pre></details>}
        </div></td></tr>}
    </>
  );
}
