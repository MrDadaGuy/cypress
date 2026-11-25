-- Issue tickets (owned by the Rust tickets service; schema lives here so one migrator runs).
create table if not exists tickets (
  id               bigint generated always as identity (start with 1001) primary key,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  status           text not null default 'new'
                   check (status in ('new', 'acknowledged', 'in_progress', 'resolved', 'closed')),
  category         text not null default 'other'
                   check (category in ('maintenance', 'safety', 'rules', 'pool', 'grounds', 'other')),
  priority         text not null default 'normal' check (priority in ('low', 'normal', 'high', 'urgent')),
  title            text not null check (length(title) between 3 and 200),
  description      text not null check (length(description) <= 4000),
  location         text check (length(location) <= 300),
  channel          text not null,                 -- web, slack, console
  reporter_role    text,                          -- null = anonymous public
  reporter_uid     text,
  reporter_contact text check (length(reporter_contact) <= 200),  -- optional, staff+ only
  assignee         text,
  slack_channel    text,                          -- where the board notification was posted
  slack_ts         text
);
create index if not exists tickets_open on tickets (status, created_at desc);

create table if not exists ticket_events (
  id         bigint generated always as identity primary key,
  ticket_id  bigint not null references tickets (id) on delete cascade,
  at         timestamptz not null default now(),
  actor      text not null,
  kind       text not null check (kind in ('created', 'status', 'assigned', 'priority', 'comment')),
  body       text check (length(body) <= 4000),
  data       jsonb not null default '{}'
);
create index if not exists ticket_events_ticket on ticket_events (ticket_id, at);
