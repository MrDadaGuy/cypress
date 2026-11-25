-- Per-channel ceiling on what Cypress shares in a Slack channel. Cypress already limits a channel
-- answer to what every member of the channel may see; a ceiling can only lower that further
-- (e.g. keep #security at 'security' even if only board members are in it today).
create table if not exists slack_channels (
  channel_id text primary key,
  name       text,
  max_role   text check (max_role in ('public', 'member', 'staff', 'security', 'board')),
  note       text,
  updated_by text not null,
  updated_at timestamptz not null default now()
);
