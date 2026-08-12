"""One config row per portal for broker attribution — the registry behind rule #21.

Attribution is the ONLY source-specific step of the broker resolver: firms,
singletons, rollups, memberships, merges and the leaderboard matview are all
source-agnostic. Four statement SHAPES cover every portal —

  1. upsert `broker_identities` (latest-wins per column, keyed on the portal's
     own broker id),
  2. upsert the email contact,
  3. upsert the phone contact,
  4. point `listings.broker_identity_id` at the identity —

and what actually differs between portals is a handful of JSON keys plus three
quirks (phone shape, phone normalisation, chunk materialisation). Onboarding a
portal is therefore ONE row here, not four hand-copied statements; the five
pre-registry sources carried ~330 lines of near-identical SQL between them.

Read by `scripts.resolve_brokers` (which executes the statements) and by
`scraper.db` (which sources enqueue into `dirty_broker_listings`, and which
`raw["broker"]` keys make a broker-only page change re-enqueue).
"""

from __future__ import annotations

from dataclasses import dataclass

# `{sel}` — a listings selector the caller substitutes per chunk (always
# `l.id = ANY(%(ids)s)`), so no attribution statement is ever unbounded.
_IDENTITIES_TEMPLATE = """
WITH src AS (
  SELECT
    (l.raw_json->'{block}'->>'{id_key}') AS uid,
    nullif(l.raw_json->'{block}'->>'{name_key}', '') AS name,
    {email_expr} AS email,
    {rating_expr} AS rating,
    {reviews_expr} AS reviews,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = '{source}' AND l.raw_json ? '{block}'
    AND (l.raw_json->'{block}'->>'{id_key}') IS NOT NULL
    AND {{sel}}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (
  SELECT DISTINCT ON (uid) uid, name, email, rating, reviews
  FROM src ORDER BY uid, last_seen_at DESC NULLS LAST
)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, rating, review_count,
   first_seen_at, last_seen_at, attrs_computed_at)
SELECT '{source}', a.uid, lt.name, lt.email, lt.rating, lt.reviews, a.fseen, a.lseen, now()
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
"""

_CONTACT_EMAIL_TEMPLATE = """
WITH chunk AS {cte_mode} (
  SELECT (l.raw_json->'{block}'->>'{id_key}') AS uid,
         lower(nullif(l.raw_json->'{block}'->>'{email_key}', '')) AS email,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = '{source}' AND l.raw_json ? '{block}'
    AND nullif(l.raw_json->'{block}'->>'{email_key}', '') IS NOT NULL AND {{sel}}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, '{source}', 'email', c.email, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = '{source}' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.email
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
"""

_CONTACT_PHONE_TEMPLATE = """
WITH chunk AS {cte_mode} (
  SELECT (l.raw_json->'{block}'->>'{id_key}') AS uid, {phone_expr} AS phone,
         l.first_seen_at, l.last_seen_at
  {phone_from}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, '{source}', 'phone', c.phone, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = '{source}' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.phone
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
"""

# An array-of-objects phone field (sreality's `user_phones`), exploded one row per
# number; the >=9-digit filter lives inside the lateral so a stub entry is dropped
# rather than written as a truncated contact.
_PHONE_ARRAY_FROM = """FROM listings l
  CROSS JOIN LATERAL (
    SELECT {norm} AS norm
    FROM (
      SELECT regexp_replace(p->>'phone', '[^0-9]', '', 'g') AS digits
      FROM jsonb_array_elements(coalesce(l.raw_json->'{block}'->'{phone_array_key}', '[]'::jsonb)) p
    ) d
    WHERE length(d.digits) >= 9
  ) ph
  WHERE l.source = '{source}' AND l.raw_json ? '{block}' AND {{sel}}"""

_PHONE_SCALAR_FROM = """FROM listings l
  WHERE l.source = '{source}' AND l.raw_json ? '{block}'
    AND length(regexp_replace(coalesce(l.raw_json->'{block}'->>'{phone_key}', ''), '[^0-9]', '', 'g')) >= 9
    AND {{sel}}"""

_LINK_TEMPLATE = """
UPDATE listings l SET broker_identity_id = bi.id
FROM broker_identities bi
WHERE bi.source = '{source}' AND bi.source_broker_id_native = (l.raw_json->'{block}'->>'{id_key}')
  AND l.source = '{source}' AND l.raw_json ? '{block}'
  AND (l.raw_json->'{block}'->>'{id_key}') IS NOT NULL
  AND l.broker_identity_id IS DISTINCT FROM bi.id AND {{sel}}
"""

_SCALAR_DIGITS = "regexp_replace(l.raw_json->'{block}'->>'{phone_key}', '[^0-9]', '', 'g')"


@dataclass(frozen=True)
class BrokerSource:
    """Where one portal keeps its broker block, and which quirks it carries."""

    source: str
    block: str                            # raw_json key holding the broker object
    id_key: str                           # the portal's own stable broker key
    name_key: str
    email_key: str | None = None
    phone_key: str | None = None          # a scalar phone string
    phone_array_key: str | None = None    # ...or an array of objects with a "phone"
    # A bare 9-digit national number gains '420', matching toolkit.broker_resolver
    # .normalize_phone. idnes stores bare digits — a pre-existing divergence kept
    # here rather than silently rewritten, because its contacts are already stored
    # that way and a flip would orphan every existing row.
    phone_prefix_420: bool = False
    rating_key: str | None = None
    review_count_key: str | None = None
    # A single corporate switchboard is not a contact: writing it would stamp one
    # number and one inbox onto every broker the portal has. The identity (and its
    # email_domain, the only firm key) still resolves.
    write_contacts: bool = True
    # Bound the listings scan by {sel} BEFORE the join to broker_identities, or a
    # cold planner inverts the join and detoasts far more raw_json than the chunk,
    # blowing the statement timeout. sreality predates the fix and keeps the
    # inlined plan (NOT MATERIALIZED reproduces its direct join).
    materialize_chunk: bool = True
    # The portal's agency/firm fields. Attribution never reads them (firms key off
    # email_domain); they exist so a firm swap re-enqueues the listing.
    firm_keys: tuple[str, ...] = ()

    @property
    def writes_email_contact(self) -> bool:
        return self.write_contacts and self.email_key is not None

    @property
    def writes_phone_contact(self) -> bool:
        return self.write_contacts and bool(self.phone_key or self.phone_array_key)

    def fingerprint_keys(self) -> tuple[str, ...]:
        """The raw["broker"] keys whose change must re-enqueue the listing."""
        keys = (self.id_key, self.name_key, self.email_key, self.phone_key,
                self.phone_array_key, *self.firm_keys)
        return tuple(k for k in keys if k)

    def statements(self) -> tuple[str, ...]:
        """This source's attribution SQL, each still carrying one `{sel}` slot."""
        common = {"source": self.source, "block": self.block, "id_key": self.id_key}
        cte_mode = "MATERIALIZED" if self.materialize_chunk else "NOT MATERIALIZED"
        out = [_IDENTITIES_TEMPLATE.format(
            name_key=self.name_key,
            email_expr=(f"lower(nullif(l.raw_json->'{self.block}'->>'{self.email_key}', ''))"
                        if self.email_key else "NULL::text"),
            rating_expr=(f"nullif(l.raw_json->'{self.block}'->>'{self.rating_key}', '')::numeric"
                         if self.rating_key else "NULL::numeric"),
            reviews_expr=(f"nullif(l.raw_json->'{self.block}'->>'{self.review_count_key}', '')::int"
                          if self.review_count_key else "NULL::int"),
            **common)]
        if self.writes_email_contact:
            out.append(_CONTACT_EMAIL_TEMPLATE.format(
                cte_mode=cte_mode, email_key=self.email_key, **common))
        if self.writes_phone_contact:
            out.append(_CONTACT_PHONE_TEMPLATE.format(
                cte_mode=cte_mode, **common, **self._phone_parts()))
        out.append(_LINK_TEMPLATE.format(**common))
        return tuple(out)

    def _phone_parts(self) -> dict[str, str]:
        if self.phone_array_key:
            norm = ("CASE WHEN length(d.digits) = 9 THEN '420' || d.digits ELSE d.digits END"
                    if self.phone_prefix_420 else "d.digits")
            return {
                "phone_expr": "ph.norm",
                "phone_from": _PHONE_ARRAY_FROM.format(
                    norm=norm, source=self.source, block=self.block,
                    phone_array_key=self.phone_array_key),
            }
        digits = _SCALAR_DIGITS.format(block=self.block, phone_key=self.phone_key)
        expr = (f"CASE WHEN length({digits}) = 9 THEN '420' || {digits} ELSE {digits} END"
                if self.phone_prefix_420 else digits)
        return {
            "phone_expr": expr,
            "phone_from": _PHONE_SCALAR_FROM.format(
                source=self.source, block=self.block, phone_key=self.phone_key),
        }


# Order is the execution order of the full sweep's per-chunk attribution and the
# `source = ANY(...)` scan that enumerates it. Append, never reorder.
BROKER_SOURCES: tuple[BrokerSource, ...] = (
    # The JSON v1 API's own user object: an array of phone objects, and the only
    # portal that publishes a broker rating.
    BrokerSource(
        source="sreality", block="user", id_key="user_id", name_key="user_name",
        email_key="user_email", phone_array_key="user_phones", phone_prefix_420=True,
        rating_key="broker_rating", review_count_key="broker_review_count",
        materialize_chunk=False,
    ),
    # account_oid is the per-broker key; agency_name is the only friendly firm
    # label any portal publishes (_FIRM_DISPLAY_NAMES reads it).
    BrokerSource(
        source="idnes", block="broker", id_key="account_oid", name_key="name",
        email_key="email", phone_key="phone", firm_keys=("agency_name",),
    ),
    # PHONE-ONLY: the site hides the broker email behind a form, so no email ->
    # no email_domain -> no firm linkage (an accepted gap).
    BrokerSource(
        source="ceskereality", block="broker", id_key="broker_id", name_key="name",
        phone_key="phone", phone_prefix_420=True, firm_keys=("agency_slug",),
    ),
    # IDENTITY-ONLY: the phone sits behind a /trackredir click and the email behind
    # a form, so there is nothing contactable to store.
    BrokerSource(
        source="realitymix", block="broker", id_key="broker_id", name_key="name",
        firm_keys=("agency_id",),
    ),
    # EMAIL-ONLY: broker_phone is an intentional zero on every RE/MAX page. Email
    # matters beyond contact detail — re-max.cz already exists as an is_franchise
    # firm, so these identities join it rather than minting a new one.
    BrokerSource(
        source="remax", block="broker", id_key="broker_id", name_key="name",
        email_key="email", firm_keys=("agency_slug",),
    ),
    # IDENTITY-ONLY, but for the opposite reason to realitymix: every one of the
    # 1,021 MM Reality brokers publishes the SAME switchboard email and phone
    # (verified: 1 distinct value each across 10,652 listings). Storing them would
    # put the switchboard in brokers.primary_phone for every MM broker and add
    # 10k contact rows that the bridge frequency guard discards anyway. The
    # identity still carries the email, which is what links them to the existing
    # mmreality.cz franchise firm.
    BrokerSource(
        source="mmreality", block="broker", id_key="id", name_key="name",
        email_key="email", phone_key="phone", phone_prefix_420=True,
        write_contacts=False,
    ),
)

BROKER_SOURCE_NAMES: tuple[str, ...] = tuple(c.source for c in BROKER_SOURCES)

# The union of every attribution-relevant key of the portals that keep their block
# at raw["broker"] — which is the only block scraper.db reads back to diff. An
# allowlist, not a hash of the whole block, so a parser adding an incidental field
# can't churn the dirty queue.
BROKER_FINGERPRINT_KEYS: tuple[str, ...] = tuple(sorted(
    {k for cfg in BROKER_SOURCES if cfg.block == "broker" for k in cfg.fingerprint_keys()}
))


def attribution_statements() -> tuple[str, ...]:
    """Every source's attribution SQL, in registry order, `{sel}` unresolved."""
    return tuple(sql for cfg in BROKER_SOURCES for sql in cfg.statements())
