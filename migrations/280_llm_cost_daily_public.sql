-- 279: read model for the /costs LLM-spend dashboard.
--
-- Per-day × called_for × provider × model aggregates over llm_calls,
-- exposed to the anon SPA like the other operator-dashboard *_public
-- views. The view runs with owner rights, so anon reads ONLY the
-- aggregate — no grant on raw llm_calls. The day filter the page sends
-- (.gte on `day`) pushes down onto the called_at::date grouping key;
-- the full-table aggregate is comfortably under the anon 3 s
-- statement_timeout at current llm_calls volume (~300k rows), backed by
-- llm_calls_called_at_idx.

create or replace view llm_cost_daily_public as
select
  l.called_at::date                                   as day,
  l.called_for,
  l.provider,
  l.model,
  count(*)::int                                       as calls,
  (count(*) filter (where l.error is not null))::int  as error_calls,
  round(sum(l.cost_usd)::numeric, 4)                  as cost_usd,
  sum(l.input_tokens)::bigint                         as input_tokens,
  sum(l.output_tokens)::bigint                        as output_tokens,
  sum(l.cache_read_tokens)::bigint                    as cache_read_tokens,
  sum(l.cache_write_tokens)::bigint                   as cache_write_tokens
from llm_calls l
group by 1, 2, 3, 4;

grant select on llm_cost_daily_public to anon, authenticated;
