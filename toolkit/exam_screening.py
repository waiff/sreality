"""The vision screener, and the stratification it feeds (migration 459).

The screener does ONE cheap job: look at an image and say which of the routing
tags might apply. It is not labelling — its guesses never become training data and
the operator never sees them. It exists only so the exam can be ENRICHED: a random
250 images holds three or four garages, which cannot grade anything, while a
stratified 250 holds around sixteen.

TUNED FOR RECALL, NOT PRECISION. A false hit costs one slot in the enriched layer.
A miss costs coverage of the very tag the enrichment exists to reach. So the prompt
asks for anything that MIGHT apply.

STRATIFY, NEVER FILTER. Every screened image belongs to exactly one stratum and
every stratum keeps a non-zero draw probability — including `screen_none`. Dropping
that stratum would measure recall only over what the screener already found, so the
probe would be graded on the half it was handed.

THE PARTITION. An image guessed for several tags lands in the stratum of the
RAREST tag it was guessed for, rarity measured by hit count within this very screen.
That keeps the partition deterministic (each image in exactly one stratum, so p is
well defined) and spends the enrichment budget where the corpus is thinnest.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

SCREEN_NONE = "screen_none"


def build_prompt(tags: list[dict[str, Any]]) -> str:
    """The screener's instruction. Deliberately short: this is a cheap triage pass
    over thousands of images, not the careful judgement the operator makes."""
    listing = "\n".join(f"  {t['id']}: {t['label']}" for t in tags)
    return (
        "You are triaging real-estate photos so a human exam can be assembled.\n\n"
        "Which of these categories MIGHT this photo be a usable example of?\n"
        f"{listing}\n\n"
        "Answer with a JSON object: {\"ids\": [<category ids>]}. Use an empty list "
        "if none of them plausibly apply.\n"
        "Err towards INCLUDING a category you are unsure about: a wrong guess costs "
        "one slot, a missed one costs coverage of that category entirely."
    )


def parse_guess(text: str, *, valid_ids: set[int]) -> list[int]:
    """Ids from the screener's reply, dropping anything it invented.

    A reply that cannot be parsed raises: the caller records it as an ERROR, never
    as an empty guess. The two look identical downstream and mean opposite things —
    "the screener saw nothing" is evidence, "the screener failed" is its absence."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw.lstrip("`")
        raw = raw.split("\n", 1)[1] if raw.lower().startswith("json") else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in screener reply: {text[:120]!r}")
    doc = json.loads(raw[start:end + 1])
    ids = doc.get("ids")
    if not isinstance(ids, list):
        raise ValueError(f"screener reply has no 'ids' list: {text[:120]!r}")
    return sorted({int(i) for i in ids if isinstance(i, (int, str))
                   and str(i).lstrip("-").isdigit() and int(i) in valid_ids})


_SCREEN_ROWS_SQL = """
    SELECT s.image_id, s.guess_tag_ids
    FROM tag_exam_screens s
    WHERE s.cohort_id = %(cohort_id)s
      AND s.error IS NULL
      AND NOT EXISTS (
            SELECT 1 FROM tag_exam_members m WHERE m.image_id = s.image_id
          )
"""

_SCREEN_ERROR_COUNT_SQL = """
    SELECT count(*) FILTER (WHERE error IS NOT NULL)::int,
           count(*)::int
    FROM tag_exam_screens WHERE cohort_id = %(cohort_id)s
"""

_UPSERT_SCREEN_SQL = """
    INSERT INTO tag_exam_screens (cohort_id, image_id, guess_tag_ids, model, error)
    VALUES (%(cohort_id)s, %(image_id)s, %(guess_tag_ids)s, %(model)s, %(error)s)
    ON CONFLICT (cohort_id, image_id) DO UPDATE
      SET guess_tag_ids = EXCLUDED.guess_tag_ids,
          model = EXCLUDED.model,
          error = EXCLUDED.error,
          screened_at = now()
"""


def record_screen(
    conn: psycopg.Connection, *, cohort_id: int, image_id: int,
    guess_tag_ids: list[int] | None, model: str, error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(_UPSERT_SCREEN_SQL, {
            "cohort_id": cohort_id, "image_id": image_id,
            "guess_tag_ids": list(guess_tag_ids or []), "model": model, "error": error,
        })


def screen_error_rate(conn: psycopg.Connection, *, cohort_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(_SCREEN_ERROR_COUNT_SQL, {"cohort_id": cohort_id})
        row = cur.fetchone()
    errors, total = (int(row[0]), int(row[1])) if row else (0, 0)
    return {"errors": errors, "total": total,
            "rate": (errors / total) if total else 0.0}


def assign_strata(
    screens: list[tuple[int, list[int]]],
) -> tuple[dict[int, str], dict[str, int]]:
    """Partition screened images into strata, and report each stratum's size.

    Returns (image_id -> stratum, stratum -> size). An image guessed for several
    tags goes to the RAREST guessed tag's stratum — rarity by hit count inside this
    screen — so the partition is deterministic and the enrichment budget lands where
    the corpus is thinnest. Every image lands somewhere; nothing is dropped."""
    hits: dict[int, int] = {}
    for _image_id, guesses in screens:
        for g in guesses:
            hits[g] = hits.get(g, 0) + 1

    def rarity(tag_id: int) -> tuple[int, int]:
        # Hit count first, tag id as the tiebreak so the partition is stable across
        # runs rather than dependent on dict ordering.
        return (hits.get(tag_id, 0), tag_id)

    stratum_of: dict[int, str] = {}
    sizes: dict[str, int] = {}
    for image_id, guesses in screens:
        if guesses:
            rarest = min(guesses, key=rarity)
            stratum = f"screen_hit:{rarest}"
        else:
            stratum = SCREEN_NONE
        stratum_of[image_id] = stratum
        sizes[stratum] = sizes.get(stratum, 0) + 1
    return stratum_of, sizes


def allocate_stratified(
    stratum_of: dict[int, str], sizes: dict[str, int], *, total: int,
    none_share: float = 0.2,
) -> dict[str, int]:
    """How many to draw from each stratum.

    `none_share` of the budget goes to `screen_none` — NOT because those images are
    interesting, but because a stratum sampled at zero is a filtered stratum, and
    then the exam can only ever measure recall over what the screener found. The
    rest is spread EVENLY across the hit strata rather than proportionally: the
    whole point is to lift the rare tags to a gradeable count, and proportional
    allocation would just reproduce the corpus imbalance the enrichment exists to
    correct."""
    hit_strata = sorted(s for s in sizes if s != SCREEN_NONE)
    quota: dict[str, int] = {}

    none_available = sizes.get(SCREEN_NONE, 0)
    none_quota = min(int(round(total * none_share)), none_available)
    if none_available and none_quota == 0:
        # Never round a present stratum down to zero: that is filtering by
        # arithmetic, and it is exactly what must not happen here.
        none_quota = 1
    if none_quota:
        quota[SCREEN_NONE] = none_quota

    remaining = total - sum(quota.values())
    if hit_strata and remaining > 0:
        base, extra = divmod(remaining, len(hit_strata))
        for i, s in enumerate(hit_strata):
            want = base + (1 if i < extra else 0)
            take = min(want, sizes[s])
            if take:
                quota[s] = take
    return quota


def inclusion_probabilities(
    quota: dict[str, int], sizes: dict[str, int],
) -> dict[str, float]:
    """p per stratum = drawn / size. Every returned p is > 0 by construction, since
    a stratum with a zero quota is simply absent from `quota`."""
    return {s: n / float(sizes[s]) for s, n in quota.items() if sizes.get(s)}


def screened_rows(
    conn: psycopg.Connection, *, cohort_id: int,
) -> list[tuple[int, list[int]]]:
    """Successfully screened images not already in the exam. Errored screens are
    excluded here rather than filtered later, so they can never be mistaken for
    the `screen_none` stratum."""
    with conn.cursor() as cur:
        cur.execute(_SCREEN_ROWS_SQL, {"cohort_id": cohort_id})
        return [(int(r[0]), [int(x) for x in (r[1] or [])]) for r in cur.fetchall()]
