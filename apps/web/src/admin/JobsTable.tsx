import { Link } from "react-router-dom";
import { api } from "./api";
import { ago, Empty, ErrorBox, StatusChip, useAction, useLoad, usePoll } from "./ui";

function duration(a: string | null, b: string | null) {
  if (!a) return "—";
  const s = Math.round(((b ? new Date(b) : new Date()).getTime() - new Date(a).getTime()) / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export default function JobsTable({ setId }: { setId?: number }) {
  const { data: jobs, error, reload } = useLoad(() => api.jobs(setId), [setId]);
  usePoll(!!jobs?.some((j) => j.status === "queued" || j.status === "running"), reload);
  const action = useAction();

  if (error) return <ErrorBox error={error} />;
  if (!jobs) return <p className="muted">Loading…</p>;
  if (jobs.length === 0) return <Empty title="No runs yet"><p>Runs appear here when you run a source or a whole set.</p></Empty>;

  return (
    <>
      <ErrorBox error={action.error} />
      <div className="tbl">
        <table className="stack-sm">
          <thead><tr><th>#</th>{!setId && <th>Set</th>}<th>Source</th><th>Status</th><th>Results</th><th>Started</th><th>Took</th><th>By</th><th></th></tr></thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td className="mono small">{j.id}</td>
                {!setId && <td><Link to={`/admin/sets/${j.set_id}?tab=jobs`}>{j.set_name}</Link></td>}
                <td className="mono small break">{j.source_label || j.source_url || "—"}</td>
                <td><StatusChip status={j.status} />{j.error && <div className="small err-text" title={j.error}>{j.error.slice(0, 100)}</div>}</td>
                <td className="small">{Object.keys(j.stats).length ? Object.entries(j.stats).map(([k, v]) => `${k.replace("_", " ")} ${v}`).join(" · ") : "—"}</td>
                <td className="small">{ago(j.started_at ?? j.created_at)}</td>
                <td className="small">{duration(j.started_at, j.finished_at)}</td>
                <td className="small muted">{j.triggered_by}</td>
                <td>{(j.status === "queued" || j.status === "running") &&
                  <button className="btn small ghost" disabled={action.busy} onClick={() => action.run(async () => { await api.cancelJob(j.id); reload(); })}>Cancel</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
