-- Evals: a test set of questions with what a good answer needs, and runs that score Cypress against
-- it (live config, or with a draft prompt / different models), so changes can be checked before saving.

create table if not exists eval_cases (
  id          bigint generated always as identity primary key,
  question    text not null,
  as_role     text not null default 'public' check (as_role in ('public', 'member', 'staff', 'security', 'board')),
  -- What a good answer looks like. All keys optional:
  --   intent: expected route intent; must_include: [[any of...], ...] (every group needs one match);
  --   must_not_include: [...]; cite: [document title fragments, any one]; refuse: true (should decline);
  --   tool: expected tool name; ideal: a model answer for the judge to compare against
  expect      jsonb not null default '{}',
  tags        text[] not null default '{}',
  notes       text,
  active      boolean not null default true,
  created_by  text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create table if not exists eval_runs (
  id           bigint generated always as identity primary key,
  label        text not null,
  overrides    jsonb not null default '{}',    -- {"prompts": {key: text}, "models": {node: model_id}}
  status       text not null default 'running' check (status in ('running', 'done', 'cancelled')),
  cases        int not null default 0,
  started_by   text not null,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz
);
create index if not exists eval_runs_started on eval_runs (started_at desc);

create table if not exists eval_results (
  run_id      bigint not null references eval_runs (id) on delete cascade,
  case_id     bigint not null references eval_cases (id) on delete cascade,
  status      text not null default 'pending' check (status in ('pending', 'done', 'error')),
  answer      text,
  intent      text,
  citations   jsonb not null default '[]',
  tools       jsonb not null default '[]',
  checks      jsonb not null default '{}',     -- deterministic checks: name -> {ok, detail}
  judge       jsonb,                           -- {score 1-5, verdict, reason}
  passed      boolean,
  cost_usd    numeric(14, 8),
  latency_ms  int,
  error       text,
  finished_at timestamptz,
  primary key (run_id, case_id)
);
