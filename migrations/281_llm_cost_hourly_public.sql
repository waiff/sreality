-- 281: hourly companion to llm_cost_daily_public (migration 280) for the
-- /costs dashboard's hour-grain chart toggle. Same shape, bucketed by
-- date_trunc('hour'); the page reads a ~48 h window via .gte('bucket', …).
-- Anon reads the aggregate only, never raw llm_calls.

create or replace view llm_cost_hourly_public as
select
  date_trunc('hour', l.called_at)                     as bucket,
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

grant select on llm_cost_hourly_public to anon, authenticated;
