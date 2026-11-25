-- Cheap re-crawls: remember what the server told us and what we downloaded, so unchanged
-- documents can be skipped before downloading (304) or before converting (same bytes).
alter table documents
  add column if not exists http_etag          text,
  add column if not exists http_last_modified text,
  add column if not exists raw_hash           text,        -- sha256 of the downloaded bytes
  add column if not exists links              text[],      -- outgoing links, so a 304 page can still be crawled through
  add column if not exists verified_at        timestamptz; -- last full download (forces one every 30 days)
