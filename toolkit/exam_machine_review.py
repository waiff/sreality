"""The definition-driven machine review of exam answers (migration 467).

The exam's suggestions (461) were computed from tag NAMES only, before any
definition existed; the operator then wrote eighteen definitions and ratified
one ruleset (what the photo is an image OF, in three tiers). This pass asks the
model the exam's own question WITH those definitions — one call per image, all
eighteen at once, answering yes / no / skip per tag — and stores the verdicts
beside the human's, so the review page can show where the two disagree and the
operator can accept or dismiss each proposal.

Machine verdicts are NEVER labels: they live in their own table, train nothing,
grade nothing, and the holdout census does not apply to them. The only way a
proposal becomes a label is the operator pressing "apply" on the review page,
which re-answers the whole image through the exam's single write path.

PROVENANCE FROZEN AT CALL TIME. A verdict written against definition v3 says
nothing about v4, and a verdict on a 12-tag list says nothing about the 18-tag
version. Each row carries the asked list and the exact definition versions it
answered against; `members_needing_review` re-offers a row whose provenance no
longer matches the current set + active definitions, so re-dispatching the lane
after a definition edit is the whole refresh mechanism.

DISMISSALS RESET ON RE-REVIEW. A dismissal says "I keep my answer against THAT
verdict"; a re-review under new wording is a new verdict, so it is offered
again. Anything else would hide a disagreement the new definitions created.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

LOG = logging.getLogger(__name__)

VERDICTS = ("yes", "no", "skip")

CALLED_FOR = "review_exam_image"

_INTRO = (
    "You are reviewing a real-estate photo against the labeling definitions below. "
    "For EVERY tag, decide from its definition whether this photo is an image OF "
    "that tag. Read each definition's COUNTS, DOES NOT COUNT, EASILY CONFUSED WITH "
    "and LEAVE OUT lists: they are the ruling on every borderline case.\n\n"
)


def build_prompt(definitions: list[dict[str, Any]]) -> str:
    """Every definition rendered as its block, the three-tier rule stated ONCE
    after all of them, then the JSON contract. `definitions` are
    (tag_id, label, definition-dict) triples in exam key order."""
    from toolkit.tag_definition_render import THREE_TIER_RULE, render_prompt_block

    blocks = []
    for entry in definitions:
        block = render_prompt_block(entry["definition"], tag_label=entry["label"])
        blocks.append(f"[TAG ID {entry['tag_id']}]\n{block}")
    ids = ", ".join(str(e["tag_id"]) for e in definitions)
    return (
        _INTRO
        + "\n\n".join(blocks)
        + "\n\n"
        + THREE_TIER_RULE
        + "\n\nReply with ONE JSON object and nothing else: "
        '{"verdicts": {"<tag id>": "yes" | "no" | "skip", ...}} '
        f"with an entry for EVERY tag id ({ids}). A missing entry is an error, not a no."
    )


def parse_verdicts(text: str, *, valid_ids: set[int]) -> dict[int, str]:
    """{tag_id: verdict} from the reply. Raises on anything short of a full,
    well-formed answer — an unparseable reply is recorded as an ERROR (absence
    of evidence), never as a row of no's."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw.lstrip("`")
        raw = raw.split("\n", 1)[1] if raw.lower().startswith("json") else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in review reply: {text[:120]!r}")
    doc = json.loads(raw[start:end + 1])
    verdicts = doc.get("verdicts")
    if not isinstance(verdicts, dict):
        raise ValueError(f"review reply has no 'verdicts' object: {text[:120]!r}")
    out: dict[int, str] = {}
    for key, value in verdicts.items():
        if not str(key).lstrip("-").isdigit():
            continue
        tag_id = int(key)
        if tag_id not in valid_ids:
            continue
        verdict = str(value).strip().lower()
        if verdict not in VERDICTS:
            raise ValueError(f"tag {tag_id}: verdict {value!r} is not yes/no/skip")
        out[tag_id] = verdict
    missing = sorted(valid_ids - set(out))
    if missing:
        raise ValueError(f"review reply is missing tags {missing}")
    return out


_ACTIVE_DEFINITIONS_SQL = """
    SELECT o.tag_id, t.label, d.version
    FROM unnest(%(tag_ids)s::bigint[]) WITH ORDINALITY AS o (tag_id, pos)
    JOIN tag_taxonomy t ON t.id = o.tag_id AND t.active
    LEFT JOIN tag_definitions d ON d.tag_id = o.tag_id AND d.status = 'active'
    ORDER BY o.pos
"""


def active_definitions(
    conn: psycopg.Connection, *, tag_ids: list[int],
) -> list[dict[str, Any]]:
    """(tag_id, label, version, definition) in exam key order. A tag WITHOUT an
    active definition raises: reviewing against a name alone is exactly what the
    old suggestions did, and this pass exists to stop doing that."""
    from toolkit import tag_definitions as td

    with conn.cursor() as cur:
        cur.execute(_ACTIVE_DEFINITIONS_SQL, {"tag_ids": list(tag_ids)})
        rows = cur.fetchall()
    undefined = [int(r[0]) for r in rows if r[2] is None]
    if undefined:
        raise ValueError(f"tags without an active definition: {undefined}")
    out = []
    for tag_id, label, version in rows:
        definition = td.get_active_definition(conn, tag_id=int(tag_id))
        out.append({"tag_id": int(tag_id), "label": label,
                    "version": int(version), "definition": definition})
    return out


def versions_of(definitions: list[dict[str, Any]]) -> dict[str, int]:
    return {str(e["tag_id"]): int(e["version"]) for e in definitions}


# Members whose review is missing, errored, or STALE — its frozen asked list
# or definition versions no longer equal the current ones. jsonb equality is
# key-order-insensitive, so the versions map compares as a set.
_MEMBERS_NEEDING_REVIEW_SQL = """
    SELECT m.image_id, i.storage_path
    FROM tag_exam_members m
    JOIN images i ON i.id = m.image_id AND i.storage_path IS NOT NULL
    WHERE m.cohort_id = %(cohort_id)s
      AND NOT EXISTS (
            SELECT 1 FROM tag_exam_machine_reviews r
            WHERE r.cohort_id = m.cohort_id AND r.image_id = m.image_id
              AND r.error IS NULL
              AND r.asked_tag_ids <@ %(tag_ids)s::bigint[]
              AND r.asked_tag_ids @> %(tag_ids)s::bigint[]
              AND r.definition_versions = %(versions)s::jsonb
          )
    ORDER BY m.position
    LIMIT %(limit)s
"""

_UPSERT_REVIEW_SQL = """
    INSERT INTO tag_exam_machine_reviews (
      cohort_id, image_id, asked_tag_ids, definition_versions, verdicts,
      model, error, dismissed_tag_ids
    )
    VALUES (%(cohort_id)s, %(image_id)s, %(asked_tag_ids)s, %(versions)s::jsonb,
            %(verdicts)s::jsonb, %(model)s, %(error)s, '{}'::bigint[])
    ON CONFLICT (cohort_id, image_id) DO UPDATE
      SET asked_tag_ids = EXCLUDED.asked_tag_ids,
          definition_versions = EXCLUDED.definition_versions,
          verdicts = EXCLUDED.verdicts,
          model = EXCLUDED.model,
          error = EXCLUDED.error,
          dismissed_tag_ids = '{}'::bigint[],
          reviewed_at = now()
"""

# Current reviews for one cohort, served beside the answers. Stale rows are
# NOT served: a verdict against an older wording would look like a proposal
# under the current one.
_REVIEWS_FOR_ANSWERS_SQL = """
    SELECT r.image_id, r.verdicts, r.dismissed_tag_ids, r.reviewed_at
    FROM tag_exam_machine_reviews r
    WHERE r.cohort_id = %(cohort_id)s AND r.error IS NULL
      AND r.asked_tag_ids <@ %(tag_ids)s::bigint[]
      AND r.asked_tag_ids @> %(tag_ids)s::bigint[]
      AND r.definition_versions = %(versions)s::jsonb
"""

_DISMISS_SQL = """
    UPDATE tag_exam_machine_reviews
       SET dismissed_tag_ids = (
             SELECT array_agg(DISTINCT x ORDER BY x)
             FROM unnest(dismissed_tag_ids || %(tag_id)s::bigint) AS u (x))
     WHERE cohort_id = %(cohort_id)s AND image_id = %(image_id)s
    RETURNING dismissed_tag_ids
"""

_CURRENT_VERSIONS_SQL = """
    SELECT o.tag_id, d.version
    FROM unnest(%(tag_ids)s::bigint[]) AS o (tag_id)
    LEFT JOIN tag_definitions d ON d.tag_id = o.tag_id AND d.status = 'active'
"""


def current_versions(conn: psycopg.Connection, *, tag_ids: list[int]) -> dict[str, int]:
    """The active definition version per tag, for the staleness comparison —
    cheap (no definition bodies), so the answers read can afford it."""
    with conn.cursor() as cur:
        cur.execute(_CURRENT_VERSIONS_SQL, {"tag_ids": list(tag_ids)})
        return {str(int(r[0])): int(r[1]) for r in cur.fetchall() if r[1] is not None}


def members_needing_review(
    conn: psycopg.Connection, *, cohort_id: int, tag_ids: list[int],
    versions: dict[str, int], limit: int,
) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(_MEMBERS_NEEDING_REVIEW_SQL, {
            "cohort_id": cohort_id, "tag_ids": list(tag_ids),
            "versions": json.dumps(versions), "limit": limit,
        })
        return [(int(r[0]), r[1]) for r in cur.fetchall()]


def record_review(
    conn: psycopg.Connection, *, cohort_id: int, image_id: int,
    asked_tag_ids: list[int], versions: dict[str, int],
    verdicts: dict[int, str] | None, model: str, error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(_UPSERT_REVIEW_SQL, {
            "cohort_id": cohort_id, "image_id": image_id,
            "asked_tag_ids": list(asked_tag_ids),
            "versions": json.dumps(versions),
            "verdicts": json.dumps({str(k): v for k, v in (verdicts or {}).items()}),
            "model": model, "error": error,
        })


def reviews_for_answers(
    conn: psycopg.Connection, *, cohort_id: int, tag_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """{image_id: {verdicts: {tag_id: verdict}, dismissed_tag_ids, reviewed_at}}
    for every member with a CURRENT review. Only the sitting's tags are served."""
    versions = current_versions(conn, tag_ids=tag_ids)
    if len(versions) != len(tag_ids):
        # Some tag has no active definition: no review can be current.
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(_REVIEWS_FOR_ANSWERS_SQL, {
                "cohort_id": cohort_id, "tag_ids": list(tag_ids),
                "versions": json.dumps(versions),
            })
            rows = cur.fetchall()
    except psycopg.errors.UndefinedTable:
        # A merge is not a deploy and a deploy is not an applied migration: this
        # read ships in the same PR as 467 and would otherwise take the whole
        # review page down for the window between the two. Narrow on purpose —
        # only the table's absence is tolerated, and only as "no reviews yet".
        # The API connection is autocommit, so the failed statement poisons
        # nothing after it.
        LOG.warning("tag_exam_machine_reviews is absent — migration 467 not applied yet")
        return {}
    current = set(tag_ids)
    out: dict[int, dict[str, Any]] = {}
    for image_id, verdicts, dismissed, reviewed_at in rows:
        out[int(image_id)] = {
            "verdicts": {int(k): v for k, v in (verdicts or {}).items()
                         if str(k).isdigit() and int(k) in current},
            "dismissed_tag_ids": sorted(int(x) for x in (dismissed or []) if int(x) in current),
            "reviewed_at": reviewed_at.isoformat() if reviewed_at else None,
        }
    return out


def dismiss_proposal(
    conn: psycopg.Connection, *, cohort_id: int, image_id: int, tag_id: int,
) -> list[int]:
    """The operator keeps their answer against the machine's on this one cell.
    Idempotent; raises KeyError when the image has no review row."""
    with conn.cursor() as cur:
        cur.execute(_DISMISS_SQL, {
            "cohort_id": cohort_id, "image_id": image_id, "tag_id": tag_id,
        })
        row = cur.fetchone()
    if row is None:
        raise KeyError(image_id)
    return [int(x) for x in (row[0] or [])]


_MEASURED_COST_SQL = """
    SELECT count(*)::int, COALESCE(avg(cost_usd), 0)::double precision
    FROM llm_calls
    WHERE called_for = %(called_for)s AND model = %(model)s AND cost_usd IS NOT NULL
"""


def measured_cost(conn: psycopg.Connection, *, model: str) -> tuple[int, float]:
    """This pass's OWN measured per-image cost. It carries eighteen definitions
    per call, so the screener's or suggester's cost is not its prior — the
    script applies a multiple to those until this has ten calls of its own."""
    with conn.cursor() as cur:
        cur.execute(_MEASURED_COST_SQL, {"called_for": CALLED_FOR, "model": model})
        row = cur.fetchone()
    return (int(row[0]), float(row[1])) if row else (0, 0.0)
