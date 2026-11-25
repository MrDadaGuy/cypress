-- Sub-agent evals: each case-file sub-agent (supervisor, plate reader, matcher, policy, writer) scored on its own
-- against fixed fictional cases, across several models at once, so the board can see which model each step needs.
-- Cases live in code (subagent_evals.CASES, fictional data only); the judge's rubric per sub-agent is an admin
-- setting (org_settings 'subagent_rubrics').

create table if not exists subagent_eval_runs (
  id           bigint generated always as identity primary key,
  label        text not null,
  plan         jsonb not null default '{}',    -- {"nodes": [...], "models": [...], "prompts": {key: text}}
  status       text not null default 'running' check (status in ('running', 'done', 'cancelled')),
  cases        int not null default 0,
  started_by   text not null,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz
);
create index if not exists subagent_eval_runs_started on subagent_eval_runs (started_at desc);

create table if not exists subagent_eval_results (
  run_id      bigint not null references subagent_eval_runs (id) on delete cascade,
  case_key    text not null,
  node        text not null,
  model       text not null,
  status      text not null default 'pending' check (status in ('pending', 'done', 'error')),
  output      jsonb,
  checks      jsonb not null default '{}',     -- name -> {ok, detail}
  judge       jsonb,                           -- {score 1-5, verdict, reason, criteria}
  passed      boolean,
  cost_usd    numeric(14, 8),
  latency_ms  int,
  error       text,
  finished_at timestamptz,
  primary key (run_id, case_key, model)
);
