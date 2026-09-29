"""The operator's reason codes — WHY a verdict was given (PROGRAM.md §9, migration 533).

THE PICKER IS GONE. The review pages ask two answers and a note (binary verdicts, D39); the
reason chips were used on 6 of 1,158 rulings and left every surface with the Judge page. What
stays is the vocabulary the stored `verdicts.reasons` column was written in: a client that
still names a code is validated against it, and the CODES never change once written. The
labels are the operator's own Czech words for those codes.
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
