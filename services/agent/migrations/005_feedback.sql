-- Thumbs up/down on each turn, for auditing and improving answers.
-- public_id is what clients hold (unguessable), so nobody can rate turns that aren't theirs by
-- counting ids.
alter table interactions
  add column if not exists public_id        uuid not null default gen_random_uuid(),
  add column if not exists rating           smallint check (rating in (-1, 1)),
  add column if not exists feedback_comment text,
  add column if not exists feedback_by      text,
  add column if not exists feedback_at      timestamptz;
create unique index if not exists interactions_public_id on interactions (public_id);
create index if not exists interactions_rating on interactions (rating, at desc) where rating is not null;
