-- Board members as individual records: terms of office, plus contact and areas of responsibility
-- on the person. Board access follows the term: an active term gives the board role, and when the
-- last term ends a 'board' role lapses on its own (admin is kept, and flagged in the digest).
alter table people add column if not exists phone text;
alter table people add column if not exists responsibilities text;   -- free text; used to route tickets

create table if not exists board_terms (
  id          bigint generated always as identity primary key,
  person_id   bigint not null references people(id) on delete cascade,
  title       text not null default 'Director',          -- officer position: President, Treasurer, ...
  seat        text,                                       -- if the bylaws number seats
  how         text not null default 'elected' check (how in ('elected', 'appointed')),
  start_date  date not null,
  end_date    date not null,                              -- scheduled end of the term
  ended_on    date,                                       -- set when someone leaves early
  end_reason  text check (end_reason in ('resigned', 'removed', 'other')),
  notes       text,
  created_at  timestamptz not null default now(),
  check (end_date >= start_date),
  check (ended_on is null or ended_on >= start_date),
  check ((ended_on is null) = (end_reason is null))
);
create index if not exists board_terms_person on board_terms (person_id);

-- Each person's access as Cypress applies it. Dates are Austin dates.
create or replace view people_access as
select p.*,
       t.id is not null as on_board,
       t.title          as board_title,
       t.start_date     as term_start,
       t.end_date       as term_end,
       case when not p.active then null
            when p.role = 'admin' then 'admin'
            when t.id is not null then 'board'
            when p.role = 'board' and exists (select 1 from board_terms x where x.person_id = p.id) then null
            else p.role end as effective_role
from people p
left join lateral (
  select * from board_terms bt
  where bt.person_id = p.id and bt.ended_on is null
    and (now() at time zone 'America/Chicago')::date between bt.start_date and bt.end_date
  order by bt.start_date desc limit 1) t on true;
