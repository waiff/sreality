-- 386_location_w1_claim_fingerprint_fn.sql
--
-- Location-data W1: promote `claim_fingerprint` from an inline expression in the
-- intake writer to ONE named SQL function.
--
-- WHY. 01-schema.md section 4.2.1 defines the fingerprint tuple; migration 382
-- declares `location_claims.claim_fingerprint bytea not null` with a UNIQUE
-- index over it. The value is therefore the dedup mechanism of an APPEND-ONLY
-- table: two producers computing it even one byte apart do not conflict — they
-- both insert, silently, forever. W1's claims-intake lane is the first producer;
-- W2's HTML re-mine, W3's snapshot backfill and the LLM lane are the next three,
-- and each of them would otherwise carry its own transcription of a 22-element
-- tuple. Declared here so there is exactly one definition to reuse and exactly
-- one place a future tuple change has to happen (which, because the fingerprint
-- is stored, is a new function + a re-fingerprint migration — never an edit).
--
-- ARGUMENTS ARE ALL `text`/`numeric`/`jsonb`/`geometry`, NOT the location enums.
-- The intake writer feeds this from `jsonb_to_recordset`, whose columns are text
-- before the cast to the enum happens in the INSERT's select list. Taking text
-- keeps the function byte-identical to the expression it replaces (an enum's
-- json output is the same string, but only if the cast is guaranteed to happen
-- first, and it is not).
--
-- IMMUTABLE, and it earns it: every argument type has an immutable output
-- function, ST_AsEWKB/encode/convert_to/sha256 are immutable, and the tuple is
-- TIME-FREE by design (no observed_at, no batch_id, no snapshot_id — 01 section
-- 4.2.1: values dedupe, occurrences are their own series). `location_value_norm`
-- is deliberately NOT called from in here: it is STABLE (unaccent is a
-- dictionary lookup), so the caller computes `value_norm` once and passes it in.
--
-- Backend/service-role only: a function's default ACL is EXECUTE TO PUBLIC and
-- anon/authenticated INHERIT it, so all three are revoked at the foot.

-- `set local`, not `set`: this file is applied inside a transaction, and a
-- session-scoped SET would leak the timeout onto whatever the pooled backend
-- serves next.
set local lock_timeout = '5s';

create function location_claim_fingerprint(
  p_listing_id               bigint,
  p_source                   text,
  p_source_id_native         text,
  p_claim_type               text,
  p_surface                  text,
  p_page_kind                text,
  p_extraction_method        text,
  p_extractor_id             text,
  p_extractor_version        text,
  p_contract_entry_id        bigint,
  p_value_norm               text,
  p_value_text               text,
  p_value_num                numeric,
  p_geom                     geometry,
  p_shape                    geometry,
  p_value_jsonb              jsonb,
  p_distance_m               integer,
  p_travel_mode              text,
  p_target_text              text,
  p_declared_precision_label text,
  p_declared_confidence      text,
  p_declared_radius_m        numeric,
  p_legacy_source_column     text
) returns bytea
language sql immutable as $fn$
  select sha256(convert_to(jsonb_build_array(
    p_listing_id, p_source, p_source_id_native,
    p_claim_type, p_surface, p_page_kind, p_extraction_method,
    p_extractor_id, p_extractor_version, p_contract_entry_id,
    coalesce(p_value_norm, p_value_text, ''),
    p_value_num, encode(ST_AsEWKB(p_geom), 'hex'), encode(ST_AsEWKB(p_shape), 'hex'),
    p_value_jsonb, p_distance_m, p_travel_mode, p_target_text,
    p_declared_precision_label, p_declared_confidence, p_declared_radius_m,
    p_legacy_source_column
  )::text, 'UTF8'))
$fn$;

comment on function location_claim_fingerprint(
  bigint, text, text, text, text, text, text, text, text, bigint, text, text,
  numeric, geometry, geometry, jsonb, integer, text, text, text, text, numeric, text) is
  'The ONE definition of location_claims.claim_fingerprint (01 section 4.2.1). Every '
  'claim producer — W1 intake, W2 HTML re-mine, W3 snapshot backfill, the LLM lane — '
  'must call this; the column carries a UNIQUE index, so a second transcription of the '
  'tuple would stop deduping instead of conflicting. TIME-FREE on purpose.';

-- `public` first: a function's default ACL is EXECUTE TO PUBLIC, which anon and
-- authenticated INHERIT, so revoking only the two named roles leaves it callable.
revoke execute on function location_claim_fingerprint(
  bigint, text, text, text, text, text, text, text, text, bigint, text, text,
  numeric, geometry, geometry, jsonb, integer, text, text, text, text, numeric, text)
  from public, anon, authenticated;
