-- Board votes on incident notice letters. A letter goes to CAM only when the board passes it. The rule is an
-- admin setting (org_settings 'board_votes'): a majority of the seated board (4 of 7) by default, or a fixed
-- number. Only people with an active board term vote (Users & roles), from Slack or the console.

alter table incident_cases
  add column if not exists vote_status text not null default 'none'
    check (vote_status in ('none', 'open', 'passed', 'failed', 'expired', 'cancelled')),
  add column if not exists vote_opened_at timestamptz,
  add column if not exists vote_closes_at timestamptz,
  add column if not exists vote_needed int,
  add column if not exists vote_seats int,
  add column if not exists vote_slack_channel text,
  add column if not exists vote_slack_ts text;

create table if not exists case_votes (
  case_id   bigint not null references incident_cases (id) on delete cascade,
  person_id bigint not null references people (id) on delete restrict,
  vote      text not null check (vote in ('yes', 'no')),
  via       text not null check (via in ('slack', 'console')),
  at        timestamptz not null default now(),
  primary key (case_id, person_id)
);

insert into org_settings (key, value, updated_by)
values ('board_votes', '{"rule": "majority", "count": 4, "seats": 7, "days": 3, "auto_start": true}', 'migration')
on conflict (key) do nothing;
