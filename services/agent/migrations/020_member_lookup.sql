-- Member lookup for board, staff and security (an audited tool, never retrieval). What each role sees is an
-- admin setting: 'details' (household, address, status, names on file), 'confirm' (yes/no on what the asker
-- already said) or 'off'. Email addresses are never returned. Every lookup is logged here for admins.

create table if not exists member_lookups (
  id          bigint generated always as identity primary key,
  at          timestamptz not null default now(),
  actor       text not null,            -- email, Slack user or uid of the person who asked
  role        text,                     -- the role the lookup ran as (a Slack channel's shared role)
  channel     text not null,
  address_q   text,                     -- what was asked, as given
  name_q      text,
  mode        text not null,            -- details | confirm
  result      text not null,            -- active_member | not_found | name_mismatch | matches | rate_limited ...
  matches     int not null default 0
);
create index if not exists member_lookups_at on member_lookups (at desc);

insert into org_settings (key, value, updated_by) values
  ('member_lookup', '{"access": {"security": "confirm", "staff": "confirm", "board": "details", "admin": "details"}, "per_hour": 30, "per_day": 100}', 'migration')
on conflict (key) do nothing;
