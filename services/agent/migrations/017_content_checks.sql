-- Content checks: find documents that disagree (fees, dates, hours...) so admins can fix the source.
-- Cypress keeps answering by its authority rules (bylaws first); this surfaces the conflicts.

create table if not exists content_scans (
  id           bigint generated always as identity primary key,
  scope        text not null default 'all' check (scope in ('all', 'changed')),
  since        timestamptz,                   -- 'changed': documents updated since then
  status       text not null default 'queued' check (status in ('queued', 'running', 'done', 'failed')),
  requested_by text not null,
  created_at   timestamptz not null default now(),
  started_at   timestamptz,
  finished_at  timestamptz,
  stats        jsonb not null default '{}',
  error        text
);

create table if not exists content_issues (
  id           bigint generated always as identity primary key,
  fingerprint  text not null unique,          -- document pair + topic, so a rescan doesn't duplicate it
  topic        text not null,
  severity     text not null default 'medium' check (severity in ('high', 'medium', 'low')),
  a_doc_id     bigint references documents (id) on delete set null,
  b_doc_id     bigint references documents (id) on delete set null,
  a_title      text not null,
  b_title      text not null,
  a_uri        text,
  b_uri        text,
  a_says       text not null,
  b_says       text not null,
  a_excerpt    text,
  b_excerpt    text,
  suggestion   text,
  status       text not null default 'open' check (status in ('open', 'dismissed', 'resolved')),
  found_at     timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  closed_by    text,
  closed_at    timestamptz,
  note         text
);
create index if not exists content_issues_open on content_issues (status, severity);

insert into org_settings (key, value, updated_by)
values ('content_checks', '{"enabled": true, "min_similarity": 0.78, "max_pairs": 300}', 'migration')
on conflict (key) do nothing;
