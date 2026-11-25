-- Content management: document sets, their sources, ingest jobs, documents and chunks.
-- A document set is the unit an admin manages and the agent retrieves from. Its visibility
-- decides who can retrieve it: 'public' (anyone, including anonymous residents) or 'board'
-- (signed-in board/staff only).

create extension if not exists vector;

create table document_sets (
  id             bigint generated always as identity primary key,
  slug           text not null unique check (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  name           text not null,
  description    text not null default '',
  visibility     text not null default 'public' check (visibility in ('public', 'board')),
  doc_type       text not null default 'general' check (doc_type in
                   ('bylaws', 'rules', 'deed', 'court_record', 'faq', 'history',
                    'newsletter', 'minutes', 'general')),
  authority_rank smallint not null default 50 check (authority_rank between 0 and 100),
  enabled        boolean not null default true,
  -- Per-set ingest and retrieval knobs (Dify-style knowledge base settings):
  -- chunk_tokens, chunk_overlap, top_k, min_score, rerank
  settings       jsonb not null default
                   '{"chunk_tokens": 800, "chunk_overlap": 100, "top_k": 6, "min_score": 0, "rerank": true}',
  created_by     text not null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

-- kind: 'web' = crawl from a seed URL; 'pdf' = a single PDF URL; 'upload' = admin-uploaded file.
-- config (web): max_depth, max_pages, allow_domains[], include[], exclude[], follow_pdfs, respect_robots
-- config (pdf/upload): ocr = 'auto' | 'always' | 'never'
create table sources (
  id          bigint generated always as identity primary key,
  set_id      bigint not null references document_sets (id) on delete cascade,
  kind        text not null check (kind in ('web', 'pdf', 'upload')),
  url         text,
  label       text not null default '',
  config      jsonb not null default '{}',
  schedule    text not null default 'manual' check (schedule in ('manual', 'daily', 'weekly')),
  enabled     boolean not null default true,
  created_by  text not null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  check (kind = 'upload' or url ~ '^https?://')
);
create index on sources (set_id);

create table jobs (
  id           bigint generated always as identity primary key,
  set_id       bigint not null references document_sets (id) on delete cascade,
  source_id    bigint references sources (id) on delete set null,
  kind         text not null default 'ingest' check (kind in ('ingest', 'reembed')),
  status       text not null default 'queued' check (status in
                 ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  triggered_by text not null,
  created_at   timestamptz not null default now(),
  started_at   timestamptz,
  finished_at  timestamptz,
  stats        jsonb not null default '{}',
  error        text
);
create index on jobs (set_id, created_at desc);
create index on jobs (status) where status in ('queued', 'running');

create table documents (
  id           bigint generated always as identity primary key,
  set_id       bigint not null references document_sets (id) on delete cascade,
  source_id    bigint references sources (id) on delete set null,
  uri          text not null,
  title        text not null default '',
  mime         text not null default '',
  status       text not null default 'active' check (status in
                 ('active', 'needs_review', 'excluded', 'gone')),
  content_hash text not null,
  markdown     text not null,
  original_uri text,            -- gs:// copy of the fetched original (PDF, HTML)
  version      int not null default 1,
  meta         jsonb not null default '{}',   -- effective_date, pages, ocr_pages, ...
  fetched_at   timestamptz not null default now(),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (set_id, uri)
);

create table document_versions (
  document_id  bigint not null references documents (id) on delete cascade,
  version      int not null,
  content_hash text not null,
  markdown     text not null,
  fetched_at   timestamptz not null,
  primary key (document_id, version)
);

create table chunks (
  id           bigint generated always as identity primary key,
  document_id  bigint not null references documents (id) on delete cascade,
  ord          int not null,
  section_path text not null default '',
  content      text not null,
  tokens       int not null default 0,
  embedding    vector(768),
  tsv          tsvector generated always as (to_tsvector('english', content)) stored,
  unique (document_id, ord)
);
create index on chunks using hnsw (embedding vector_cosine_ops);
create index on chunks using gin (tsv);

create table audit_log (
  id        bigint generated always as identity primary key,
  at        timestamptz not null default now(),
  actor     text not null,
  action    text not null,
  entity    text not null,
  entity_id text,
  detail    jsonb not null default '{}'
);
create index on audit_log (at desc);
