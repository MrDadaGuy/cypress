-- Dues balance from the Vantaca homeowner export: stored for later, used nowhere yet (not shown to the agent,
-- lookups, case files or any role other than direct database access).
alter table households
  add column if not exists dues_balance numeric(12, 2),
  add column if not exists dues_balance_at timestamptz;
