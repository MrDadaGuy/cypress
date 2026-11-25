-- Abuse protection for the public chat: App Check, admin-editable rate limits, and a daily cap on
-- anonymous model spend. Settings live in org_settings 'protection'; blocks and throttles are logged
-- (visitors appear only as a short hash, never an IP) and App Check outcomes are counted per day.

create table if not exists protection_events (
  id        bigint generated always as identity primary key,
  at        timestamptz not null default now(),
  reason    text not null,              -- rate_hour | rate_day | app_check_missing | app_check_invalid | spend_cap
  endpoint  text not null,              -- chat | upload | feedback | ...
  visitor   text not null,              -- short hash of the rate-limit key
  enforced  boolean not null default true   -- false: monitor mode, the request was let through
);
create index if not exists protection_events_at on protection_events (at desc);

create table if not exists app_check_daily (
  day      date not null,
  outcome  text not null,               -- verified | missing | invalid
  n        int not null default 0,
  primary key (day, outcome)
);

insert into org_settings (key, value, updated_by) values
  ('protection', '{"app_check": "monitor", "anon_per_hour": 20, "anon_per_day": 60, "user_per_hour": 120, "user_per_day": 400, "anon_daily_usd": 2}', 'migration')
on conflict (key) do nothing;
