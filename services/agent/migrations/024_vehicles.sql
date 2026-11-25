-- Vehicles registered to member households (imported from CAM's vehicle list until Vantaca sync exists),
-- and phone numbers on households. Private like the rest of the membership list: admins see them in the
-- console; staff, security and the board reach them only through the audited lookup, at their role's level.

create table if not exists vehicles (
  id           bigint generated always as identity primary key,
  household_id bigint references households (id) on delete set null,
  plate        text not null,                      -- as given, e.g. "ABC-1234"
  plate_norm   text not null check (plate_norm ~ '^[A-Z0-9]{1,10}$'),   -- "ABC1234"
  state        text not null default '',           -- "TX"; '' when unknown
  make         text,
  model        text,
  color        text,
  year         int check (year between 1900 and 2100),
  tag          text,                               -- parking sticker / permit / gate tag
  notes        text,
  account_ref  text,                               -- as given in the import, used to match the household
  address      text,                               -- as given in the import, used to match the household
  source       text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (plate_norm, state)
);
create index if not exists vehicles_household on vehicles (household_id);
create index if not exists vehicles_plate_prefix on vehicles (plate_norm text_pattern_ops);

alter table households add column if not exists phones text[] not null default '{}';
alter table member_lookups add column if not exists plate_q text;
