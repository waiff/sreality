"""GLOBAL_SEARCH 6.1 (a), pre-registered before any post-heal arm: the keep rule and the cut rule,
read off leaderboard rows. A row counts only when its stamp (the engine's artefact and the lab's
code) passed `lab verify` with its rung-by-rung read, and its score column is the one path's.

Keep: an arm is kept only if its c18 M1 is not lower than the incumbent's, M3 holds on the extended
fixture list (every fixture of `keep_apart.json` read by some row, and apart, unless the operator
took the case off the bar at an RP), M2 is unchanged on trial and c18, and its label-free move (M7)
is read (a person's step: printed, never decided here). Every row it needs must exist for BOTH the
arm and the incumbent, and each pair of rows must come off one cache (or, through an explicit
alias, off one export and one engine); a missing row makes the verdict INCOMPLETE, never KEEP.

Cut: the highest t_merge in {0.70, 0.75, 0.80, 0.85, 0.90} whose c18 M1 is not lower than the
incumbent's c18 M1, then checked by the c17 M4 read. Never tuned on the trial: the rule reads no
trial row. The rule's incumbent is R1; until R1 has rows, another arm (R0, `w31_reference`) stands
for it, and `keep` / `cut` print which.

Readers, 6.1 (b), for a challenger arm: at most two readers or tolerances added back over MF-P14;
each fixture case only the arm joins needs one of its own."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

CUTS: tuple[float, ...] = (0.70, 0.75, 0.80, 0.85, 0.90)
VALIDATE: str = "c18"
READ: str = "c17"
RULE_INCUMBENT: str = "R1"
M2_COHORTS: tuple[str, ...] = ("trial", "c18")

Rows = Mapping[str, Mapping[str, Any]]
Fixture = tuple[str, int, int, str]


def _m(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return (row.get("m") or {}).get(key) or {}


def comparable(mine: Mapping[str, Any], theirs: Mapping[str, Any], aliased: bool) -> str | None:
    """Why two rows of one cohort cannot be compared, or None: the same cache, or (aliased) the
    same export under the same engine code."""
    if aliased:
        if mine.get("export") != theirs.get("export") or mine.get("engine") != theirs.get("engine"):
            return (f"alias rows differ in export or engine ({mine.get('export')}/"
                    f"{mine.get('engine')} vs {theirs.get('export')}/{theirs.get('engine')})")
        return None
    if mine.get("cache") != theirs.get("cache"):
        return f"cache {mine.get('cache')} vs incumbent {theirs.get('cache')}"
    return None


def _counts(row: Mapping[str, Any], verified: set[str]) -> str | None:
    if row.get("stamp") not in verified:
        return "unverified cache"
    if row.get("verified") is not True:
        return str(row.get("verified") or "not verified")
    return None


def keep(arm: Rows, incumbent: Rows, verified: set[str], validate: str = VALIDATE,
         m2_cohorts: Iterable[str] = M2_COHORTS, accepted: Iterable[str] = (),
         read: str = READ, fixtures: Iterable[Fixture] = (),
         aliased: Iterable[str] = ()) -> dict[str, Any]:
    """KEEP, DROP or INCOMPLETE for one arm (its rows per cohort, aliases already applied) against
    the incumbent's rows. `fixtures` is the keep-apart list (M3's whole bar)."""
    off_bar = set(accepted)
    alias = set(aliased)
    m2_cohorts = tuple(m2_cohorts)
    checks: dict[str, Any] = {}
    reasons: list[str] = []
    missing: list[str] = []
    needed = list(dict.fromkeys([validate, read, *m2_cohorts]))
    for cohort in needed:
        for side, rows in (("arm", arm), ("incumbent", incumbent)):
            if cohort not in rows:
                missing.append(f"no {side} row for {cohort}")
    rows = [(side, cohort, row) for side, pool in (("arm", arm), ("incumbent", incumbent))
            for cohort, row in sorted(pool.items())]
    unverified = sorted(f"{side} {r.get('experiment')}@{cohort}: {why}" for side, cohort, r in rows
                        if (why := _counts(r, verified)))
    checks["verified"] = not unverified
    if unverified:
        reasons.append(f"rows that do not count: {'; '.join(unverified)}")
    mismatch = [f"{cohort}: {why}" for cohort in sorted(set(arm) & set(incumbent))
                if (why := comparable(arm[cohort], incumbent[cohort], cohort in alias))]
    checks["comparable"] = not mismatch
    if mismatch:
        reasons.append(f"rows off different caches: {'; '.join(mismatch)}")
    if validate in arm and validate in incumbent:
        mine, theirs = _m(arm[validate], "M1"), _m(incumbent[validate], "M1")
        checks["M1"] = {"arm": mine.get("together"), "incumbent": theirs.get("together"),
                        "n": theirs.get("n")}
        if mine.get("together") is None or mine["together"] < theirs.get("together", 0):
            reasons.append(f"{validate} M1 {mine.get('together')} < incumbent "
                           f"{theirs.get('together')}")

    def together(pool: Rows) -> list[str]:
        return [f"{cohort}: {item}" for cohort, row in sorted(pool.items())
                for item in _m(row, "M3").get("together", []) if item.split(" (")[0] not in off_bar]
    joined, theirs_joined = together(arm), together(incumbent)
    bar = {f"{a}x{b}": (cohort, case) for cohort, a, b, case in fixtures if case not in off_bar}
    unread: dict[str, list[str]] = {}
    for side, pool in (("arm", arm), ("incumbent", incumbent)):
        covered = {x for row in pool.values() for x in _m(row, "M3").get("present", [])}
        for key in sorted(set(bar) - covered):
            unread.setdefault(f"{side} {bar[key][0]}", []).append(key)
    checks["M3"] = {"together": joined, "accepted": sorted(off_bar),
                    "incumbent_together": theirs_joined,
                    "arm_only": sorted(set(joined) - set(theirs_joined)),
                    "bar": len(bar), "unread": {k: len(v) for k, v in sorted(unread.items())}}
    for key, ids in sorted(unread.items()):
        missing.append(f"M3: no {key.split()[0]} row reads {len(ids)} fixture(s) listed under "
                       f"{key.split()[1]} ({', '.join(ids[:4])}{', ...' if len(ids) > 4 else ''})")
    if joined:
        reasons.append(f"M3: {len(joined)} fixture pair(s) together ({len(theirs_joined)} in the "
                       f"incumbent; {len(set(joined) - set(theirs_joined))} only in the arm)")
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
    verdict = "DROP" if reasons else "INCOMPLETE" if missing else "KEEP"
    return {"verdict": verdict, "reasons": reasons, "missing": missing, "checks": checks,
            "owed": ["the M7 read (label-free move) by a person"] if verdict == "KEEP" else []}


READERS_MAX: int = 2


def readers(kept: Mapping[str, Any], added: int) -> dict[str, Any]:
    """Rule 6.1 (b) on a `keep` result: at most READERS_MAX readers or tolerances added back over
    MF-P14 before MF's freeze. Every fixture case the arm alone joins states its own fact, so it
    needs one reader of its own (until a reader is shown to hold two); a case the operator took off
    the bar needs none."""
    cases = sorted({item.split(": ", 1)[1].split(" (")[0]
                    for item in kept["checks"].get("M3", {}).get("arm_only", [])})
    needed = added + len(cases)
    return {"verdict": "DROP" if needed > READERS_MAX else "KEEP", "added": added,
            "cases_needing_a_reader": cases, "needed": needed, "max": READERS_MAX}


def stop_rules(kept: str, rule_b: str) -> str:
    """Rules 6.1 (a) and (b) together: a DROP by either drops the arm, otherwise (a)'s KEEP or
    INCOMPLETE. (a) is INCOMPLETE only when the arm joins no fixture, so a (b) DROP beside it comes
    from the readers added alone and no missing row can lift it."""
    return "DROP" if "DROP" in (kept, rule_b) else kept


def cut(sweep: Mapping[float, Rows], incumbent: Rows, verified: set[str],
        validate: str = VALIDATE, read: str = READ, cuts: Iterable[float] = CUTS
        ) -> dict[str, Any]:
    """The cut by rule (a): `sweep` maps each t_merge to that arm's rows per cohort. A sweep row
    counts only when it is verified and comes off the incumbent's cache."""
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
        why = _counts(row, verified) or comparable(row, incumbent[validate], False)
        m1 = _m(row, "M1").get("together")
        table[f"{t:.2f}"] = {"M1": m1, "counts": why or "yes"}
        if why is None and m1 is not None and m1 >= bar:
            chosen = t
    out: dict[str, Any] = {"bar": {f"incumbent {validate} M1": bar}, "sweep": table, "cut": chosen}
    if chosen is None:
        out["check"] = "no cut reaches the incumbent's M1"
        return out
    read_row = sweep[chosen].get(read, {})
    m4 = _m(read_row, "M4")
    why = (None if not read_row else _counts(read_row, verified)
           or (comparable(read_row, incumbent[read], False) if read in incumbent
               else f"no incumbent {read} row"))
    if not m4:
        out["check"] = f"{read} row owed"
    elif why:
        out["check"] = f"{read} row does not count: {why}"
    elif m4.get("read", 0) < m4.get("sampled", 0):
        out["check"] = f"{read} M4 read owed ({m4.get('read', 0)}/{m4.get('sampled', 0)} read)"
    elif m4.get("fused", 0):
        out["check"] = f"FAILS: {m4['fused']} fused in the {read} M4 read"
    else:
        out["check"] = f"holds: 0 fused of {m4['read']} in the {read} M4 read"
    return out
