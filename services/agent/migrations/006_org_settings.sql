-- Small organization facts the agent uses verbatim (editable in the admin console).
create table if not exists org_settings (
  key        text primary key,
  value      jsonb not null,
  updated_by text not null default 'seed',
  updated_at timestamptz not null default now()
);
-- Board roster is entered in the admin console (Board & contacts).
insert into org_settings (key, value) values
  ('board_members', '[]'),
  ('board_contact', '{"name": "CAM, our management company", "email": "admin@camanagers.com"}')
on conflict (key) do nothing;
