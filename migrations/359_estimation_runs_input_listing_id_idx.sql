-- 359_estimation_runs_input_listing_id_idx.sql
-- Gate-2 wave-5 tail: the rent-estimation read chain (latest_rent_estimations_by_listing,
-- the /estimations list's locality-display LEFT JOIN) now matches preferring
-- estimation_runs.input_listing_id (the surrogate stamped by #914) over the legacy
-- input_sreality_id, which is NULL for a post-Gate-2 non-sreality subject. The column
-- (migration 324) never got its own index — only input_sreality_id did (migration 010) —
-- so the new equality join would fall back to a sequential scan. Purely additive.

create index if not exists estimation_runs_input_listing_id_idx
  on estimation_runs (input_listing_id)
  where input_listing_id is not null;
