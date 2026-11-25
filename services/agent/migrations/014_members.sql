-- Members: households and their email addresses (imported from the membership list by admins).
-- Private: only admins can read these, through the console. The agent never retrieves them; a signed-in
-- member's own address is matched to grant the "member" role and nothing else is revealed.

create table if not exists households (
  id          bigint generated always as identity primary key,
  name        text not null,                         -- e.g. "Smith" or "Smith / Jones"
  address     text,
  account_ref text,                                  -- id from the source list, used to match re-imports
  status      text not null default 'active' check (status in ('active', 'inactive')),
  notes       text,
  source      text,                                  -- which import created/last updated it
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create unique index if not exists households_account_ref on households (account_ref) where account_ref is not null;

create table if not exists member_emails (
  id             bigint generated always as identity primary key,
  household_id   bigint not null references households (id) on delete cascade,
  email          text not null unique check (email = lower(email) and email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  name           text,
  firebase_uid   text unique,
  last_signed_in timestamptz,
  created_at     timestamptz not null default now()
);
create index if not exists member_emails_household on member_emails (household_id);

-- Something an anonymous resident asked for that needs a signed-in member (e.g. filing a report).
-- Kept for a short time; completed once, by whoever signs in with the emailed link.
create table if not exists pending_actions (
  id           uuid primary key default gen_random_uuid(),
  kind         text not null check (kind in ('report_issue')),
  payload      jsonb not null,
  attachments  text[] not null default '{}',
  channel      text not null default 'web',
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null default now() + interval '1 day',
  completed_at timestamptz,
  completed_by text,                                 -- member email
  result       jsonb
);
create index if not exists pending_actions_expires on pending_actions (expires_at);

-- Admin settings for member sign-in.
insert into org_settings (key, value, updated_by)
values ('member_auth', '{"require_for_reports": true, "nudge_after_turns": 3, "pending_hours": 24}', 'migration')
on conflict (key) do nothing;
