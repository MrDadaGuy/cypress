-- People with a Cypress role, their Slack identity, and groups.
-- The source of truth for roles (replaces Firebase custom claims). Board, staff and security
-- volunteers only: this is NOT the member roster, and holds no member data.
create table if not exists people (
  id              bigint generated always as identity primary key,
  email           text not null check (email = lower(email) and email like '%@%'),
  name            text,
  role            text check (role in ('admin', 'board', 'staff', 'security', 'member')),
  active          boolean not null default true,
  slack_user_id   text unique,
  slack_name      text,
  firebase_uid    text unique,
  notes           text,
  last_seen_web   timestamptz,
  last_seen_slack timestamptz,
  created_by      text not null default 'system',
  created_at      timestamptz not null default now(),
  updated_by      text,
  updated_at      timestamptz not null default now()
);
create unique index if not exists people_email on people (email);

create table if not exists groups (
  id                bigint generated always as identity primary key,
  name              text not null unique,
  description       text,
  -- New tickets in these categories @-mention the group's members in Slack.
  ticket_categories text[] not null default '{}',
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create table if not exists group_members (
  group_id  bigint not null references groups(id) on delete cascade,
  person_id bigint not null references people(id) on delete cascade,
  primary key (group_id, person_id)
);

-- Bootstrap admin for a fresh install. Change it with: python -m cypress.tools.set_role YOU@EXAMPLE.COM admin
insert into people (email, role, created_by) values ('admin@example.com', 'admin', 'migration')
on conflict (email) do nothing;

-- What a full Slack workspace member who isn't listed above gets. 'board' keeps today's behaviour
-- (the workspace is the board's); tighten to 'member' or 'none' once everyone is listed.
insert into org_settings (key, value) values ('slack_default_role', '"board"') on conflict (key) do nothing;
