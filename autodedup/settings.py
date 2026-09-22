"""Every tunable of the offline engine in one frozen-by-default row (PROGRAM.md E17/E22/E34).

The W2 lane is pure Python over the exported cohort, but each default here is the local
twin of a column that `autodedup.settings` will carry in production, so a sweep is a JSON
file rather than an edit: `harness run --settings s.json`. `from_json` rejects unknown keys
on purpose — a typo in a sweep file must fail loudly, not silently run the defaults.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from autodedup.features import DEFAULT_VOCABULARY_ATTR_KEYS

# C2 of the W8 verification: 21 of the 23 pairs an interior ratio alone would have carried
# rested on three photos or fewer, so an interior arm carries a floor no settings row may
# lower.
CONTEXT_RULE_MIN_IMAGES_FLOOR: float = 4.0

# E84: one minute, the floor under K-B's window separation. No scrape cadence in this program
# re-lists an advert within a minute of its last sighting, so the bar can only catch two adverts
# the same index walk saw for the first and only time.
MIN_CERTIFICATE_B_GAP_DAYS: float = 1.0 / 1440.0


@dataclass(slots=True)
class Settings:
    area_band_tol: float = 0.20
    area_reject_pct: float = 0.08
    area_band_pct: float = 0.03
    catalog_df: int = 8
    # E83 (W10): E9's population test cannot tell a marketing catalogue from a broker re-posting
    # ONE advert nine times, so a frame is stock only when its carriers are several PARTIES.
    # Each limb is a settings row because each costs differently; `combine` says whether one limb
    # clearing its bar is enough to call a frame stock (`any`, the conservative reading — more
    # frames stay subtracted) or every configured limb must (`all`).
    # `catalog_carrier_coverage_min` is the honest rail: carrier identity is observable only
    # inside the cohort while `pop` is corpus-wide, so a frame the cohort holds less than this
    # share of the population of stays subtracted. OFF by default — E9 is what g6 shipped with,
    # and every relaxation of it adds image evidence and therefore adds merges.
    catalog_carrier_aware: bool = False
    catalog_min_broker_carriers: int | None = None
    catalog_min_source_carriers: int | None = None
    catalog_min_block_carriers: int | None = None
    catalog_carrier_combine: str = "any"
    catalog_carrier_coverage_min: float = 1.0
    # E83's purity rail, E60's restated for a photograph: carriers that print two dispositions
    # or stated areas further apart than this hold the PROJECT's material, not one advert's.
    # 0.01 is K-B's own `CERT_B_AREA` — the certificate already demands the two sides agree
    # within 1 %, so the frame that carries it may not span more.
    catalog_carrier_area_tol: float = 0.01
    anchor_images: int = 3
    max_block_size: int = 200
    max_candidates_per_listing: int = 60
    phash_tight: int = 6
    phash_loose: int = 11
    band_bits: int = 16
    simhash_bands: int = 4
    text_min_chars: int = 200
    t_hi: float = 0.97
    t_lo: float = 0.30
    store_floor: float = 0.02
    cluster_area_spread: float = 0.08
    max_cluster_size: int = 8
    rare_token_df: int = 2
    # E45/E46/W4c (gold eval 2026-09-16). `price_unit` contradicts on 55% of CROSS-portal TRUE
    # duplicates (221/401) and 0% of same-portal ones (1/238) because the portals spell one fact
    # two ways (`celkem|mesic` on sreality/bezrealitky, `za nemovitost|za mesic` elsewhere);
    # `area_basis` contradicts on 31% of cross-portal positives against 14% of negatives. Both are
    # portal VOCABULARY, not property facts, so neither may contradict nor agree.
    vocabulary_attr_keys: tuple[str, ...] = DEFAULT_VOCABULARY_ATTR_KEYS
    # Only identity slots may raise `numeral_conflict` — a price cut (kc) or a rounded area (m2)
    # is what an E6 re-listing looks like, not a different unit.
    numeral_conflict_units: tuple[str, ...] = ("floor", "rooms", "unit")
    max_attr_contradictions: float = 3.0
    # K-A merged at 52.6% HT precision (n=98) on the gold holdout — structurally it certifies a
    # BUILDING (one RUIAN point + disposition + area), which is exactly the developer-unit
    # false-merge shape. Kept as code so an evaluation can switch it back on.
    certificate_ka_enabled: bool = False
    # E60 (W8): two adverts printing the same rare, pure agency order code are one order.
    # ON by default — 0 operator and 0 gold false merges on g5, and it is the only certificate
    # resting on a fact the broker states rather than on a resemblance.
    certificate_kr_enabled: bool = True
    # E65 (W9): K-B may not certify a pair whose photographs the engine never compared. The
    # floor is read on both SIDES (`n_images_min`, E9-subtracted, so an all-catalogue gallery
    # counts as zero) and on the MATCHED set (`phash_loose_matches`); an absent match count
    # fails it, because nothing to compare is not evidence of agreement. 0 disables a limb.
    # Measured on the g6 cohort: under the DETECTION clock a floor of (1, 1) withholds 982 of
    # 1,110 K-B merges carrying 169 reliable labelled duplicates and 0 labelled negatives, so
    # the default is OFF — the floor is the HONEST clock's price, not a free tightening, and
    # `validate` refuses to run the honest clock without it.
    # E110 (W13): `validate` no longer refuses the honest clock without this floor. The floor's
    # whole case was the two gold negatives of the ceskereality Rezidence K Botici families,
    # and on 2026-09-20 the operator ruled both pairs `same`; the price the honest clock pays
    # is now E84 alone. The rows stay live and `certificate_b` still enforces them, so
    # restoring the W9 arm is one number.
    certificate_b_min_images: float = 0.0
    certificate_b_min_matched_images: float = 0.0
    # E84 (W10): K-B's disjointness clause reads a PAIR of windows, so it has to be read at the
    # resolution the sightings have. Under the honest clock an advert seen exactly once has
    # `first_seen == last_seen` and its live window is a POINT, which makes `max(starts) >
    # min(ends)` true for ANY two once-seen adverts — two Regus products listed 2.8 seconds apart
    # in one index walk certified as a re-post (412540 x 412544, gold not-same, unanimous). The
    # separation must therefore exceed the sighting cadence, and the rail reads the pair, never
    # one side: "either side degenerate" costs 7 labelled duplicates, the pair gap costs 0. One
    # minute is measured: engine-wide over the candidate's 1,167 K-B merges it withholds exactly
    # that pair; an hour costs 2 labelled duplicates, a day 95 (M89). 0 by DEFAULT, and that is
    # not timidity: on the detection clock the gap runs from `inactive_at`, so a sub-minute one
    # is a delisting DETECTED and the successor listed in the same drain pass — a true re-post,
    # and the minute withholds exactly one of g6's K-B certificates, 18715382 x 18739520, the
    # same broker's 29,000 Kc 2+kk re-listed 44 seconds after it was detected gone (M89). Under
    # the honest clock the gap runs from a SIGHTING, where a sub-minute separation can only mean
    # one index walk saw both adverts for the first and only time. So the rail is required
    # exactly where the hazard is: `validate` refuses the honest clock without it.
    certificate_b_min_gap_days: float = 0.0
    # E85 (W11): K-B certifies a pair only when its FAMILY — the connected component of the
    # pairs K-B certified — is consistent with ONE unit. Every clause of K-B is pair-local, and
    # a serial developer's project reads the same way pair by pair as a broker's own re-post
    # chain; what separates them is the chain. `off` is the shipped default, `family` refuses
    # every K-B edge of an impure family, `cell` partitions the family and refuses only the
    # edges that cross a cell or sit in an inconsistent one. Each clause below is its own row
    # because each was priced separately against the 77 adjudicated families (M93).
    # W11's verification then RE-ADJUDICATED those families and inverted the reading (M99): the
    # two ceskereality families that carry 64 of the 77 refusals are one unit each, and on the
    # sealed split the guard changes nothing at all (M100). It stays as data, `off`, with its
    # structural case withdrawn; `pair` is the fourth mode E86 added, the only one whose verdict
    # does not move with the order its family arrived in (M101).
    family_guard_mode: str = "off"
    family_guard_area_tol: float = 0.01
    # How far a number the body prints may sit from the stored area and still be read as this
    # unit's headline size. `stated_areas` bounds mentions at 3x the stored value, which is wide
    # enough to pick a half-house's whole-house number (478609: 103 and 206 against a stored
    # 150) and refuse a true re-post on it.
    family_guard_area_window: float = 0.25
    family_guard_price_tol: float = 0.01
    family_guard_price_rise_max: float = 0.10
    family_guard_overlap_days: float = 0.0
    family_guard_concurrency_clause: bool = True
    family_guard_price_clause: bool = True
    # OFF, and the measurement says why: E60 already ruled that a DIFFERING agency order code is
    # evidence of nothing in either direction, and engine-wide this clause refuses E60's own
    # worked example (23201 x 486034 and 395722 x 496635 — one 43,3 m2 flat at 7 974 910 Kc
    # under codes N115815 and N118731) plus two more pairs of the identical shape in one
    # project. It buys two adjudicated MULTI families whose only separator is a code, and it
    # pays for them by contradicting a standing rule (M95).
    family_guard_ref_code_clause: bool = False
    family_guard_unit_designator_clause: bool = True
    family_guard_disposition_clause: bool = True
    # E88 (W12): the honest clock everywhere EXCEPT where the new-development hazard lives.
    # A K-B certificate inside a new-development serial-poster family is HELD in the band and
    # the pair is decided as it would be with no certificate — never rejected. `off` is the
    # shipped default; `narrow` is vocabulary AND a family of at least
    # `development_size_min_narrow`; `wide` is vocabulary OR a family of at least
    # `development_size_min_wide` OR a block density at or above
    # `development_block_density_min`. Both definitions and every constant below were written
    # down and committed BEFORE the first arm ran (E88) — they are a pre-registration, not a
    # fit, and `autodedup/development.py` carries the two term lists. REFUTED (E89, D32): the
    # rule stays here as DATA and OFF; both definitions cost more true duplicates than the
    # honest clock buys, and the sealed read makes the NARROW row worse than g6 outright.
    development_hold_mode: str = "off"
    # A marker is a property of the FAMILY, so a term has to be in at least this share of its
    # members: a serial poster re-posts one template, so a genuine project marker travels with
    # it. One member mentioning a project next door is not a development family.
    development_vocab_min_share: float = 0.50
    # A co-operative advert on its own is a flat sold as a share (`podil` + `anuita` is how a
    # družstevní byt is priced); it is a DEVELOPMENT marker only beside a family of adverts.
    development_coop_min_size: int = 3
    development_size_min_narrow: int = 3
    development_size_min_wide: int = 4
    # "Many OTHER listings at the same address block in the same category". 20 is not a new
    # number: it is E63's own fungible-catalogue block limb (C3, `context_rule_block_min`),
    # which is the only block-density bar this program has ever written down.
    development_block_density_min: int = 20
    # E61 (W8): two adverts naming a DIFFERENT unit inside one address block are two units,
    # whatever they look like. ON by default — it demotes nothing the operator or gold calls a
    # duplicate, and it is the only rule that can separate a developer's own near-identical
    # adverts. Its evidence is 3 facts in 1 development; the mechanism carries it, not the n.
    unit_designator_veto: bool = True
    # E45: the merge zone needs one UNIT-specific corroboration. K-A + interior_match_ratio >= 0.5
    # scored 88.6% (n=35) against K-A's 52.6% overall; K-B (the disjoint-window re-post shape)
    # scored 100% (n=94).
    unit_evidence_required: bool = True
    unit_interior_min: float = 0.50
    unit_rare_tokens_min: float = 2.0
    unit_containment_min: float = 0.90
    # OFF by measurement: once E46 is widened below, every merge resting on rare tokens ALONE is
    # a judged positive (19/19, HT 100%) — the 82% that argued for a support clause was the two
    # catalogue-only CROSS-portal pairs E46 now bands. Requiring a photo beside the tokens cost
    # 1.9 points of weighted recall for -0.06 of precision, so the clause stays as a dial.
    unit_rare_requires_support: bool = False
    unit_rare_support_interior_min: float = 0.01
    # E46: every hand-checked K-A false merge was a co-live advert pair whose only image
    # agreement was catalogue material (n_images_min 0, catalog_ratio_max ~1.0). The
    # same-portal/one-broker clauses are a TIGHTENER, not a requirement — the same shape appears
    # across two portals (83202 x 10515714, 83467 x 10515837, both gold not-same).
    developer_signature_guard: bool = True
    developer_signature_same_broker_only: bool = False
    developer_catalog_ratio_min: float = 0.80
    # E47: two adverts of one broker on one portal that ran side by side for WEEKS are two units
    # of one project until a shared unit number says otherwise — no image or text rule can
    # separate units photographed together (21000 x 27781 merged as K-C at interior 1.0).
    # Swept 0/3/7/14/30/60/90 days: precision is flat at 98.06% across the range and recall rises
    # with the bar, so the default sits at the wide end that still clears the 61.6-day case.
    developer_colive_guard: bool = True
    colive_overlap_days: float = 30.0
    # W8: read an advert's live window as ending at its last SIGHTING rather than at the
    # delisting-DETECTION stamp (`dataset.live_end_stamp`). True is the honest clock and the one
    # the benchmark always uses; the engine defaults to False because the truer clock is the
    # looser one for every co-live test it feeds — see `features.window_end_stamp` for the
    # measurement and the refit debt.
    # E110 (W13): the price `validate` asks for it is E84's pair-gap rail and nothing else,
    # because W11's seal measured exactly that arm (347 of 406 against g6's 300, gained 47,
    # lost 0) and the one reliable false merge it carried is a pair the OPERATOR has since
    # ruled a duplicate. Still False by default: promoting it is a generation decision.
    live_window_from_sighting: bool = False
    # E63 (W8): the band promotion the W8 verification endorsed, and the ONLY one it endorsed.
    # W8a proposed merging band pairs inside a "safe" hazard context; verification refuted that
    # — the context arm carried both of the rule's labelled errors while the context-free arm
    # carried none — and kept this: a near-identical BODY (`containment_max`), an identical
    # current PRICE, and a score at the very top of the isotonic range. On g5 it moves 347 band
    # pairs (6.47%) at 0 labelled negatives on 110 labelled pairs, 0 negative blocks in 66, and
    # 0 in 33 sealed; it selects none of the 18 operator, 63 gold or 43 structural band
    # negatives. OFF by default: the sealed split that read it had already been spent by W8a's
    # 1,476-candidate search, so this is a settings row a promotion turns on (as E57's bridge
    # pass is), not a new default.
    context_rule_enabled: bool = False
    context_rule_min_score: float = 0.9999
    context_rule_containment_min: float = 0.90
    context_rule_price_ratio_min: float = 0.995
    # C1 from the verification: the endorsed arm states no area clause and 2 of its 347 pairs
    # differ by more than 1% (max 5.3%). Adding it is a TIGHTENING — it withholds no labelled
    # pair — so the rule ships with the clause the verifier said any wider rule would need.
    context_rule_area_max: float = 0.01
    # C2: an interior-ratio arm may never rest on three photos. The arm itself is OFF (None);
    # `validate` refuses to enable it below four images, so the condition cannot be forgotten.
    context_rule_interior_min: float | None = None
    context_rule_min_images: float = 4.0
    # C3, the fungible-catalogue veto, one settings row per limb because each costs differently
    # and each cost was measured rather than assumed. On g5: the FROM price withholds 0 of the
    # 347 endorsed pairs, an ALL-stock image warrant 3 — while the block-size limb withholds
    # 112 pairs carrying 66 labelled DUPLICATES, so that limb is off and its cost is recorded
    # rather than paid. `None` disables a census limb. `image_population_min` reads the
    # LEAST-carried SHARED image (see `hazard_context.shared_image_population`).
    context_rule_from_price_veto: bool = True
    context_rule_block_min: int | None = None
    context_rule_image_population_min: int | None = 10
    # C4: a census only ever gets worse, so a promotion taken under one owes a re-read. The cap
    # is per address block, so a block that doubles overnight re-opens a few merges rather than
    # all of them; re-opening means back to the BAND, never an unmerge.
    context_rail_max_reopen_per_block: int = 8
    # E48: the per-stratum merge switch D3 asks for. A key is `<layer>|<side>` spelled exactly
    # as `evaluate.decide_stratum` spells it (K-A/K-B/K-C/model x same/cross); the value is that
    # stratum's own `t_hi`, and NULL is not "missing" but PROPOSE-ONLY — the stratum could not
    # prove the bar, so nothing in it auto-merges however high it scores or whichever certificate
    # it earned. A stratum with no entry runs on the global `t_hi`. This is the only way to ship
    # K-C|same propose-only (94.7% dev / 85.7% sealed, under the 0.97 floor) while K-B and
    # K-C|cross keep merging: certificates are read BEFORE `t_hi`, so a global cut cannot reach
    # them.
    t_hi_by_stratum: dict[str, float | None] = field(default_factory=dict)
    # E57: a refused BRIDGE (E37) is re-offered once, and applied only when the MERGED member
    # set satisfies every invariant — must-not-link included — and the edge is a certificate or
    # scores at least `bridge_min_score`. Default OFF, so E37 stands wherever a settings row does
    # not ask for it.
    bridge_apply: bool = False
    bridge_min_score: float = 0.999
    # The two image-lane sample caps `features.py` reads: CLIP is the only non-popcount quadratic
    # in the pass, so both belong in the swept row rather than in a module constant.
    clip_sample: int = 8
    phash_sample: int = 30

    # ---------------------------------------------------------------- D43 (W14, 2026-09-21)
    #
    # The operator's ruling redefines a false merge: two adverts are INDISTINGUISHABLE when no
    # stated fact tells the units apart, and merging those is the WANTED outcome; a false merge
    # is a merge ACROSS a stated fact. Every dial below is OFF by default, because g7 must stay
    # byte-replayable under `settings/w13.json` — `test_g7_replay_parity` pins it.
    #
    # E130: the gate. A merge carrying a stated fact is demoted to the band.
    d43_gate: bool = False
    # E131: the promotion. A band pair carrying no stated fact is promoted to merge.
    d43_promote: bool = False
    # E131's evidence rail: how many comparable attributes both adverts must STATE and AGREE on
    # before an ABSENCE of facts is allowed to merge them. 0 is the bare predicate (G8-B), 2 is
    # the rail (G8-A). The predicate is weakest exactly where both adverts say almost nothing,
    # and this is the direct measure of what the model score was proxying.
    d43_promote_min_agreeing: int = 0
    # G8-A2: one tight non-catalogue photo match is an ALTERNATIVE to the attribute count. A
    # shared frame is the evidence a presence count stands in for, and it does not penalise the
    # prose portals (bazos) the way counting stated fields does.
    d43_promote_photo_alternative: bool = False
    # E132: the CLUSTER-grain invariant. A group is transitive, so a pairwise gate alone lets
    # A-B and B-C both pass while A and C differ on the floor; without this limb the relaxed
    # arms carry real negatives and bad groups.
    d43_cluster_invariant: bool = False
    # E132's retired limbs. D43 reads both pairwise with a measured tolerance, so keeping them
    # at cluster grain charges one difference twice — `floor_spread` alone refused 842 of g7's
    # 858 rejected unions, because two portals disagree about `přízemí`.
    cluster_floor_spread: bool = True
    cluster_disposition: bool = True
    # E133 (N1): the portal ground-floor camps, `source -> level`. Level 1 counts the ground
    # floor, level 0 does not, and a source absent from the table has no known convention.
    # DERIVED from data by `floor_convention.measure_camps` — never hard-coded — and an empty
    # table turns the whole reading off.
    floor_camps: dict[str, int] = field(default_factory=dict)
    # How far the camps are trusted when reading the FLOOR itself. A camp is a majority
    # behaviour (84-94 % on this corpus), not a law, so the three readings are a real choice:
    #   `off`    — no joint reading at all: `floor` and `total_floors` are two facts, which is
    #              exactly the predicate `truth/labels_d43.jsonl` was built with.
    #   `joint`  — the camps power the joint `floor`+`total_floors` excuse; the floor fact keeps
    #              g7's rule, which already excuses a one-floor gap across portals.
    #   `slack`  — the floor gap is read with the convention taken out, and a residual of one
    #              is still vocabulary.
    #   `strict` — where the camps place both sources, any residual gap is a fact.
    floor_camps_reads: str = "off"
    # E134 (N2): the asking price read as a PATH. Two adverts whose price histories ever name
    # the same amount are not told apart by a momentary gap; the tolerance is what "the same
    # amount" means.
    d43_price_path: bool = False
    d43_price_path_tol: float = 0.005
    # E134's other half: two adverts live at the same time whose paths never name one another's
    # price contradict each other, and that IS a fact even on one portal.
    d43_price_colive_contradiction: bool = False
    # E135 (M199): the obec tells two adverts apart only when BOTH sides are resolved at street
    # grain or finer. Measured over 140 obce: 533 of 22,421 structurally certain duplicates are
    # recorded under two towns (a village against the district town it is advertised under), and
    # every one of them has at least one side known only to the obec or the quarter.
    d43_obec_street_grain_only: bool = False
    # E136 (N3): the area tolerance is ASYMMETRIC on purpose. Promotion — merging on the ABSENCE
    # of evidence — reads `area_band_pct` (3%). The gate and the cluster invariant — overruling
    # positive evidence the engine already certified — read this wider bar, the engine's own
    # merge-grade guard (`area_reject_pct`, 8%). None = the strict definition on both sides.
    d43_gate_area_tol: float | None = None
    # E138: the GATE's own reading, and the generalisation of E136. Demoting a merge means
    # overruling positive evidence the engine already certified, so the gate does not demote on
    # a difference that is INFERRED rather than stated, nor on one a known vocabulary or geocode
    # ambiguity explains. Promotion keeps reading every fact strictly — it has no positive
    # evidence to fall back on. Each limb names a measured loss class of the W14 arms:
    #   image facts   — `interior` and `floorplan` are a CLIP similarity and a model flag, not
    #                   anything either advert states (9 labelled duplicates).
    #   street metres — two names for one corner building, pins 0.1-4 m apart (3 duplicates,
    #                   and the town probe's `street_kills_a_merge` worked example).
    #   total floors  — a one-storey gap across a boundary the camps cannot place is the same
    #                   ground-floor ambiguity `floor` is already read with (3 duplicates).
    d43_gate_image_facts: bool = True
    # The same limb at CLUSTER grain, and it stays ON: the invariant is the only thing between
    # a development and one big group. Measured on the chosen arm, dropping it recovers 9
    # labelled duplicates and builds 4 groups holding a pair a judge called different on
    # exactly that evidence — the one trade this wave refuses.
    d43_cluster_image_facts: bool = True
    d43_street_min_distance_m: float | None = None
    d43_gate_total_floors_slack: bool = False
    # E137: the re-partitioner. A component the invariants cannot make one group is cut into
    # maximal consistent sub-groups rather than left where greedy arrival order dropped it. Off
    # = E33/E37's constrained union-find, unchanged.
    repartition: bool = False
    repartition_max_rounds: int = 4

    # ------------------------------------------------------- W15 (g8b, 2026-09-21): the repairs
    #
    # W14's adversarial read of g8 found merges ACROSS facts the predicate could not see. Each
    # row below is one of them, read as a general fact a Czech estate agent would recognise
    # rather than as a patch for the listings that exposed it. All OFF by default so
    # `settings/w13.json` (g7) and `settings/w14.json` (g8) keep replaying byte-for-byte.
    #
    # E140: the land-register parcel. A pozemek or dům advert prints the parcel the object
    # stands on, and that number IS the object's identity in the register. DISJOINT printed
    # sets are two objects; a subset is not a conflict, because an advert also names the access
    # road and the neighbour's plot. Measured on the 140-town region cohort: 1 of 22,421
    # structurally certain duplicates — and that one is two of three building plots in Paceřice
    # that share one photo set, so the reference is wrong there, not the rule — against 235 of
    # 7,354 structural negatives and 8 of the 18 groups g8 fused.
    d43_parcel_numbers: bool = False
    # E141: the accessory the flat comes WITH — parking space, cellar, garage, by number. E61
    # already reads the UNIT designator; where a developer prints none, the building's own
    # numbering of its accessories is the next thing that separates two identical flats.
    # 6 of 22,421 region certain duplicates, and all six are the ONE Turnov pair the blind
    # hand-read called different ("stání č. 47" against "stání č. 32 + kóje č. 25").
    d43_accessory_designators: bool = False
    # E142: the offered EXTENT. Three spellings of one fact — a serviced-office capacity
    # ("kancelář pro 1 osobu" at 10,890 Kč against "pro 2 pracovní místa" at 15,590), a room
    # let's room count ("Pronajmu pokoj" at 8,500 against "2 spojené pokoje" at 12,000), and a
    # parcel inventory one advert offers strictly more of than the other. The first two need
    # the price and overlap conjunction: a bare capacity regex fires on service-charge lines
    # and on "byt se hodí pro 1 osobu", and 5 of its 7 g8 hits were exactly those.
    # `d43_offered_extent_requires_price_gap` is the conjunction the W14 group attack proposed,
    # kept as a dial and measured rather than assumed. Its case was that a CAPACITY regex is
    # noisy — 5 of its 7 hits in g8 were prose — but the noise was the bare regex, and once the
    # office noun must carry the phrase the reading costs 0 of 22,421 region certain duplicates
    # and 0 of 2,052 trial labelled ones. The conjunction then only removes real catches: Regus
    # publishes its 1-person and its 2-desk product at the SAME price (43617 at 16,290 Kč,
    # 38043 at 15,490), so requiring a price gap merges two products because their prices met.
    d43_offered_extent: bool = False
    d43_offered_extent_requires_price_gap: bool = False
    d43_offered_extent_price_tol: float = 0.05
    # E143: the two-unit signature. One advert has ONE area and ONE price at any moment, so two
    # adverts whose area AND price BOTH differ beyond rounding are two units — this is what the
    # 3 % / 5 % / 60 % tolerances cannot see, because neighbouring units of one project sit
    # 0.5-2 % apart. Two rails keep it honest: an agreeing price PATH (E134) excuses a moment,
    # and a floor area BOTH bodies print excuses a stored-column basis difference (`užitná 51
    # m² (podlahová 55 m²)` against `podlahová 55 m² (užitná 51 m²)`). Measured: 58 of 22,421
    # region certain duplicates, 0 of the 1,415 carrying a shared agency ORDER code, 0 of any
    # operator-tier labelled duplicate, and 16 of the 18 fused groups.
    d43_two_unit_signature: bool = False
    d43_two_unit_area_tol: float = 0.005
    d43_two_unit_price_tol: float = 0.005
    d43_two_unit_stated_tol: float = 0.005
    # E144: E134's co-live limb needs an overlap BAR. Windows that merely touch are a re-post
    # boundary — 38 of the 63 raw pairs — and a 3-day bar removes every one of them. It also
    # needs a SIDE: all 25 contradictions the W14 group attack found are same-portal, and
    # across portals the limb only ever overrules E134's own price-path excuse, which is where
    # its cost lives (7 dev and 3 sealed labelled duplicates, against 0 for the same-portal
    # reading).
    d43_price_colive_min_overlap_days: float = 0.0
    d43_price_colive_same_source_only: bool = False
    # E145: WHOSE ground-floor convention is it? N1's same-portal clause assumes one portal is
    # one convention, and the data refuses that: among same-portal KNOWN duplicates a one-storey
    # gap runs at 7.5 % on sreality, 6.8 % on ceskereality, 15 % on realitymix and 23.5 % on
    # bazos. The convention belongs to the FEED, and the feed is the broker. `portal` is g7/g8's
    # reading; `broker` makes the same-portal clause need a shared `broker_key`, which is what a
    # broker-feed aggregator needs and what stops the V Aleji 131 m² 4+1 being cut in two.
    floor_same_source_feed: str = "portal"
    # E11 as a dial rather than a module constant, so an arm can open it without a monkeypatch.
    min_evidence_families: int = 2
    # E27/N4: the batch build loads the operator's permanent negatives. g7 did not — its
    # `n_must_not_link = 45` is the E61 designator veto set and nothing else — so a pass that
    # loads none now has to say so out loud instead of looking identical to one that did.
    operator_must_not_link: bool = True

    # ------------------------------------------ W16 (g8c, 2026-09-21): identity is DEMONSTRATED
    #
    # D50. g8b promotes a band pair unless a reader FINDS a distinguishing fact, and that is
    # fail-open: its safety is bounded by how much Czech prose the readers cover, and every new
    # cohort has produced a form none of them knew — `B1.2.2` against `B1.2.3`, plots 7/8/9 of
    # one parcelling, a garage block G3 against G4, 58,90 m² against 58,70 m². Silence is not
    # evidence. Everything below is OFF by default so w13/w14/w15 keep replaying byte-for-byte.
    #
    # E150: the reader that knows no form. Two adverts for two units of one project are written
    # from ONE template, so they align everywhere except where the unit is named; the alignment
    # says WHERE to look and the token says whether what is written there can name a unit.
    d43_body_align: bool = False
    # How much of the two bodies must align before any position is read. 0.60 is where the
    # Rokytná pair (one shared first sentence, then two different paragraphs) still aligns.
    d43_body_align_min_ratio: float = 0.60
    # E151: the street the BODY names, for the adverts whose resolved street key is missing —
    # two Olomouc office blocks, one on Litovelská and one on třída 28. října, same obec, no
    # street key on either side, and the prose is the only place either street is written.
    d43_prose_street: bool = False
    # E152: the obec the BODY names. E135 suppresses the obec fact below street grain, which is
    # right for a village advertised under its district town and wrong for two adverts that each
    # PRINT their own different town (Droždín against Oplocany u Tovačova). Reading the printed
    # name restores the fact without re-opening the 533 recorded-under-two-towns duplicates.
    d43_prose_obec: bool = False
    # E153: the printed headline area, read WITHOUT the stored column. 16 % of the corpus prints
    # a headline area outside `stated_areas`' stored-column window, because the portal stored a
    # terrace, a cellar or the plot. Equality is the ROUNDING rule, not a tolerance: two numbers
    # agree when they agree within half of the coarser one's last printed digit.
    d43_printed_area: bool = False
    # E154: E145 fails CLOSED. `floor_same_source_feed="broker"` drops the same-portal one-storey
    # fact wherever a broker key is null — and bazos, bezrealitky and maxima are 100 % null — so
    # floors 3 and 4 of one new-build fuse. A null key is UNKNOWN, not "a different feed": the
    # fact stands unless both keys are known AND different.
    floor_feed_unknown_closed: bool = False
    # E155: the parcel forms the narrow keyword misses. Fail-safe in both directions.
    d43_parcel_forms_wide: bool = False
    # E156: the re-partitioner may not drop a member that carries NO fact against the group it
    # is being separated from. Measured on the region cohort: 8 of g8b's 38 lost certain
    # duplicates are exactly that — Penzion Horálka, same 374 m², same price, same body, one
    # side sreality and one mmreality, cut to two singletons by a conflict elsewhere.
    repartition_keep_factless: bool = False

    # E157 (A): the key facts must be POSITIVELY EQUAL before a band pair may be promoted.
    demonstrate_identity: bool = False
    # A price gap the two adverts never reconcile is only excusable when they were never on sale
    # together: a cut between two sequential postings is one unit, two co-live prices are two.
    demonstrate_price_colive_days: float = 3.0
    # And the slack must be read RELATIVE to the two lives. Two Okružní garages at 1,190,000 and
    # 1,240,000 were first sighted seven minutes apart and the cheaper one died two days later:
    # its whole life overlapped the other's, and "1.59 days" made that look like a re-post tail.
    # A re-post boundary is a small fraction of both windows; a shared life is all of one.
    demonstrate_price_colive_fraction: float = 0.25
    demonstrate_require_disposition: bool = True
    demonstrate_require_obec: bool = True
    # E158 (B): unit-grade corroboration, the positive evidence that these two galleries or
    # bodies are of ONE home. `unit` is the strict reading (a tight non-catalogue photo file, a
    # near-identical body, or a shared rare order code); `two_of` also accepts two of the wider
    # list; `development_only` asks for `unit` inside a development and nothing outside one.
    # Every mode is a SUPERSET of `unit`, which is what makes S ⊆ M ⊆ L an identity rather than
    # a measurement.
    # E157 at CLUSTER grain, price limb only. A group is built transitively, so a pairwise
    # filter alone lets two co-live prices into one group through a third advert. Only the
    # price limb travels here: the others already have a fact of their own at this grain, and
    # only this one has D49's ratio mechanism to keep it away from the co-op share and the
    # dražba figure.
    demonstrate_cluster_price: bool = False
    corroboration: str = "off"
    corroboration_body_containment: float = 0.80
    corroboration_min: int = 2

    # --- W17 / S2: exactness where identity is CLAIMED (E160-E165) -------------------------
    # E160: "price equal" at promotion is EXACT, or one price on the other's recorded path.
    # 5 % is the width of two portals carrying one order; it is also the width of a developer's
    # next unit (Kozolupy 11,250,000 against 11,500,000, 2.2 %, one cluster of 25). A tolerance
    # that cannot tell those apart is not a demonstration of identity.
    demonstrate_price_exact: bool = False
    # What a portal's ROUNDING costs, and nothing more: 0.2 % covers 11,250,000 written as
    # `11,25 mil.` and still refuses `11,5`.
    demonstrate_price_exact_tol: float = 0.002
    # E160: "area equal" is decided by what the two BODIES print whenever both print anything.
    # A stored integer column rescues 75,52 against 75,64 — both portals store 76 — and that is
    # the Chotěšov twin. The column stays the reading only where a body states nothing.
    demonstrate_area_printed_decides: bool = False
    d43_printed_area_decimals_decide: bool = False
    # E161: the printed unit code, read WHOLE and in the bare form. `wide` adds the Roman
    # numeral, the number word and the single letter, each behind an explicit marker.
    d43_unit_codes: bool = False
    d43_unit_codes_wide: bool = False
    # E162: a fact ONE side prints and the other is silent about. Outside a development that is
    # E12's missing datum and no refusal; inside one it is the whole hazard, so the silent side
    # fails closed. `development_context_mode` says how narrowly "inside" is read — `vocab` is
    # `PROJECT_TERMS` on either side (the skeptic measured that at 41.8 % of all pairs, which is
    # not a context), `narrow` asks for the vocabulary on BOTH sides AND a second marker.
    demonstrate_onesided: bool = False
    development_context_mode: str = "off"
    # E163: the healed generic reader — charges, contract terms, year-less dates, short order
    # codes, inventory multipliers, metre dimensions, ranges, and NP against patro.
    d43_body_align_heal: bool = False
    # E164: 87 % of the A-limb's refusals are a MISSING reading, not a disagreement. A missing
    # reading may be waived where the pair carries evidence only one unit has.
    demonstrate_recover_missing: bool = False
    demonstrate_recover_min_photos: float = 3.0
    demonstrate_recover_body_containment: float = 0.98
    # E165: E143's two-unit signature reads two numbers as SIMULTANEOUS. Two sequential
    # postings never were, and a price cut plus a re-parsed area is what a re-post looks like.
    d43_two_unit_requires_colive: bool = False
    # E166: the storey the BODY prints, read under the same convention rule as the column.
    d43_prose_floor: bool = False

    # --- W18 / S3: what the cohort-5 confirmation named (E180-E185, D57) --------------------
    # E180: the ground-floor convention is a difference BETWEEN camps. Inside one camp — and a
    # portal is always inside its own — a one-storey gap is not vocabulary, it is a storey.
    # `d43_floor_within_camp_colive` keeps the excuse for two SEQUENTIAL postings, where the
    # gap is one portal's parse drifting between re-posts (29 bazos re-posts of one Dašice rent
    # advert drift 0/1); two adverts on sale TOGETHER have no such excuse.
    d43_floor_within_camp: bool = False
    d43_floor_within_camp_colive: bool = False
    # E181: the storey a PLACEMENT clause states of the offered unit, and the storey written in
    # words rather than digits. Both are read under E180's camp rule; the worded pair is not,
    # because `přízemí` is the ground floor on every portal.
    d43_subject_floor: bool = False
    d43_ground_vs_upper: bool = False
    # E182: the parcel area read EXACTLY for a house or a plot, and read through a truncating
    # carrier by its residue instead of being blanked.
    d43_plot_area_exact: bool = False
    # Two portals measuring one parcel differ in the last digit; two parcels of a parcelling
    # differ by more (Ráby's 981 / 998 / 1,001 m²). An absolute metre, not a percentage — 2 %
    # of 1,000 m² is 20 m² and 2 % of 200 m² is 4, and a tape measure does not scale.
    d43_plot_exact_abs: float = 1.0
    d43_plot_truncation_residue: bool = False
    # E183: the catalogue row that is THIS advert's, selected by its own area and price, and
    # the widest parcel keyword set (`číslo pozemku`, the Czech word order idnes uses).
    d43_parcel_table: bool = False
    # E184: a stated count of dwelling units. `colive_price` is D49's refusal kept intact — the
    # bare co-live price limb stays refused, and only the conjunction with a stated count is
    # read; `always` reads the count alone.
    d43_stated_unit_count: str = "off"
    # E185: a price MOVE between two postings that were never on sale together is one price
    # path, when the rest of the identity is demonstrated. This is the standing ruling about
    # re-lists applied to the `price` FACT, which until now only `price_demonstrated` honoured.
    d43_price_sequential_path: bool = False
    d43_price_sequential_same_feed: bool = False
    d43_price_sequential_containment: float = 0.9
    d43_price_sequential_min_photos: float = 3.0
    # D57: exactness where identity is CLAIMED reaches the PATH too. `price_paths_agree` runs at
    # `d43_price_path_tol` (0.5 %), which is looser than the exact bar and silently readmits
    # every pair the exact bar refuses — the Ráby packages at 10,999,000 and 10,988,000 are
    # 0.1 % apart and meet through it. Rounding-aware means the coarser number is the finer one
    # rounded at the coarser's OWN granularity, not a percentage.
    demonstrate_price_path_exact: bool = False
    demonstrate_price_rounding_aware: bool = False
    # E186: the area the body LEADS with. `printed_area` compares SETS, and the advert of one
    # third of a parcel names the whole parcel while explaining the split, so the two sets meet
    # on the number that is not the offer. What an advert leads with is what it sells.
    d43_offer_area: bool = False

    def __post_init__(self) -> None:
        # A sweep file is JSON, so a tuple field arrives as a list: normalise before validating.
        self.vocabulary_attr_keys = tuple(str(key) for key in self.vocabulary_attr_keys)
        self.numeral_conflict_units = tuple(str(unit) for unit in self.numeral_conflict_units)
        self.t_hi_by_stratum = {
            str(key): (None if value is None else float(value))
            for key, value in dict(self.t_hi_by_stratum).items()
        }
        self.floor_camps = {str(key): int(value)
                            for key, value in dict(self.floor_camps).items()}
        self.validate()

    def validate(self) -> None:
        """Every ordering a sweep can break, named — `--settings s.json` must fail at load."""
        if not 0.0 < self.area_band_tol < 1.0:
            raise ValueError(f"area_band_tol must be in (0, 1): {self.area_band_tol}")
        if not 0.0 <= self.area_band_pct <= self.area_reject_pct:
            raise ValueError(
                f"area_band_pct must be in [0, area_reject_pct]: {self.area_band_pct} "
                f"> {self.area_reject_pct}"
            )
        if not 0.0 <= self.t_lo <= self.t_hi <= 1.0:
            raise ValueError(f"thresholds must satisfy 0 <= t_lo <= t_hi <= 1: "
                             f"t_lo={self.t_lo} t_hi={self.t_hi}")
        if not 0.0 <= self.store_floor <= self.t_lo:
            raise ValueError(f"store_floor must be in [0, t_lo]: {self.store_floor} > {self.t_lo}")
        for name in ("catalog_df", "anchor_images", "max_block_size",
                     "max_candidates_per_listing", "band_bits", "simhash_bands",
                     "clip_sample", "phash_sample"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1: {getattr(self, name)}")
        if self.simhash_bands * self.band_bits > 64:
            raise ValueError(f"cannot split 64 bits into {self.simhash_bands} bands "
                             f"of {self.band_bits}")
        if self.phash_tight > self.phash_loose:
            raise ValueError(f"phash_tight must not exceed phash_loose: "
                             f"{self.phash_tight} > {self.phash_loose}")
        if self.max_cluster_size < 2:
            raise ValueError(f"max_cluster_size must be at least 2: {self.max_cluster_size}")
        if self.cluster_area_spread <= 0.0:
            raise ValueError(f"cluster_area_spread must be positive: {self.cluster_area_spread}")
        if self.rare_token_df < 0 or self.text_min_chars < 0:
            raise ValueError("rare_token_df and text_min_chars must not be negative")
        if self.min_evidence_families < 1:
            raise ValueError(
                f"min_evidence_families must be at least 1: {self.min_evidence_families}"
            )
        if self.d43_promote_min_agreeing < 0:
            raise ValueError(
                f"d43_promote_min_agreeing must not be negative: "
                f"{self.d43_promote_min_agreeing}"
            )
        if not 0.0 < self.d43_price_path_tol < 1.0:
            raise ValueError(
                f"d43_price_path_tol must be in (0, 1): {self.d43_price_path_tol}"
            )
        if self.d43_gate_area_tol is not None and not (
            self.area_band_pct <= self.d43_gate_area_tol <= self.area_reject_pct
        ):
            # The gate is the PERMISSIVE side of E136: wider than promotion reads, and never
            # wider than the guard that let the merge through in the first place.
            raise ValueError(
                f"d43_gate_area_tol must lie in [area_band_pct, area_reject_pct]: "
                f"{self.d43_gate_area_tol}"
            )
        if (self.d43_street_min_distance_m is not None
                and not 0.0 < self.d43_street_min_distance_m <= 250.0):
            raise ValueError(
                f"d43_street_min_distance_m must be in (0, 250] metres: "
                f"{self.d43_street_min_distance_m}"
            )
        if self.repartition_max_rounds < 1:
            raise ValueError(
                f"repartition_max_rounds must be at least 1: {self.repartition_max_rounds}"
            )
        for name in ("d43_two_unit_area_tol", "d43_two_unit_price_tol",
                     "d43_two_unit_stated_tol", "d43_offered_extent_price_tol"):
            value = getattr(self, name)
            if not 0.0 < value < 1.0:
                raise ValueError(f"{name} must be in (0, 1): {value}")
        # The signature's whole case is that it sees BELOW the engine's own area guard: a
        # tolerance at or above `area_band_pct` reads nothing the `area` fact does not already.
        if self.d43_two_unit_area_tol >= self.area_band_pct:
            raise ValueError(
                "d43_two_unit_area_tol must sit below area_band_pct — above it the signature "
                f"is the `area` fact spelled twice: {self.d43_two_unit_area_tol} >= "
                f"{self.area_band_pct}"
            )
        if self.d43_price_colive_min_overlap_days < 0.0:
            raise ValueError(
                "d43_price_colive_min_overlap_days must not be negative: "
                f"{self.d43_price_colive_min_overlap_days}"
            )
        if self.floor_same_source_feed not in ("portal", "broker"):
            raise ValueError(
                f"floor_same_source_feed must be portal/broker: {self.floor_same_source_feed}"
            )
        if self.d43_promote_photo_alternative and not self.d43_promote:
            raise ValueError("d43_promote_photo_alternative needs d43_promote")
        if self.corroboration not in ("off", "unit", "two_of", "development_only"):
            raise ValueError(
                "corroboration must be off/unit/two_of/development_only: "
                f"{self.corroboration}"
            )
        if not 0.0 < self.d43_body_align_min_ratio <= 1.0:
            raise ValueError(
                f"d43_body_align_min_ratio must be in (0, 1]: {self.d43_body_align_min_ratio}"
            )
        if not 0.0 < self.corroboration_body_containment <= 1.0:
            raise ValueError(
                "corroboration_body_containment must be in (0, 1]: "
                f"{self.corroboration_body_containment}"
            )
        if self.corroboration_min < 1:
            raise ValueError(f"corroboration_min must be at least 1: {self.corroboration_min}")
        if self.demonstrate_price_colive_days < 0.0:
            raise ValueError(
                "demonstrate_price_colive_days must not be negative: "
                f"{self.demonstrate_price_colive_days}"
            )
        if not 0.0 <= self.demonstrate_price_colive_fraction <= 1.0:
            raise ValueError(
                "demonstrate_price_colive_fraction must be in [0, 1]: "
                f"{self.demonstrate_price_colive_fraction}"
            )
        # The ladder is nested by CONSTRUCTION, and the constructor is where that is enforced:
        # corroboration is a filter on promotion, so it cannot be asked for without the
        # demonstration it refines, or S ⊆ M ⊆ L would stop being an identity (E159).
        if self.corroboration != "off" and not self.demonstrate_identity:
            raise ValueError("corroboration needs demonstrate_identity")
        if self.demonstrate_cluster_price and not self.demonstrate_identity:
            raise ValueError("demonstrate_cluster_price needs demonstrate_identity")
        if self.development_context_mode not in ("off", "vocab", "narrow", "template"):
            raise ValueError(
                "development_context_mode must be off/vocab/narrow/template: "
                f"{self.development_context_mode}"
            )
        if not 0.0 <= self.demonstrate_price_exact_tol < 1.0:
            raise ValueError(
                "demonstrate_price_exact_tol must be in [0, 1): "
                f"{self.demonstrate_price_exact_tol}"
            )
        if self.demonstrate_onesided and self.development_context_mode == "off":
            raise ValueError("demonstrate_onesided needs a development_context_mode")
        for name in ("demonstrate_price_exact", "demonstrate_area_printed_decides",
                     "demonstrate_onesided", "demonstrate_recover_missing"):
            if getattr(self, name) and not self.demonstrate_identity:
                raise ValueError(f"{name} needs demonstrate_identity")
        if self.d43_unit_codes_wide and not self.d43_unit_codes:
            raise ValueError("d43_unit_codes_wide needs d43_unit_codes")
        if self.d43_body_align_heal and not self.d43_body_align:
            raise ValueError("d43_body_align_heal needs d43_body_align")
        if self.floor_camps_reads not in ("off", "joint", "slack", "strict"):
            raise ValueError(
                f"floor_camps_reads must be off/joint/slack/strict: {self.floor_camps_reads}"
            )
        if set(self.floor_camps.values()) - {0, 1}:
            raise ValueError(f"floor_camps levels must be 0 or 1: {sorted(set(self.floor_camps.values()))}")
        if self.d43_stated_unit_count not in ("off", "colive_price", "always"):
            raise ValueError(
                "d43_stated_unit_count must be off/colive_price/always: "
                f"{self.d43_stated_unit_count}"
            )
        if self.d43_floor_within_camp_colive and not self.d43_floor_within_camp:
            raise ValueError("d43_floor_within_camp_colive needs d43_floor_within_camp")
        if self.d43_plot_exact_abs < 0.0:
            raise ValueError(f"d43_plot_exact_abs must be >= 0: {self.d43_plot_exact_abs}")
        if self.d43_price_sequential_same_feed and not self.d43_price_sequential_path:
            raise ValueError("d43_price_sequential_same_feed needs d43_price_sequential_path")
        for name in ("demonstrate_price_path_exact", "demonstrate_price_rounding_aware"):
            if getattr(self, name) and not self.demonstrate_price_exact:
                raise ValueError(f"{name} needs demonstrate_price_exact")
        from autodedup.features import ATTR_KEYS, CONFLATED_ATTR_KEYS, NUMERAL_TOLERANCE

        unknown_attrs = sorted(
            set(self.vocabulary_attr_keys) - set(ATTR_KEYS) - set(CONFLATED_ATTR_KEYS)
        )
        if unknown_attrs:
            raise ValueError(f"vocabulary_attr_keys names no attribute: {', '.join(unknown_attrs)}")
        unknown_units = sorted(set(self.numeral_conflict_units) - set(NUMERAL_TOLERANCE))
        if unknown_units:
            raise ValueError(f"numeral_conflict_units names no slot: {', '.join(unknown_units)}")
        from autodedup.stock import COMBINE_MODES, MIN_CARRIER_BAR

        if self.catalog_carrier_combine not in COMBINE_MODES:
            raise ValueError(
                f"catalog_carrier_combine must be one of {COMBINE_MODES}: "
                f"{self.catalog_carrier_combine}"
            )
        if not 0.0 <= self.catalog_carrier_area_tol < 1.0:
            raise ValueError(
                f"catalog_carrier_area_tol must be in [0, 1): {self.catalog_carrier_area_tol}"
            )
        if not 0.0 < self.catalog_carrier_coverage_min <= 1.0:
            raise ValueError(
                "catalog_carrier_coverage_min must be in (0, 1]: "
                f"{self.catalog_carrier_coverage_min}"
            )
        limbs = ("catalog_min_broker_carriers", "catalog_min_source_carriers",
                 "catalog_min_block_carriers")
        for name in limbs:
            value = getattr(self, name)
            # A bar of one is vacuous: every frame has a carrier, so nothing would ever be stock.
            if value is not None and value < MIN_CARRIER_BAR:
                raise ValueError(f"{name} must be at least {MIN_CARRIER_BAR} or null: {value}")
        if self.catalog_carrier_aware and not any(getattr(self, name) is not None
                                                  for name in limbs):
            raise ValueError(
                "catalog_carrier_aware needs at least one carrier limb: with none configured "
                "the switch is inert and E9 runs exactly as it does with it off"
            )
        if self.certificate_b_min_gap_days < 0.0:
            raise ValueError(
                "certificate_b_min_gap_days must not be negative: "
                f"{self.certificate_b_min_gap_days}"
            )
        for name in ("certificate_b_min_images", "certificate_b_min_matched_images"):
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} must not be negative: {getattr(self, name)}")
        if (self.certificate_b_min_matched_images > 0.0
                and self.certificate_b_min_matched_images > self.certificate_b_min_images):
            raise ValueError(
                "certificate_b_min_matched_images cannot exceed certificate_b_min_images: a "
                "matched set is a subset of the smaller gallery "
                f"({self.certificate_b_min_matched_images} > {self.certificate_b_min_images})"
            )
        # E110 (W13): the honest clock's price is E84, and it is a price a SEALED read has
        # measured. E87 and E89 both refused a widening because the arm behind it had never
        # been read on a holdout; this one has. W11's seal read exactly this arm — the honest
        # clock, the E84 one-minute pair gap, NO image floor, no family guard, no hold — and
        # measured 347 of 406 sealed reliable labelled duplicates merged against g6's 300,
        # McNemar gained 47 lost 0 (p ~ 7.1e-15), band 4,524 against 4,907, with ONE reliable
        # false merge at pair, block and family grain: the sealed gold negative
        # 522698 x 13221982 (M98/D30 i). On 2026-09-20 the OPERATOR ruled that pair, and its
        # dev-side twin 555448 x 18626270, `same` in the validation UI — and operator labels
        # outrank gold everywhere in this program — so the one number that refused the arm is
        # gone and the sealed read stands as its price. The E65 image floor is no longer
        # required: it withheld 89% of the one-unit re-post chains' merges for a hazard the
        # operator has now ruled is not one (M96, D24 ii).
        #
        # What this acceptance RESTS ON, stated so a later session can revoke it without
        # re-deriving it: one operator ruling about ONE development, the two ceskereality
        # Rezidence K Botici serial-poster families F518656 and F521778. What REVOKES it: an
        # operator NEGATIVE inside a K-B family — a pair the operator rules is two units that
        # K-B certifies under the honest clock. That is a settings change, not a code change:
        # the floor rows are still read, `certificate_b` still enforces them, and putting a
        # number back in `certificate_b_min_images` restores the W9 arm exactly.
        if self.live_window_from_sighting and self.certificate_b_min_gap_days <= 0.0:
            raise ValueError(
                "live_window_from_sighting needs the E84 gap rail: under the honest clock a "
                "once-seen advert's live window is a point, so every pair of once-seen adverts "
                "is disjoint by construction and K-B certifies a 2.8-second separation as a "
                "re-post"
            )
        from autodedup.development import MODES as DEVELOPMENT_HOLD_MODES

        if self.development_hold_mode not in DEVELOPMENT_HOLD_MODES:
            raise ValueError(
                f"development_hold_mode must be one of {DEVELOPMENT_HOLD_MODES}: "
                f"{self.development_hold_mode}"
            )
        if not 0.0 < self.development_vocab_min_share <= 1.0:
            raise ValueError(
                "development_vocab_min_share must be in (0, 1]: "
                f"{self.development_vocab_min_share}"
            )
        # A family has at least two adverts by construction, so a size bar below two is
        # vacuous: it would hold every K-B certificate the engine ever issues.
        for name in ("development_coop_min_size", "development_size_min_narrow",
                     "development_size_min_wide"):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} must be at least 2: {getattr(self, name)}")
        if self.development_block_density_min < 1:
            raise ValueError(
                "development_block_density_min must be at least 1: "
                f"{self.development_block_density_min}"
            )
        # The hold is not a second spelling of E84: it withholds a certificate the honest clock
        # manufactured, and E84 removes the degenerate window that manufactures it. A hold
        # without the gap rail would still certify two adverts one index walk saw once each.
        if self.development_hold_mode != "off" and self.certificate_b_min_gap_days <= 0.0:
            raise ValueError(
                "development_hold_mode needs the E84 gap rail (certificate_b_min_gap_days): "
                "the hold removes a certificate inside a development family, and E84 removes "
                "the one it should never have issued anywhere"
            )
        from autodedup.family import MODES as FAMILY_GUARD_MODES

        if self.family_guard_mode not in FAMILY_GUARD_MODES:
            raise ValueError(
                f"family_guard_mode must be one of {FAMILY_GUARD_MODES}: {self.family_guard_mode}"
            )
        for name in ("family_guard_area_tol", "family_guard_area_window",
                     "family_guard_price_tol", "family_guard_price_rise_max"):
            value = getattr(self, name)
            if not 0.0 <= value < 1.0:
                raise ValueError(f"{name} must be in [0, 1): {value}")
        if self.family_guard_overlap_days < 0.0:
            raise ValueError(
                f"family_guard_overlap_days must not be negative: {self.family_guard_overlap_days}"
            )
        if not 0.0 <= self.unit_interior_min <= 1.0:
            raise ValueError(f"unit_interior_min must be in [0, 1]: {self.unit_interior_min}")
        if not 0.0 <= self.unit_containment_min <= 1.0:
            raise ValueError(f"unit_containment_min must be in [0, 1]: {self.unit_containment_min}")
        if not 0.0 <= self.developer_catalog_ratio_min <= 1.0:
            raise ValueError(
                f"developer_catalog_ratio_min must be in [0, 1]: {self.developer_catalog_ratio_min}"
            )
        if not 0.0 <= self.unit_rare_support_interior_min <= 1.0:
            raise ValueError(
                f"unit_rare_support_interior_min must be in [0, 1]: "
                f"{self.unit_rare_support_interior_min}"
            )
        if self.colive_overlap_days < 0.0:
            raise ValueError(
                f"colive_overlap_days must not be negative: {self.colive_overlap_days}"
            )
        if self.unit_rare_tokens_min < 0.0:
            raise ValueError(
                f"unit_rare_tokens_min must not be negative: {self.unit_rare_tokens_min}"
            )
        from autodedup.decide import CERTIFICATES, SIDES

        layers = (*CERTIFICATES, "model")
        for key, value in self.t_hi_by_stratum.items():
            layer, _, side = key.partition("|")
            if layer not in layers or side not in SIDES:
                raise ValueError(
                    f"t_hi_by_stratum key must be <layer>|<side> with layer in "
                    f"{layers} and side in {SIDES}: {key}"
                )
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"t_hi_by_stratum[{key}] must be in [0, 1] or null: {value}")
        if not 0.0 <= self.context_rule_min_score <= 1.0:
            raise ValueError(
                f"context_rule_min_score must be in [0, 1]: {self.context_rule_min_score}"
            )
        for name in ("context_rule_containment_min", "context_rule_price_ratio_min",
                     "context_rule_area_max"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]: {value}")
        if self.context_rule_interior_min is not None:
            if not 0.0 <= self.context_rule_interior_min <= 1.0:
                raise ValueError(
                    f"context_rule_interior_min must be in [0, 1] or null: "
                    f"{self.context_rule_interior_min}"
                )
            if self.context_rule_min_images < CONTEXT_RULE_MIN_IMAGES_FLOOR:
                raise ValueError(
                    "an interior-ratio arm needs at least "
                    f"{CONTEXT_RULE_MIN_IMAGES_FLOOR:g} images (C2 of the W8 verification): "
                    f"context_rule_min_images={self.context_rule_min_images}"
                )
        for name in ("context_rule_block_min", "context_rule_image_population_min"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1 or null: {value}")
        if self.context_rail_max_reopen_per_block < 1:
            raise ValueError(
                "context_rail_max_reopen_per_block must be at least 1: "
                f"{self.context_rail_max_reopen_per_block}"
            )
        if not 0.0 <= self.bridge_min_score <= 1.0:
            raise ValueError(f"bridge_min_score must be in [0, 1]: {self.bridge_min_score}")
        if self.max_attr_contradictions <= 0.0:
            raise ValueError(
                f"max_attr_contradictions must be positive: {self.max_attr_contradictions}"
            )

    def band_width(self) -> float:
        """`w = -ln(1 - t)` — the log-band width lifted from `toolkit/dedup_candidates_sql.py`,
        which turns a ±t range tolerance into an equality lookup on `floor(ln(area)/w)`."""
        if not 0.0 < self.area_band_tol < 1.0:
            raise ValueError(f"area_band_tol must be in (0, 1): {self.area_band_tol}")
        return -math.log(1.0 - self.area_band_tol)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Settings":
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"unknown settings keys: {', '.join(unknown)}")
        return cls(**raw)

    @classmethod
    def from_json(cls, path: str | Path) -> "Settings":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
