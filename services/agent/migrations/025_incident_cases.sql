-- Security incidents and their case files. A guard or staff member files an incident (a ticket in the new
-- 'security' category); the worker builds a case file with a supervisor and sub-agents (cases.py), then waits
-- for the board. The full case file (household, contact details, vehicles, history) is for the roles in
-- org_settings 'cases' (board and admin by default); everyone else sees only whether there was a match.

alter table tickets drop constraint if exists tickets_category_check;
alter table tickets add constraint tickets_category_check
  check (category in ('maintenance', 'safety', 'rules', 'pool', 'grounds', 'security', 'other'));

create table if not exists incident_cases (
  id            bigint generated always as identity primary key,
  ticket_id     bigint not null unique references tickets (id) on delete restrict,
  status        text not null default 'queued'
                check (status in ('queued', 'running', 'awaiting_board', 'approved', 'dismissed', 'failed')),
  report        jsonb not null default '{}',       -- what the reporter gave: plates, names, addresses, vehicle, when
  attachments   text[] not null default '{}',      -- upload ids (photos)
  plan          jsonb,                             -- the supervisor's choice of sub-agents
  findings      jsonb not null default '{}',       -- per sub-agent output
  household_ids bigint[] not null default '{}',    -- households matched (high/medium confidence)
  profile       jsonb,                             -- the writer's summary + contact block (board only)
  letter        jsonb,                             -- {subject, body} draft for the board; null if none
  decision      jsonb,                             -- {action, note, letter} from the board
  decided_by    text,
  decided_at    timestamptz,
  error         text,
  trace_id      text,
  created_by    text,
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz
);
create index if not exists incident_cases_status on incident_cases (status, created_at);
create index if not exists incident_cases_households on incident_cases using gin (household_ids);

insert into org_settings (key, value, updated_by)
values ('cases', '{"full_roles": ["board", "admin"], "vision": true, "draft_letters": true}', 'migration')
on conflict (key) do nothing;
