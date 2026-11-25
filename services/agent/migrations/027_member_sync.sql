-- Scheduled sync of the membership and vehicle lists from the Google Sheet that is their source of truth
-- (until the Vantaca sync exists). Settings in org_settings 'member_sync'; each run is recorded (counts only).

create table if not exists member_sync_runs (
  id        bigint generated always as identity primary key,
  at        timestamptz not null default now(),
  trigger   text not null,                 -- nightly | console
  status    text not null check (status in ('ok', 'warning', 'failed', 'skipped')),
  stats     jsonb not null default '{}',
  error     text
);
create index if not exists member_sync_runs_at on member_sync_runs (at desc);

insert into org_settings (key, value, updated_by)
values ('member_sync', '{"enabled": false, "sheet_id": null, "members_tab": null, "vehicles_tab": null,
                         "members_mapping": null, "vehicles_mapping": null, "replace": true, "min_fraction": 0.8}', 'migration')
on conflict (key) do nothing;
