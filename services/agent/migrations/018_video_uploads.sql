-- Videos on reports. Uploaded straight to the bucket with a one-time signed link, then confirmed.
alter table uploads add column if not exists kind text not null default 'photo' check (kind in ('photo', 'video'));
alter table uploads add column if not exists content_type text not null default 'image/jpeg';
alter table uploads add column if not exists status text not null default 'ready' check (status in ('pending', 'ready'));
alter table uploads alter column width drop not null;
alter table uploads alter column height drop not null;

insert into org_settings (key, value, updated_by)
values ('uploads', '{"videos": true, "max_video_mb": 100}', 'migration')
on conflict (key) do nothing;
