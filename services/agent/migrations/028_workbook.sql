-- CAM's membership workbook (the sheet CAM maintains alongside Vantaca) imported as a whole: households with the
-- member names on file, up to three vehicles per household, gate cards, golf carts, vendor gate cards, and the
-- cell colors CAM uses as part of the record. What each color means is an admin-maintained legend
-- (org_settings 'member_colors'); each import compares the workbook with the last one and flags anything new.

alter table households
  add column if not exists people text[] not null default '{}',     -- member names on file (no emails in the workbook)
  add column if not exists row_color text,                          -- normalized hex of the row's fill, e.g. 'DBE5F1'
  add column if not exists highlights jsonb not null default '[]';  -- [{field, color, kind: fill|text}]

alter table vehicles add column if not exists highlights jsonb not null default '[]';

create table if not exists gate_cards (
  id           bigint generated always as identity primary key,
  card         text not null,
  household_id bigint references households (id) on delete set null,
  holder       text,                    -- vendor or contractor name when not a household
  kind         text not null check (kind in ('member', 'vendor')),
  issued       text,
  notes        text,
  color        text,
  source       text,
  updated_at   timestamptz not null default now(),
  unique (card, kind)
);
create index if not exists gate_cards_household on gate_cards (household_id);

create table if not exists golf_carts (
  id           bigint generated always as identity primary key,
  tag          text not null unique,
  household_id bigint references households (id) on delete set null,
  account_ref  text,
  description  text,
  serial       text,
  waiver       boolean,
  status_note  text,                    -- the workbook's INACTIVE column, as written
  color        text,
  source       text,
  updated_at   timestamptz not null default now()
);
create index if not exists golf_carts_household on golf_carts (household_id);

-- What the last workbook looked like (tabs, headers, colors, counts), so the next import can say what changed.
create table if not exists workbook_snapshots (
  id        bigint generated always as identity primary key,
  at        timestamptz not null default now(),
  source    text not null,             -- upload | sync
  shape     jsonb not null
);

insert into org_settings (key, value, updated_by)
values ('member_colors', '{"colors": {}}', 'migration')
on conflict (key) do nothing;

-- Membership type (full, senior, deeded access, ...): business rules will differ by type. The types are an admin
-- list (org_settings 'member_types'); a household's type comes from a legend color's effect or a mapped column.
alter table households add column if not exists member_type text;
insert into org_settings (key, value, updated_by)
values ('member_types', '{"types": [{"key": "full", "label": "Full member"}, {"key": "senior", "label": "Senior member"},
                                    {"key": "deeded", "label": "Deeded access member"}], "default": "full"}', 'migration')
on conflict (key) do nothing;
