"""The keep-apart fixture list (hard bar M3 / A3): one JSON the lab reads too, `autodedup/lab/keep_apart.json`."""
from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "autodedup" / "lab" / "keep_apart.json"
SECTIONS = ("fixtures", "convention_pairs", "dropped")


def load(path: Path = PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def by_cohort(section: str = "fixtures", path: Path = PATH) -> dict[str, dict[str, tuple[int, int]]]:
    """{cohort: {"case (lo x hi)": (lo, hi)}} for one section of the file."""
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for row in load(path)[section]:
        lo, hi = row["ids"]
        out.setdefault(row["cohort"], {})[f"{row['case']} ({lo} x {hi})"] = (lo, hi)
    return out
