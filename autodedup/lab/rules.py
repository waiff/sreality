"""GLOBAL_SEARCH 6.1 (a), pre-registered before any post-heal arm: the keep rule and the cut rule,
read off leaderboard rows. A row counts only when its stamp (the engine's artefact and the lab's
code) passed `lab verify`.

Keep: an arm is kept only if its c18 M1 is not lower than the incumbent's, M3 holds on the extended
fixture list (every present fixture apart, unless the operator took the case off the bar at an RP),
M2 is unchanged on trial and c18, and its label-free move (M7) is read (a person's step: printed,
never decided here).

Cut: the highest t_merge in {0.70, 0.75, 0.80, 0.85, 0.90} whose c18 M1 is not lower than the
incumbent's c18 M1, then checked by the c17 M4 read. Never tuned on the trial: the rule reads no
trial row."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

CUTS: tuple[float, ...] = (0.70, 0.75, 0.80, 0.85, 0.90)
VALIDATE: str = "c18"
READ: str = "c17"
M2_COHORTS: tuple[str, ...] = ("trial", "c18")

Rows = Mapping[str, Mapping[str, Any]]


def _m(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return (row.get("m") or {}).get(key) or {}


def keep(arm: Rows, incumbent: Rows, verified: set[str], validate: str = VALIDATE,
         m2_cohorts: Iterable[str] = M2_COHORTS, accepted: Iterable[str] = ()) -> dict[str, Any]:
    """KEEP or DROP for one arm (its rows per cohort) against the incumbent's rows."""
    off_bar = set(accepted)
    checks: dict[str, Any] = {}
    reasons: list[str] = []
    rows = [*arm.values(), *incumbent.values()]
    unverified = sorted({f"{r['experiment']}@{r['cohort']}" for r in rows
                         if r.get("stamp") not in verified})
    checks["verified"] = not unverified
    if unverified:
        reasons.append(f"rows from unverified caches: {', '.join(unverified)}")
    if validate not in arm or validate not in incumbent:
        reasons.append(f"no {validate} row for the arm or the incumbent")
    else:
        mine, theirs = _m(arm[validate], "M1"), _m(incumbent[validate], "M1")
        checks["M1"] = {"arm": mine.get("together"), "incumbent": theirs.get("together"),
                        "n": theirs.get("n")}
        if mine.get("together") is None or mine["together"] < theirs.get("together", 0):
            reasons.append(f"{validate} M1 {mine.get('together')} < incumbent "
                           f"{theirs.get('together')}")
    joined: list[str] = []
    for cohort, row in sorted(arm.items()):
        for item in _m(row, "M3").get("together", []):
            case = item.split(" (")[0]
            if case not in off_bar:
                joined.append(f"{cohort}: {item}")
    checks["M3"] = {"together": joined, "accepted": sorted(off_bar)}
    if joined:
        reasons.append(f"M3: {len(joined)} fixture pair(s) together")
    m2: dict[str, Any] = {}
    for cohort in m2_cohorts:
        if cohort in arm and cohort in incumbent:
            mine, theirs = _m(arm[cohort], "M2"), _m(incumbent[cohort], "M2")
            m2[cohort] = [mine.get("together"), theirs.get("together")]
            if mine.get("together") != theirs.get("together"):
                reasons.append(f"{cohort} M2 {mine.get('together')} != incumbent "
                               f"{theirs.get('together')}")
    checks["M2"] = m2
    checks["M7"] = {cohort: _m(row, "M7") for cohort, row in sorted(arm.items())}
    return {"verdict": "DROP" if reasons else "KEEP", "reasons": reasons, "checks": checks,
            "owed": [] if reasons else ["the M7 read (label-free move) by a person"]}


def cut(sweep: Mapping[float, Rows], incumbent: Rows, verified: set[str],
        validate: str = VALIDATE, read: str = READ, cuts: Iterable[float] = CUTS
        ) -> dict[str, Any]:
    """The cut by rule (a): `sweep` maps each t_merge to that arm's rows per cohort."""
    if validate not in incumbent:
        return {"cut": None, "reasons": [f"no incumbent {validate} row"]}
    bar = _m(incumbent[validate], "M1").get("together", 0)
    table: dict[str, Any] = {}
    chosen: float | None = None
    for t in sorted(cuts):
        rows = sweep.get(t)
        if not rows or validate not in rows:
            table[f"{t:.2f}"] = "no row"
            continue
        row = rows[validate]
        ok_row = row.get("stamp") in verified
        m1 = _m(row, "M1").get("together")
        table[f"{t:.2f}"] = {"M1": m1, "verified": ok_row}
        if ok_row and m1 is not None and m1 >= bar:
            chosen = t
    out: dict[str, Any] = {"bar": {f"incumbent {validate} M1": bar}, "sweep": table, "cut": chosen}
    if chosen is None:
        out["check"] = "no cut reaches the incumbent's M1"
        return out
    m4 = _m(sweep[chosen].get(read, {}), "M4")
    if not m4:
        out["check"] = f"{read} row owed"
    elif m4.get("read", 0) < m4.get("sampled", 0):
        out["check"] = f"{read} M4 read owed ({m4.get('read', 0)}/{m4.get('sampled', 0)} read)"
    elif m4.get("fused", 0):
        out["check"] = f"FAILS: {m4['fused']} fused in the {read} M4 read"
    else:
        out["check"] = f"holds: 0 fused of {m4['read']} in the {read} M4 read"
    return out
