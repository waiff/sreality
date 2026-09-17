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
# v5 = W9 (operator ruling 2026-09-14): a composite locality name is bound against the
#      REGISTER instead of being folded by a regex in the reader. The whole string is matched
#      at obec / část obce / MOMC / správní obvod first; only if nothing matches whole is it
#      split, and then a token naming an obec (or a uniquely named part) anchors the rest
#      INSIDE that town, which is what lets "Praha 4 - Podolí" publish Praha + Podolí with
#      both names spelled by RÚIAN. Ambiguity binds nothing. Every row re-resolves once.
# v5.1 = W11 (incident 2026-09-14): a contract bump never blacks out. The claim read takes a
#      listing's NEWEST EVIDENCE — the highest contract version present for that (listing,
#      portal) that is <= the active one — instead of the active version only, so the hours
#      between a bump and its re-mine no longer judge a listing with no claims at all. The
#      bump re-queues the 595,816 rows the W9 sweep emptied.
# v5.2 = W18 (operator ruling 2026-09-16, bazos 223293822). Five rules, and every one of them
#      can move an answer, so the bump IS the corpus sweep:
#      * a street is PUBLISHED only when it BINDS to `ruian_streets` in the anchoring obec —
#        an unbound claim text is no longer copied onto the row (1,864 rows across seven
#        portals lose a name that joined to nothing);
#      * a bound street has a POINT (the centroid of its valid address points) and an EXTENT
#        (the distance from it to the FARTHEST of them — half the bounding-box diagonal
#        excluded a real door on 65 % of streets), and that point outranks the portal pin when
#        the pin is absent, declared blurred, or farther away than the street reaches;
#      * CHECK's containment tests read the elected coordinate CLAIM rather than the published
#        position, so `pin_outside_obec` / `pin_outside_cz` survive a register-placed row and
#        outrank the new `pin_off_street` (an EXACT pin overridden by the street, capped at
#        `medium`);
#      * a portal's declared precision no longer caps the GRANULARITY at all — the
#        `DECLARED_CAP` ladder is deleted; the grain is the bind's and a declaration caps the
#        CONFIDENCE;
#      * every street claim is bound by ONE matcher: split on the portals' separators, matched
#        exactly inside the anchoring obec in two tiers (full name first, type-word-tolerant
#        only when nothing matched exactly), fail-closed on two distinct streets. The
#        similarity rung (R3) runs only for a claim the CONTRACT calls an address field, never
#        for one it declares `claim_confidence: low`.
# v5.3 = W18-b (incident 2026-09-17 00:50Z). W11 read a listing's newest evidence per
#      (listing, PORTAL); the rail is now per (listing, portal, CLAIM TYPE). W18 bumped the
#      bazos contract 6 -> 7 for ONE payload entry (`street_name`), the payload lane mined it
#      across all 147 k listings in one hop, and the same run's bounded BODIES pass reached
#      only 56,905 of ~155 k pages — so for ~90 k listings the newest version present carried
#      the STREET alone and the per-portal partition hid the obec, the PSČ and the pin still
#      sitting at version 6: 29,545 of 50,598 live bazos listings `undetermined` with no
#      geom, 61,396 rows at granularity `unknown`. Per claim type, each type keeps the newest
#      version that actually carries it, so a PARTIAL re-mine can never blank a listing
#      again; a claim type the ACTIVE contract no longer declares is not read at all. The
#      bump re-queues the corpus so the blanked rows resolve on their real evidence.
RESOLVER_VERSION = "resolver:v5.3"
