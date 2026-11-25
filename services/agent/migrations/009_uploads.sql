-- Photos people attach in chat (web or Slack). Bytes live in the private uploads bucket.
create table if not exists uploads (
  id         uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  channel    text not null,
  owner      text not null,            -- uid, slack:<user>, or ip:<addr> (abuse tracing only)
  path       text not null,            -- object path in the bucket (or local file in dev)
  bytes      int not null,
  width      int not null,
  height     int not null,
  ticket_id  bigint                    -- set when a report is filed with it
);
create index if not exists uploads_ticket on uploads (ticket_id) where ticket_id is not null;
