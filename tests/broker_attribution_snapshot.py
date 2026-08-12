"""Frozen SQL snapshot for the broker attribution registry — the equivalence baseline.

`PRE_REGISTRY_SQL` is a verbatim copy of the five hand-written attribution
families that lived in `scripts/resolve_brokers.py` up to origin/main 6d034d2c,
one entry per statement in execution order. It is a literal transcript, not a
summary of properties: `tests/test_broker_sources.py` renders the registry and
compares the WHOLE statement text against it, so any edit to a template — a
dropped predicate, a swapped JSON key, a lost `IS DISTINCT FROM` — fails loudly
instead of slipping past a substring check.

Nine of the sixteen statements are reproduced byte-for-byte (after whitespace
normalisation). The seven that are not are frozen separately in `REGISTRY_DELTAS`
— the registry's own rendered text — and each is explained by one of the three
documented, verified-equivalent deviations in the test module's docstring.
"""

from __future__ import annotations

# (source, statement kind) -> the pre-registry SQL, verbatim, `{sel}` unresolved.
PRE_REGISTRY_SQL: dict[tuple[str, str], str] = {
    ("sreality", "identity"): """
WITH src AS (
  SELECT
    (l.raw_json->'user'->>'user_id')                           AS uid,
    nullif(l.raw_json->'user'->>'user_name', '')               AS name,
    lower(nullif(l.raw_json->'user'->>'user_email', ''))       AS email,
    nullif(l.raw_json->'user'->>'broker_rating', '')::numeric  AS rating,
    nullif(l.raw_json->'user'->>'broker_review_count', '')::int AS reviews,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'sreality' AND l.raw_json ? 'user'
    AND (l.raw_json->'user'->>'user_id') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (
  SELECT DISTINCT ON (uid) uid, name, email, rating, reviews
  FROM src ORDER BY uid, last_seen_at DESC NULLS LAST
)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, rating, review_count,
   first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'sreality', a.uid, lt.name, lt.email, lt.rating, lt.reviews, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  email        = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.email ELSE broker_identities.email END,
  rating       = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.rating ELSE broker_identities.rating END,
  review_count = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.review_count ELSE broker_identities.review_count END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
""",
    ("sreality", "email"): """
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'sreality', 'email', lower(nullif(l.raw_json->'user'->>'user_email', '')),
       min(l.first_seen_at), max(l.last_seen_at)
FROM listings l
JOIN broker_identities bi
  ON bi.source = 'sreality' AND bi.source_broker_id_native = (l.raw_json->'user'->>'user_id')
WHERE l.source = 'sreality' AND l.raw_json ? 'user'
  AND nullif(l.raw_json->'user'->>'user_email', '') IS NOT NULL AND {sel}
GROUP BY bi.id, lower(nullif(l.raw_json->'user'->>'user_email', ''))
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
""",
    ("sreality", "phone"): """
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'sreality', 'phone', ph.norm, min(l.first_seen_at), max(l.last_seen_at)
FROM listings l
JOIN broker_identities bi
  ON bi.source = 'sreality' AND bi.source_broker_id_native = (l.raw_json->'user'->>'user_id')
CROSS JOIN LATERAL (
  SELECT CASE WHEN length(d.digits) = 9 THEN '420' || d.digits ELSE d.digits END AS norm
  FROM (
    SELECT regexp_replace(p->>'phone', '[^0-9]', '', 'g') AS digits
    FROM jsonb_array_elements(coalesce(l.raw_json->'user'->'user_phones', '[]'::jsonb)) p
  ) d
  WHERE length(d.digits) >= 9
) ph
WHERE l.source = 'sreality' AND l.raw_json ? 'user' AND {sel}
GROUP BY bi.id, ph.norm
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
""",
    ("sreality", "link"): """
UPDATE listings l SET broker_identity_id = bi.id
FROM broker_identities bi
WHERE bi.source = 'sreality' AND bi.source_broker_id_native = (l.raw_json->'user'->>'user_id')
  AND l.source = 'sreality' AND l.raw_json ? 'user'
  AND (l.raw_json->'user'->>'user_id') IS NOT NULL
  AND l.broker_identity_id IS DISTINCT FROM bi.id AND {sel}
""",
    ("idnes", "identity"): """
WITH src AS (
  SELECT
    (l.raw_json->'broker'->>'account_oid')            AS uid,
    nullif(l.raw_json->'broker'->>'name', '')          AS name,
    lower(nullif(l.raw_json->'broker'->>'email', ''))  AS email,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'idnes' AND l.raw_json ? 'broker'
    AND (l.raw_json->'broker'->>'account_oid') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (SELECT DISTINCT ON (uid) uid, name, email FROM src ORDER BY uid, last_seen_at DESC NULLS LAST)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'idnes', a.uid, lt.name, lt.email, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  email        = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.email ELSE broker_identities.email END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
""",
    ("idnes", "email"): """
WITH chunk AS MATERIALIZED (
  SELECT (l.raw_json->'broker'->>'account_oid') AS uid,
         lower(nullif(l.raw_json->'broker'->>'email', '')) AS email,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'idnes' AND l.raw_json ? 'broker'
    AND nullif(l.raw_json->'broker'->>'email', '') IS NOT NULL AND {sel}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'idnes', 'email', c.email, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = 'idnes' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.email
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
""",
    ("idnes", "phone"): """
WITH chunk AS MATERIALIZED (
  SELECT (l.raw_json->'broker'->>'account_oid') AS uid,
         regexp_replace(l.raw_json->'broker'->>'phone', '[^0-9]', '', 'g') AS phone,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'idnes' AND l.raw_json ? 'broker'
    AND length(regexp_replace(coalesce(l.raw_json->'broker'->>'phone', ''), '[^0-9]', '', 'g')) >= 9
    AND {sel}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'idnes', 'phone', c.phone, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = 'idnes' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.phone
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
""",
    ("idnes", "link"): """
UPDATE listings l SET broker_identity_id = bi.id
FROM broker_identities bi
WHERE bi.source = 'idnes' AND bi.source_broker_id_native = (l.raw_json->'broker'->>'account_oid')
  AND l.source = 'idnes' AND l.raw_json ? 'broker'
  AND (l.raw_json->'broker'->>'account_oid') IS NOT NULL
  AND l.broker_identity_id IS DISTINCT FROM bi.id AND {sel}
""",
    ("ceskereality", "identity"): """
WITH src AS (
  SELECT
    (l.raw_json->'broker'->>'broker_id')       AS uid,
    nullif(l.raw_json->'broker'->>'name', '')   AS name,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'ceskereality' AND l.raw_json ? 'broker'
    AND (l.raw_json->'broker'->>'broker_id') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (SELECT DISTINCT ON (uid) uid, name FROM src ORDER BY uid, last_seen_at DESC NULLS LAST)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'ceskereality', a.uid, lt.name, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
""",
    ("ceskereality", "phone"): """
WITH chunk AS MATERIALIZED (
  SELECT (l.raw_json->'broker'->>'broker_id') AS uid,
         regexp_replace(l.raw_json->'broker'->>'phone', '[^0-9]', '', 'g') AS digits,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'ceskereality' AND l.raw_json ? 'broker'
    AND length(regexp_replace(coalesce(l.raw_json->'broker'->>'phone', ''), '[^0-9]', '', 'g')) >= 9
    AND {sel}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'ceskereality', 'phone',
       CASE WHEN length(c.digits) = 9 THEN '420' || c.digits ELSE c.digits END,
       min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = 'ceskereality' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, CASE WHEN length(c.digits) = 9 THEN '420' || c.digits ELSE c.digits END
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
""",
    ("ceskereality", "link"): """
UPDATE listings l SET broker_identity_id = bi.id
FROM broker_identities bi
WHERE bi.source = 'ceskereality' AND bi.source_broker_id_native = (l.raw_json->'broker'->>'broker_id')
  AND l.source = 'ceskereality' AND l.raw_json ? 'broker'
  AND (l.raw_json->'broker'->>'broker_id') IS NOT NULL
  AND l.broker_identity_id IS DISTINCT FROM bi.id AND {sel}
""",
    ("realitymix", "identity"): """
WITH src AS (
  SELECT
    (l.raw_json->'broker'->>'broker_id')       AS uid,
    nullif(l.raw_json->'broker'->>'name', '')   AS name,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'realitymix' AND l.raw_json ? 'broker'
    AND (l.raw_json->'broker'->>'broker_id') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (SELECT DISTINCT ON (uid) uid, name FROM src ORDER BY uid, last_seen_at DESC NULLS LAST)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'realitymix', a.uid, lt.name, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
""",
    ("realitymix", "link"): """
UPDATE listings l SET broker_identity_id = bi.id
FROM broker_identities bi
WHERE bi.source = 'realitymix' AND bi.source_broker_id_native = (l.raw_json->'broker'->>'broker_id')
  AND l.source = 'realitymix' AND l.raw_json ? 'broker'
  AND (l.raw_json->'broker'->>'broker_id') IS NOT NULL
  AND l.broker_identity_id IS DISTINCT FROM bi.id AND {sel}
""",
    ("remax", "identity"): """
WITH src AS (
  SELECT
    (l.raw_json->'broker'->>'broker_id')               AS uid,
    nullif(l.raw_json->'broker'->>'name', '')          AS name,
    lower(nullif(l.raw_json->'broker'->>'email', ''))  AS email,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'remax' AND l.raw_json ? 'broker'
    AND (l.raw_json->'broker'->>'broker_id') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (SELECT DISTINCT ON (uid) uid, name, email FROM src ORDER BY uid, last_seen_at DESC NULLS LAST)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'remax', a.uid, lt.name, lt.email, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  email        = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.email ELSE broker_identities.email END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
""",
    ("remax", "email"): """
WITH chunk AS MATERIALIZED (
  SELECT (l.raw_json->'broker'->>'broker_id') AS uid,
         lower(nullif(l.raw_json->'broker'->>'email', '')) AS email,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'remax' AND l.raw_json ? 'broker'
    AND nullif(l.raw_json->'broker'->>'email', '') IS NOT NULL AND {sel}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'remax', 'email', c.email, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = 'remax' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.email
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
""",
    ("remax", "link"): """
UPDATE listings l SET broker_identity_id = bi.id
FROM broker_identities bi
WHERE bi.source = 'remax' AND bi.source_broker_id_native = (l.raw_json->'broker'->>'broker_id')
  AND l.source = 'remax' AND l.raw_json ? 'broker'
  AND (l.raw_json->'broker'->>'broker_id') IS NOT NULL
  AND l.broker_identity_id IS DISTINCT FROM bi.id AND {sel}
""",
}

# The registry's rendered text for the seven statements that are NOT byte-identical
# to the pre-registry family above. Frozen so a template mutation cannot hide here
# either; the deviations themselves are pinned by name in the test module.
REGISTRY_DELTAS: dict[tuple[str, str], str] = {
    ("sreality", "email"): """
WITH chunk AS NOT MATERIALIZED (
  SELECT (l.raw_json->'user'->>'user_id') AS uid,
         lower(nullif(l.raw_json->'user'->>'user_email', '')) AS email,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'sreality' AND l.raw_json ? 'user'
    AND nullif(l.raw_json->'user'->>'user_email', '') IS NOT NULL AND {sel}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'sreality', 'email', c.email, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = 'sreality' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.email
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
""",
    ("sreality", "phone"): """
WITH chunk AS NOT MATERIALIZED (
  SELECT (l.raw_json->'user'->>'user_id') AS uid, ph.norm AS phone,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  CROSS JOIN LATERAL (
    SELECT CASE WHEN length(d.digits) = 9 THEN '420' || d.digits ELSE d.digits END AS norm
    FROM (
      SELECT regexp_replace(p->>'phone', '[^0-9]', '', 'g') AS digits
      FROM jsonb_array_elements(coalesce(l.raw_json->'user'->'user_phones', '[]'::jsonb)) p
    ) d
    WHERE length(d.digits) >= 9
  ) ph
  WHERE l.source = 'sreality' AND l.raw_json ? 'user' AND {sel}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'sreality', 'phone', c.phone, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = 'sreality' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.phone
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
""",
    ("idnes", "identity"): """
WITH src AS (
  SELECT
    (l.raw_json->'broker'->>'account_oid') AS uid,
    nullif(l.raw_json->'broker'->>'name', '') AS name,
    lower(nullif(l.raw_json->'broker'->>'email', '')) AS email,
    NULL::numeric AS rating,
    NULL::int AS reviews,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'idnes' AND l.raw_json ? 'broker'
    AND (l.raw_json->'broker'->>'account_oid') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (
  SELECT DISTINCT ON (uid) uid, name, email, rating, reviews
  FROM src ORDER BY uid, last_seen_at DESC NULLS LAST
)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, rating, review_count,
   first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'idnes', a.uid, lt.name, lt.email, lt.rating, lt.reviews, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  email        = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.email ELSE broker_identities.email END,
  rating       = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.rating ELSE broker_identities.rating END,
  review_count = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.review_count ELSE broker_identities.review_count END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
""",
    ("ceskereality", "identity"): """
WITH src AS (
  SELECT
    (l.raw_json->'broker'->>'broker_id') AS uid,
    nullif(l.raw_json->'broker'->>'name', '') AS name,
    NULL::text AS email,
    NULL::numeric AS rating,
    NULL::int AS reviews,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'ceskereality' AND l.raw_json ? 'broker'
    AND (l.raw_json->'broker'->>'broker_id') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (
  SELECT DISTINCT ON (uid) uid, name, email, rating, reviews
  FROM src ORDER BY uid, last_seen_at DESC NULLS LAST
)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, rating, review_count,
   first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'ceskereality', a.uid, lt.name, lt.email, lt.rating, lt.reviews, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  email        = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.email ELSE broker_identities.email END,
  rating       = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.rating ELSE broker_identities.rating END,
  review_count = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.review_count ELSE broker_identities.review_count END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
""",
    ("ceskereality", "phone"): """
WITH chunk AS MATERIALIZED (
  SELECT (l.raw_json->'broker'->>'broker_id') AS uid, CASE WHEN length(regexp_replace(l.raw_json->'broker'->>'phone', '[^0-9]', '', 'g')) = 9 THEN '420' || regexp_replace(l.raw_json->'broker'->>'phone', '[^0-9]', '', 'g') ELSE regexp_replace(l.raw_json->'broker'->>'phone', '[^0-9]', '', 'g') END AS phone,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'ceskereality' AND l.raw_json ? 'broker'
    AND length(regexp_replace(coalesce(l.raw_json->'broker'->>'phone', ''), '[^0-9]', '', 'g')) >= 9
    AND {sel}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'ceskereality', 'phone', c.phone, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = 'ceskereality' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.phone
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
""",
    ("realitymix", "identity"): """
WITH src AS (
  SELECT
    (l.raw_json->'broker'->>'broker_id') AS uid,
    nullif(l.raw_json->'broker'->>'name', '') AS name,
    NULL::text AS email,
    NULL::numeric AS rating,
    NULL::int AS reviews,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'realitymix' AND l.raw_json ? 'broker'
    AND (l.raw_json->'broker'->>'broker_id') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (
  SELECT DISTINCT ON (uid) uid, name, email, rating, reviews
  FROM src ORDER BY uid, last_seen_at DESC NULLS LAST
)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, rating, review_count,
   first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'realitymix', a.uid, lt.name, lt.email, lt.rating, lt.reviews, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  email        = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.email ELSE broker_identities.email END,
  rating       = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.rating ELSE broker_identities.rating END,
  review_count = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.review_count ELSE broker_identities.review_count END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
""",
    ("remax", "identity"): """
WITH src AS (
  SELECT
    (l.raw_json->'broker'->>'broker_id') AS uid,
    nullif(l.raw_json->'broker'->>'name', '') AS name,
    lower(nullif(l.raw_json->'broker'->>'email', '')) AS email,
    NULL::numeric AS rating,
    NULL::int AS reviews,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'remax' AND l.raw_json ? 'broker'
    AND (l.raw_json->'broker'->>'broker_id') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (
  SELECT DISTINCT ON (uid) uid, name, email, rating, reviews
  FROM src ORDER BY uid, last_seen_at DESC NULLS LAST
)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, rating, review_count,
   first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'remax', a.uid, lt.name, lt.email, lt.rating, lt.reviews, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  email        = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.email ELSE broker_identities.email END,
  rating       = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.rating ELSE broker_identities.rating END,
  review_count = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.review_count ELSE broker_identities.review_count END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
""",
}
