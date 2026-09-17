"""The operator's reason vocabulary — WHY a verdict was given (PROGRAM.md §9, migration 533).

ONE REGISTRY, SERVED TO THE SPA. The codes live here and reach the browser through
`GET /autodedup/verdict-reasons`; the page hard-codes none of them, so adding a shape a
review session named is one line in this file and no migration (the column carries no CHECK
— see 533). The labels are the operator's own Czech words, because the chips are clicked in
Czech; the CODES are what the histogram and any future feature work group on, so they never
change once written.

WHAT IT IS FOR. A chip is the evidence the engine did not have: `floor_plan_differs` on a
pair the engine merged names a discriminator the feature set misses, and the per-code
histogram is directly comparable with the judge's `unit_discriminator` — the feature-gap
loop, not a comment field.
"""

from __future__ import annotations

VERDICT_REASONS: tuple[tuple[str, str], ...] = (
    ("floor_plan_differs", "Jiný půdorys"),
    ("unit_number", "Číslo jednotky"),
    ("kitchen_or_bathroom_differs", "Jiná kuchyň nebo koupelna"),
    ("floor_differs", "Jiné podlaží"),
    ("area_differs", "Jiná výměra"),
    ("price_history", "Cenová historie sedí"),
    ("description", "Popis"),
    ("broker", "Stejný makléř"),
    ("identical_photos", "Stejné fotky"),
    ("catalogue_photos_only", "Jen katalogové fotky"),
    ("relisted_after_gap", "Znovu inzerováno po pauze"),
    ("same_project", "Stejný projekt"),
    ("other", "Jiné"),
)

REASON_CODES: tuple[str, ...] = tuple(code for code, _ in VERDICT_REASONS)
_KNOWN: frozenset[str] = frozenset(REASON_CODES)

# A verdict cannot carry more codes than the vocabulary holds, whatever a client sends.
MAX_REASONS: int = len(REASON_CODES)


def registry() -> list[dict[str, str]]:
    """The vocabulary as the wire carries it — code plus the label the chip renders."""
    return [{"code": code, "label": label} for code, label in VERDICT_REASONS]


def normalise(values: list[str] | None) -> list[str]:
    """De-duplicated, order preserved, unknown codes rejected.

    Order is the operator's click order, which is the order the chips read back in; the
    de-duplication is silent because a repeated code is not a different statement.
    """
    if not values:
        return []
    out: list[str] = []
    for value in values:
        code = value.strip()
        if code not in _KNOWN:
            raise ValueError(f"unknown verdict reason: {value}")
        if code not in out:
            out.append(code)
    return out
