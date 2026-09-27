"""The keep-apart fixture list (hard bar M3 / A3): one JSON the lab reads too, `autodedup/lab/keep_apart.json`."""
from __future__ import annotations

import json
from collections.abc import Container
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "autodedup" / "lab" / "keep_apart.json"
SECTIONS = ("fixtures", "convention_pairs", "dropped")


def load(path: Path = PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def present(cohort: str, ids: Container[int], section: str = "fixtures",
            path: Path = PATH) -> dict[str, tuple[int, int]]:
    """{"case (lo x hi)": (lo, hi)} for every pair of `section` whose two adverts are in `ids`, whatever cohort
    the file names it under; the lab's M3 (`autodedup.lab.metrics.fixtures_apart`) counts the same pairs.

    A pair listed under `cohort`, or with one advert in `ids`, that is not whole there raises: a re-export
    dropped or renumbered a fixture advert, and M3 would otherwise pass it silently."""
    out: dict[str, tuple[int, int]] = {}
    absent = []
    for row in load(path)[section]:
        lo, hi = row["ids"]
        label = f"{row['case']} ({lo} x {hi})"
        if lo in ids and hi in ids:
            out[label] = (lo, hi)
        elif row["cohort"] == cohort or lo in ids or hi in ids:
            absent.append(label)
    if absent:
        raise ValueError(f"keep-apart {section} absent from cohort {cohort}: {absent}; re-read them against "
                         "this export and edit autodedup/lab/keep_apart.json")
    return out
