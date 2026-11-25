-- Agent: per-node model choice, rate limiting, and an interaction log for analytics.

-- Which model each graph node uses. Missing rows fall back to the defaults in cypress/models.py.
create table model_assignments (
  node       text primary key,
  model_id   text not null,
  updated_by text not null,
  updated_at timestamptz not null default now()
);

-- Sliding-window rate limiting without Redis: one row per request, pruned as it goes.
create table rate_events (
  key text not null,
  at  timestamptz not null default now()
);
create index on rate_events (key, at);

-- One row per chat turn. Questions are logged so the board can see what people ask and where
-- the content has gaps; member data never appears here because lookups aren't logged verbatim.
create table interactions (
  id          bigint generated always as identity primary key,
  at          timestamptz not null default now(),
  channel     text not null,            -- web, slack, email
  thread_id   text not null,
  user_role   text,                     -- null = anonymous resident
  user_email  text,
  question    text not null,
  intent      text,
  answer      text,
  citations   jsonb not null default '[]',
  retrieval   jsonb not null default '{}',   -- queries tried, passage scores, grade
  models      jsonb not null default '{}',   -- node -> model id
  latency_ms  int,
  error       text,
  embedding   vector(768)                -- filled later for clustering/analytics
);
create index on interactions (at desc);
