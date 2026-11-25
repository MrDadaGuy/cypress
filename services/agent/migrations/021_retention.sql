-- Retention: abandoned uploads, attachments on long-closed tickets, and the member lookup log are cleaned up
-- nightly (admin settings in org_settings 'retention'). A legal hold (per ticket, or everything at once)
-- stops all of it, and also puts a temporary hold on the objects in the bucket so its 2-year safety-net rule
-- can't delete them either.

alter table uploads add column if not exists removed_at timestamptz;      -- attachment removed by retention
alter table uploads add column if not exists held boolean not null default false;  -- bucket temporary hold applied

create table if not exists legal_holds (
  ticket_id  bigint primary key references tickets (id) on delete restrict,
  set_at     timestamptz not null default now(),
  set_by     text not null,
  reason     text not null check (length(reason) between 3 and 500)
);

create table if not exists retention_runs (
  id       bigint generated always as identity primary key,
  at       timestamptz not null default now(),
  summary  jsonb not null
);

insert into org_settings (key, value, updated_by) values
  ('retention', '{"unattached_hours": 24, "closed_ticket_days": 180, "lookup_log_days": 365, "hold_all": false, "hold_all_reason": null}', 'migration')
on conflict (key) do nothing;
