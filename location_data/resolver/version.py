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
# v5.4 = MF program PR-B (operator rulings D5 + Q7, 2026-09-25). FILL stores `katastr_kod`,
#      the single KÚ of the BOUND entity — a KÚ/ZSJ on the chain, the one KÚ of a one-KÚ
#      obec, an address point's own KÚ, a street or část obce whose every RÚIAN door lies in
#      one KÚ — and never a pin's. Containment reads the `pip` pieces only (the authoritative
#      fallback branch is gone). The bump re-queues the corpus so every row gets its KÚ; the
#      MF view swap (migration 567) refuses to apply until the address-grain rows have one.
# v5.5 = text-reader W2 (operator rulings 2026-09-30). THE TOWN RULE (D4): a town name is
#      looked up as obec / část obce / KÚ and climbed to its obec, kept by the PSČ, else by
#      okres/kraj and, with a PSČ or a precise pin, within 40 km of the pin; a pin settles a tie
#      (its obec, else the nearest — no lowest-id pick where a pin exists). House numbers are
#      TYPED (D7): a marked č.ev. joins only a `č.ev.` point, an unmarked number a č.p. first;
#      a streetless one is looked up in the one bound část; only a bound č.p. publishes one
#      (`street_segment` deleted). An exact street in a named town takes the register's část
#      when all its doors lie in one (Q3). Six dead claim types leave BIND, eleven leave S1.
# v5.6 = text-reader W3: the separator split is deleted (a street claim is matched whole) and R3
#      runs for every street claim (the `low` gate left with bazos' headline, its one user);
#      bazos' town, part, street and numbers come from the stored reading.
# v5.7 = text-reader W4 (operator verdicts 2026-10-14): the village a town name climbed from is
#      the part; "X u Y" retries as X kept to Y; a street line naming a part is that part; of two
#      accent-folded twins, the one with a door in the one bound part, else the one a claim with
#      diacritics spells, binds; abbreviations fold (ČSA, nám., tř. …); a PSČ yields to the street.
# v5.8 = the fallback point (operator ruling 2026-10-25). A row no pin and no street placed,
#      bound to a část obce, stands at the centroid of that část's own doors (`part_point`,
#      which also answers its KÚ — `part_katastr_kod` folded into it), not on its town's point:
#      23,618 active quarter-level ads stood on a town point, Prague's in Vinohrady. A FILL
#      placement's radius is never below the placed-on entity's reach — the část's farthest
#      door, the polygon's `containment_radius_m` (Praha 17.6 km where `obec` said 1 km).
#      The bump re-queues the corpus.
# v5.9 = the words decide (operator ruling 2026-10-30). A pin the portal DECLARES blurred that
#      lies outside the obec the claims bound is not the position: `bind.place` leaves the row
#      unplaced and FILL stands it at the named entity's own point (a bound část's doors, else
#      the obec's point), its radius floored by that entity's reach (v5.8's rule). Every
#      blur-declared label counts (bazos approximate_location, idnes no_exact_address, sreality
#      street/ward/quarter/municipality/not_address, mmreality regional, maxima Circle/LineString,
#      realitymix estimated); a blurred pin outside every Czech obec is set aside the same way,
#      and CHECK then records `pin_outside_cz` or `country_conflict`. bazos stamps every pin
#      "Přibližná lokalita" and places it on a postal-area point — one Kolín pin carried 143
#      active ads from 34 obce — so 12,543 active bazos rows said one town and drew another,
#      8.8 km off at r = 1 km (listing 19509113: Jestřabí Lhota, drawn in Kolín); ~2.9k of them
#      bound a část and now publish the quarter grain the bind always had. The pin stays
#      evidence (`pin_outside_obec` is still recorded); an exact or undeclared pin outside the
#      town keeps the position as before. The bump re-queues the corpus.
# v5.9.1 = location W3, one claim per fact (#2223). The read returns at most ONE claim per
#      listing and claim type, the operator's ahead of the portal's (migration 699 keys the
#      store), so BIND's seven tie rules go: no pin election by declared rank and id, no
#      first-PSČ / first-street / last-address-point idiom, no blur OR'd across a type's
#      claims, no two-street pooling, and no operator bypass of the register (an operator
#      value outranks at the read and binds like any claim). A house-number claim outranks the
#      number in the street line. A point release: W5 takes v5.10. The bump re-resolves the
#      corpus through W2's reconcile.
# v5.10 = location W5 (rulings Q1, Q2, Q3). ONE TRUTH: where the words bound the row, a pin is
#      its position only inside the bound town's polygon and never when the portal declares it an
#      area marker (bazos approximate_location); without words it names its town (R7/R8) as
#      before. v5.9's declared-blurred exception and CHECK's coarsening go: 1,353 pins outside
#      their town and 23,297 bazos postal pins move to the bound entity's point at its reach. The
#      quarter is a část only: MOMC is a match key that anchors its town; správní obvod and ZSJ
#      are no level (2,834 MOMC codes and 200 SO ties re-bind). A part claim matches like a town
#      name (a KÚ binds the část it spells), and a street keeps the claimed část where it has
#      doors. A street line naming two části of its town now reads as a place too, so R3 skips it
#      (no such row known as of 2026-10-31; the count's SELECT: the W5 design, As built). The
#      grain is the row's finest key. An address point without coordinates takes its část's KÚ.
#      The bump re-resolves the corpus through W2's reconcile.
RESOLVER_VERSION = "resolver:v5.10"
