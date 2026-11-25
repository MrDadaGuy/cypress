-- Cloud Trace id of each chat turn, so the Conversations page can link to the trace.
alter table interactions add column if not exists trace_id text;
