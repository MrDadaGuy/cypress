-- LLM FinOps: an admin-editable model catalog with prices, runtime settings, a budget, and a ledger of
-- every paid model call (tokens, cost, latency). Code only seeds these; the console owns them.

-- The model catalog. kind = 'chat' models can be assigned to graph nodes. 'embed' and 'rerank' entries
-- are priced so that spend is metered too; switching a rerank entry off skips reranking (cheaper, a bit
-- less precise). Changing the embedding model needs a re-index, so it isn't assignable.
create table if not exists llm_models (
  id            text primary key check (id ~ '^[a-z0-9][a-z0-9.-]{1,48}$'),
  kind          text not null default 'chat' check (kind in ('chat', 'embed', 'rerank')),
  provider      text not null check (provider in ('gemini', 'claude', 'maas', 'vertex_search')),
  name          text not null,            -- provider model name, e.g. gemini-3.8-flash
  region        text not null default 'global',
  label         text not null,
  thinking      text check (thinking in ('minimal', 'low', 'medium', 'high')),
  input_per_m   numeric(10, 4),           -- USD per 1M tokens; null = unknown (calls are flagged unpriced)
  cached_per_m  numeric(10, 4),
  output_per_m  numeric(10, 4),           -- thinking tokens are billed as output
  per_call_usd  numeric(10, 6),           -- flat price per request (e.g. the ranking API)
  price_source  text,
  enabled       boolean not null default true,
  note          text,
  updated_by    text not null default 'migration',
  updated_at    timestamptz not null default now()
);

insert into llm_models (id, kind, provider, name, region, label, thinking, input_per_m, cached_per_m, output_per_m,
                        price_source, enabled, note) values
  ('gemini-flash', 'chat', 'gemini', 'gemini-3.8-flash', 'global', 'Gemini 3.8 Flash', 'low', 1.50, 0.15, 7.50,
   'Cloud Billing catalog, 2026-11-20', true, null),
  ('gemini-flash-lite', 'chat', 'gemini', 'gemini-3.5-flash-lite', 'global', 'Gemini 3.5 Flash-Lite', null, 0.30, 0.03, 2.50,
   'Cloud Billing catalog, 2026-11-20', true, null),
  ('gpt-oss-120b', 'chat', 'maas', 'openai/gpt-oss-120b-maas', 'global', 'gpt-oss-120b (open weights)', null, 0.09, null, 0.36,
   'Cloud Billing catalog, 2026-11-20', true, null),
  ('llama-4-maverick', 'chat', 'maas', 'meta/llama-4-maverick-17b-128e-instruct-maas', 'us-east5',
   'Llama 4 Maverick (open weights)', null, 0.35, null, 1.15, 'Cloud Billing catalog, 2026-11-20', true, null),
  ('claude-sonnet', 'chat', 'claude', 'claude-sonnet-5-5', 'global', 'Claude Sonnet 5.5', null, 2.00, 0.10, 10.00,
   'Cloud Billing catalog, 2026-11-20', false, 'Vertex quota pending (429 on 2026-11-20)'),
  ('claude-haiku', 'chat', 'claude', 'claude-haiku-5-5', 'global', 'Claude Haiku 5.5', null, null, null, null,
   null, false, 'Vertex quota pending; not in the billing catalog yet, enter prices before enabling'),
  ('claude-opus', 'chat', 'claude', 'claude-opus-5-5', 'global', 'Claude Opus 5.5', null, 4.00, 0.20, 20.00,
   'Cloud Billing catalog, 2026-11-20', false, 'Vertex quota not requested'),
  ('gemini-embedding', 'embed', 'gemini', 'gemini-embedding-001', 'us-central1', 'Gemini Embedding', null, 0.15, null, 0,
   'Cloud Billing catalog, 2026-11-20', true, 'Used for search and ingest; changing it needs a full re-index')
on conflict (id) do nothing;
insert into llm_models (id, kind, provider, name, region, label, per_call_usd, input_per_m, output_per_m, price_source, note)
values ('semantic-ranker', 'rerank', 'vertex_search', 'semantic-ranker-default@latest', 'global', 'Vertex semantic ranker',
        0.001, 0, 0, 'Cloud Billing catalog, 2026-11-20 ($1 per 1,000 requests)',
        'Reorders search results before answering. Off = use the hybrid-search order (no cost)')
on conflict (id) do nothing;

-- Versioned agent configuration, per graph step ("node") and audience:
--   audience 'all' (default), 'residents' (anonymous public chat) or 'team' (signed-in staff,
--   security, board, admin); a missing audience falls back to 'all'.
-- Each save is a new version (the highest version is live); restoring copies an old version forward,
-- so history is append-only. Null fields mean "use the built-in default" for that step.
create table if not exists agent_configs (
  id          bigint generated always as identity primary key,
  node        text not null,
  audience    text not null default 'all' check (audience in ('all', 'residents', 'team')),
  version     int not null,
  model_id    text references llm_models (id) on update cascade,
  temperature numeric(3, 2) check (temperature between 0 and 2),
  max_tokens  int check (max_tokens between 16 and 32768),
  prompt      text,                       -- system prompt override (prompt editor; null = built-in)
  note        text,                       -- why it changed
  created_by  text not null,
  created_at  timestamptz not null default now(),
  unique (node, audience, version)
);
create index if not exists agent_configs_live on agent_configs (node, audience, version desc);

-- Carry over existing model choices as version 1, then retire the old table.
insert into agent_configs (node, audience, version, model_id, note, created_by, created_at)
select node, 'all', 1, model_id, 'Carried over from model assignments', updated_by, updated_at
from model_assignments where model_id in (select id from llm_models)
on conflict do nothing;
drop table if exists model_assignments;

-- One row per provider call, including hedge, fallback, failed and test calls: they cost money too.
-- cost_usd uses the price in effect at the time (null = the model had no price; fixable later).
create table if not exists llm_calls (
  id              bigint generated always as identity primary key,
  at              timestamptz not null default now(),
  node            text not null,            -- graph node, 'embed_query', 'embed_ingest' or 'probe'
  model_id        text not null,
  provider        text not null,
  purpose         text not null default 'primary' check (purpose in ('primary', 'hedge', 'fallback', 'probe')),
  input_tokens    int not null default 0,   -- includes cached tokens
  cached_tokens   int not null default 0,
  output_tokens   int not null default 0,   -- includes thinking tokens
  thinking_tokens int not null default 0,
  cost_usd        numeric(14, 8),
  estimated       boolean not null default false,  -- tokens estimated because the provider didn't report usage
  latency_ms      int,
  ok              boolean not null default true,
  error           text,
  config_id       bigint,                   -- agent_configs.id in effect (compare cost/quality across versions)
  turn_id         uuid,                     -- interactions.public_id when the call was part of a chat turn
  channel         text,
  user_role       text
);
create index if not exists llm_calls_at on llm_calls (at desc);
create index if not exists llm_calls_model_at on llm_calls (model_id, at desc);
create index if not exists llm_calls_turn on llm_calls (turn_id) where turn_id is not null;

-- Runtime knobs and the budget, editable in the console.
insert into org_settings (key, value, updated_by) values
  ('llm_runtime', '{"fallback_model": "gemini-flash-lite", "hedge_s": 4, "stream_hedge_s": 5, "timeout_s": 25}', 'migration'),
  ('llm_budget', '{"monthly_usd": 15, "guard": true, "guard_model": "gemini-flash-lite", "alert_pct": 80}', 'migration')
on conflict (key) do nothing;
