"""The resolver's version constant — a code constant, bumped per shipped rule.

`resolver_version` is one of the THREE version inputs stamped on a `listing_location` row
(with `claim_set_hash` and `registry_version`). Bumping it does not rewrite anything: the
drain's sweep sees every row whose stamp is not the current one and enqueues it, so the
corpus re-resolves through the ordinary lane.

Bump it whenever a rule that can change an output changes — a new rung, a different score,
a changed cap, a changed normalization. `normalizer_version` is part of `resolver_version`
by construction, so there is deliberately no second knob.
"""

from __future__ import annotations

# v1 = the first shipped S1-S7 implementation.
# v2 = 2026-09-10: on a registry-bound row the official RÚIAN `street_name` overwrites the
#      portal's spelling.
# v3 = 2026-09-11: the PSČ-only bind goes through the qualifier ladder; the imprecise
#      coordinate tie-break and the post-town tie-break are served at `low`.
# v4 = W2-a: nine stages become four (bind → fill → grade → check) writing the 26-column
#      `listing_location`. Survivorship, the uncertainty policy, the contradiction ledger,
#      the pin-collision epoch and the parcel rung are gone; the hierarchy is a pure join
#      off the bound entity, so `cast_obce` now lands on every branch and not only on PIP.
# v4.1 = W2-a3: a towned row always has a position. When no pin was admissible, FILL places
#      the row at the finest bound unit's own registry point (the boundary's inscribed-circle
#      centre, always inside the polygon) instead of leaving `geom` NULL on 29 % of towned
#      rows. Grade, confidence and radius are untouched, so the bump exists to re-resolve the
#      corpus once, not to re-grade it.
RESOLVER_VERSION = "resolver:v4.1"
