"""Rule #3's policy: how many nominated rows get a page check now, and when a
gone verdict is not trusted. Pure: no DB, no network, no clock read."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

LOG = logging.getLogger(__name__)

# --- the per-walk verify throttle (migrations 451/452) -----------------------
#
# CALIBRATED AGAINST 60 DAYS OF REAL SWEEPS, not guessed. Over 11,763 sweeps
# that flipped at least one row, the per-sweep share of a category is p95=1.8%,
# p99=3.4%, and then the tail jumps straight to 86%: routine churn and genuine
# incidents are two separate populations, and the gap between them is where the
# ceiling belongs. At 2% the throttle would have bitten 446 times in 60 days --
# on ordinary sreality and idnes rental churn -- which is not a throttle, it is
# an outage generator. At 10% it bites on exactly the four real events in that
# window (realitymix dum/prodej at 86%, ceskereality komercni/prodej at 30% and
# 13.7%, sreality pozemek/podil at 18.7%).
#
# min_rows is a floor on CATEGORY SIZE, not on the ceiling. It is 2,000 because
# the small categories are the churny ones: sreality pozemek/drazba holds ~600
# live rows and legitimately turns over 6-39% of them in a sweep (auctions end
# on a date), as does idnes dum/pronajem at ~630. Policing those is noise.
DELIST_CAP_DEFAULTS: dict[str, float | int] = {"fraction": 0.10, "min_rows": 2000}


@dataclass(frozen=True)
class VerifyBudget:
    """How many of one walk's nominated rows go to the drain now (rule #3 throttle)."""

    queue: int                              # enqueue now, oldest-unseen first
    deferred: int                           # nominated - queue; recorded, re-nominated next walk
    cap: int | None                         # max(1, int(active_rows*fraction)); None below min_rows
    override: Mapping[str, Any] | None      # the operator entry that lifted the cap


def verify_budget(
    setting: object,
    *,
    source: str,
    category_main: str | None,
    category_type: str | None,
    subtype: str | None,
    nominated: int,
    active_rows: int,
    now: datetime,
) -> VerifyBudget:
    """Split one walk's nominations into queue-now and deferred under app_settings.delist_flip_cap."""
    fraction, min_rows, overrides = _parse_cap(setting)
    if active_rows < min_rows:
        return VerifyBudget(nominated, 0, None, None)
    cap = max(1, int(active_rows * fraction))
    if nominated <= cap:
        return VerifyBudget(nominated, 0, cap, None)
    permit = _override_permits(
        overrides, source=source, category_main=category_main,
        category_type=category_type, subtype=subtype, candidates=nominated, now=now,
    )
    if permit is not None:
        return VerifyBudget(nominated, 0, cap, permit)
    return VerifyBudget(cap, nominated - cap, cap, None)


def _parse_cap(setting: object) -> tuple[float, int, list[dict[str, Any]]]:
    """The operator-tunable ceiling, falling back to the baked defaults so a
    malformed knob can never REMOVE the throttle.

    The operator's scoped lift (`overrides`, see `_override_permits`) lives in
    the SAME setting as the cap, so there is one knob to read, one to audit,
    and no second mechanism that can drift out of step with the first.
    """
    defaults = (float(DELIST_CAP_DEFAULTS["fraction"]), int(DELIST_CAP_DEFAULTS["min_rows"]), [])
    if not isinstance(setting, dict):
        return defaults
    try:
        fraction = float(setting.get("fraction", DELIST_CAP_DEFAULTS["fraction"]))
        min_rows = int(setting.get("min_rows", DELIST_CAP_DEFAULTS["min_rows"]))
        raw = setting.get("overrides")
        overrides = [o for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
    except Exception as exc:  # noqa: BLE001 - a broken knob must not disarm the throttle
        LOG.warning("delist cap: falling back to defaults (%s)", exc)
        return defaults
    return fraction, min_rows, overrides


def _override_permits(
    overrides: list[dict[str, Any]],
    *,
    source: str,
    category_main: str | None,
    category_type: str | None,
    subtype: str | None,
    candidates: int,
    now: datetime,
) -> dict[str, Any] | None:
    """The operator lift for one scope of the verify throttle.

    The throttle defers a walk's excess nominations to the next walk, so a real
    backlog drains on its own in a few walks; this is for the operator who has
    verified one scope and wants it checked now, without that meaning "raise the
    ceiling everywhere".

    An override is therefore SCOPED (it names the source, and may name the
    category), BOUNDED (`max_rows` is a hard row count, so even a wildcard
    override cannot authorise an unbounded queue), and EXPIRING (`until` is
    required and must still be in the future). Anything missing, unparseable or
    already expired is IGNORED -- the lift fails shut, like the cap it lifts.
    """
    for o in overrides:
        try:
            if o.get("source") != source:
                continue
            for key, actual in (("category_main", category_main),
                                ("category_type", category_type),
                                ("subtype", subtype)):
                want = o.get(key)
                if want is not None and want != actual:
                    break
            else:
                until = datetime.fromisoformat(str(o["until"]).replace("Z", "+00:00"))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if until <= now:
                    continue
                if candidates <= int(o["max_rows"]):
                    return o
        except Exception as exc:  # noqa: BLE001 - a malformed lift stays shut
            LOG.warning("delist cap: ignoring malformed override %r (%s)", o, exc)
    return None
