-- What changed when a re-crawl updated a document: a line diff against the previous version (kept in
-- document_versions) plus a short model summary of changed facts (before -> after). Also new pages and pages
-- that disappeared, on sources that had been crawled before. Shown in Content checks -> Changes and the digest.
create table if not exists document_changes (
  id           bigint generated always as identity primary key,
  at           timestamptz not null default now(),
  document_id  bigint not null references documents (id) on delete cascade,
  job_id       bigint,
  kind         text not null check (kind in ('changed', 'new', 'gone')),
  version      int not null,
  title        text not null,
  uri          text,
  summary      jsonb,                 -- [{what, before, after, important}]; null = not summarized
  trivial      boolean not null default false,   -- only formatting, dates stamped on the page, etc.
  added        int not null default 0,
  removed      int not null default 0,
  diff         text,                  -- capped unified diff
  status       text not null default 'new' check (status in ('new', 'reviewed')),
  reviewed_by  text,
  reviewed_at  timestamptz,
  unique (document_id, kind, version)
);
create index if not exists document_changes_at on document_changes (at desc);
