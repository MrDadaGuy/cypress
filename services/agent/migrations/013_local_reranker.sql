-- Free local reranker (FlashRank, TinyBERT cross-encoder, baked into the API image). It runs on the
-- API's own CPU, so it costs nothing per search. Only one reranker is on at a time; the Vertex ranker
-- is switched off because the API's service account can't call it yet (403 in production).
alter table llm_models drop constraint if exists llm_models_provider_check;
alter table llm_models add constraint llm_models_provider_check
  check (provider in ('gemini', 'claude', 'maas', 'vertex_search', 'local'));

insert into llm_models (id, kind, provider, name, region, label, input_per_m, output_per_m, per_call_usd,
                        price_source, enabled, note)
values ('flashrank-tinybert', 'rerank', 'local', 'ms-marco-TinyBERT-L-2-v2', 'local', 'FlashRank TinyBERT (local)',
        0, 0, 0, 'Runs on the API container: no per-call cost', true,
        'Free reranker on the API''s CPU (~50 ms per search). Matched the Vertex ranker''s top results in testing')
on conflict (id) do nothing;

update llm_models set enabled = false,
  note = 'Needs the Discovery Engine viewer role on the API service account (403 in production). $1 per 1,000 searches'
where id = 'semantic-ranker' and updated_by = 'migration';
