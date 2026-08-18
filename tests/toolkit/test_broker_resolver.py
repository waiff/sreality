"""Unit tests for the pure cross-source broker identity-resolution rules."""

from __future__ import annotations

from toolkit import broker_resolver as R


def test_normalize_email():
    assert R.normalize_email("  Jan.Novak@RE-MAX.cz ") == "jan.novak@re-max.cz"
    assert R.normalize_email("info@mmreality.cz") == "info@mmreality.cz"
    assert R.normalize_email("not-an-email") is None
    assert R.normalize_email("a@nodot") is None  # domain needs a dot
    assert R.normalize_email("@x.cz") is None
    assert R.normalize_email("") is None
    assert R.normalize_email(None) is None


def test_email_domain():
    assert R.email_domain("denisa.dubinova@iopartners.com") == "iopartners.com"
    assert R.email_domain("bad") is None


def test_normalize_phone():
    assert R.normalize_phone("+420 731 404 040") == "420731404040"
    assert R.normalize_phone("731404040") == "420731404040"      # bare CZ national -> +420
    assert R.normalize_phone("420731404040") == "420731404040"
    assert R.normalize_phone("12345") is None                    # too short
    assert R.normalize_phone(None) is None


def test_is_free_provider():
    free = ["gmail.com", "Seznam.cz"]
    assert R.is_free_provider("gmail.com", free) is True
    assert R.is_free_provider("SEZNAM.CZ", free) is True
    assert R.is_free_provider("re-max.cz", free) is False
    assert R.is_free_provider(None, free) is False


def test_name_key_order_and_diacritics_insensitive():
    assert R.name_key("Jan Novák") == R.name_key("novak jan")
    assert R.names_match("Jan Novák", "Novák Jan") is True
    assert R.names_match("Jan Novák", "Petr Svoboda") is False
    assert R.names_match(None, None) is False  # unknown names never corroborate


def _ids(*specs):
    return [R.Identity(i, s, n) for (i, s, n) in specs]


def test_two_independent_bridges_auto_merge():
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"))
    bridges = [R.Bridge(1, 2, "email", "jan@x.cz"), R.Bridge(1, 2, "phone", "420600111222")]
    d = R.decide_merges(ids, bridges, ["sreality", "idnes"])
    assert d.auto_merge_groups == [[1, 2]]
    assert d.review_pairs == []


def test_single_bridge_plus_name_match_auto_merges():
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Novak Jan"))
    d = R.decide_merges(ids, [R.Bridge(1, 2, "phone", "420600111222")], ["sreality", "idnes"])
    assert d.auto_merge_groups == [[1, 2]]


def test_single_bridge_name_mismatch_is_dismissed_not_queued():
    # A recycled/ported phone shared by two DIFFERENT people: must NOT auto-merge.
    # Since 2026-08-18 it is not queued either — two names with nothing in common is
    # a verdict, not a question (it used to land in review_pairs).
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Petr Svoboda"))
    d = R.decide_merges(ids, [R.Bridge(1, 2, "phone", "420600111222")], ["sreality", "idnes"])
    assert d.auto_merge_groups == []
    assert d.review_pairs == []
    assert d.dismiss_pairs == [(1, 2)]


def test_same_source_pair_never_bridges():
    ids = _ids((1, "sreality", "Jan Novak"), (2, "sreality", "Jan Novak"))
    d = R.decide_merges(ids, [R.Bridge(1, 2, "email", "jan@x.cz"), R.Bridge(1, 2, "phone", "420600111222")],
                        ["sreality", "idnes"])
    assert d.auto_merge_groups == []
    assert d.review_pairs == []


def test_disabled_source_queues_even_with_two_bridges():
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"))
    bridges = [R.Bridge(1, 2, "email", "jan@x.cz"), R.Bridge(1, 2, "phone", "420600111222")]
    d = R.decide_merges(ids, bridges, ["sreality"])  # idnes not enabled
    assert d.auto_merge_groups == []
    assert d.review_pairs == [(1, 2)]


def test_oversized_component_downgraded_to_review():
    # A chain of 7 corroborated cross-source identities exceeds the auto-merge cap;
    # the whole component must be queued, not silently fused.
    sources = ["sreality", "idnes", "bazos", "remax", "bezrealitky", "maxima", "mmreality"]
    ids = _ids(*[(i + 1, sources[i], "Jan Novak") for i in range(7)])
    bridges = []
    for i in range(6):
        bridges.append(R.Bridge(i + 1, i + 2, "email", f"e{i}@x.cz"))
        bridges.append(R.Bridge(i + 1, i + 2, "phone", f"42060000{i:04d}"))
    d = R.decide_merges(ids, bridges, sources)
    assert d.auto_merge_groups == []
    assert len(d.review_pairs) > 0


def test_no_bridges_is_noop():
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"))
    d = R.decide_merges(ids, [], ["sreality", "idnes"])
    assert d.auto_merge_groups == []
    assert d.review_pairs == []


# --- the suppression rail (migration 401) -------------------------------------


def test_a_suppressed_pair_reaches_neither_auto_merge_nor_review():
    """The gap D5 closes: the sweep re-derives every bridge from scratch, so an
    unmerged pair came back the next night. It must not fall through to REVIEW
    either — re-proposing a pair the operator already rejected asks them the same
    question every sweep, which is the same failure wearing a queue row."""
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"))
    bridges = [R.Bridge(1, 2, "email", "jan@x.cz"), R.Bridge(1, 2, "phone", "420600111222")]
    d = R.decide_merges(ids, bridges, ["sreality", "idnes"], suppressed_pairs={(1, 2)})
    assert d.auto_merge_groups == []
    assert d.review_pairs == []
    assert d.suppressed == [(1, 2)]


def test_the_same_evidence_still_auto_merges_when_not_suppressed():
    """The control: suppression is the ONLY difference between these two runs."""
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"))
    bridges = [R.Bridge(1, 2, "email", "jan@x.cz"), R.Bridge(1, 2, "phone", "420600111222")]
    d = R.decide_merges(ids, bridges, ["sreality", "idnes"], suppressed_pairs=set())
    assert d.auto_merge_groups == [[1, 2]] and d.suppressed == []


def test_suppressing_one_pair_leaves_the_others_alone():
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"),
               (3, "sreality", "Petr Svoboda"), (4, "idnes", "Petr Svoboda"))
    bridges = [R.Bridge(1, 2, "phone", "420600111222"),
               R.Bridge(3, 4, "phone", "420600333444")]
    d = R.decide_merges(ids, bridges, ["sreality", "idnes"], suppressed_pairs={(1, 2)})
    assert d.auto_merge_groups == [[3, 4]]
    assert d.suppressed == [(1, 2)]


def test_the_pair_key_is_normalized_like_bridge_pair():
    """Bridges arrive in whichever order the contact scan emitted them; the
    suppression set is stored lo<hi, so a hi-first bridge must still be caught."""
    ids = _ids((7, "sreality", "Jan Novak"), (3, "idnes", "Jan Novak"))
    d = R.decide_merges(ids, [R.Bridge(7, 3, "phone", "420600111222")],
                        ["sreality", "idnes"], suppressed_pairs={(3, 7)})
    assert d.auto_merge_groups == [] and d.suppressed == [(3, 7)]


def test_single_rung_email_only_pair_remax_shaped():
    """remax identities carry an email and NEVER a phone (toolkit/broker_sources.py
    — deliberate, RE/MAX pages publish no broker phone), so one email plus a name
    match is the entire case for a merge. That single-rung class is what D5 gates:
    it auto-merges the moment remax is enabled, and the rail must be able to stop
    it before that switch is flipped."""
    ids = _ids((1, "sreality", "Jan Novak"), (2, "remax", "Novak Jan"))
    bridges = [R.Bridge(1, 2, "email", "jan.novak@re-max.cz")]
    assert R.decide_merges(ids, bridges, ["sreality", "remax"]).auto_merge_groups == [[1, 2]]
    d = R.decide_merges(ids, bridges, ["sreality", "remax"], suppressed_pairs={(1, 2)})
    assert d.auto_merge_groups == [] and d.review_pairs == [] and d.suppressed == [(1, 2)]


def test_single_rung_phone_only_pair_ceskereality_shaped():
    """The mirror image: ceskereality publishes a phone and no broker email."""
    ids = _ids((1, "sreality", "Jan Novak"), (2, "ceskereality", "Novak Jan"))
    bridges = [R.Bridge(1, 2, "phone", "420600111222")]
    assert R.decide_merges(ids, bridges,
                           ["sreality", "ceskereality"]).auto_merge_groups == [[1, 2]]
    d = R.decide_merges(ids, bridges, ["sreality", "ceskereality"],
                        suppressed_pairs={(1, 2)})
    assert d.auto_merge_groups == [] and d.review_pairs == [] and d.suppressed == [(1, 2)]


def test_two_emails_no_longer_merge_without_a_name_match():
    """An email-only source does NOT imply exactly one rung: broker_identity_contacts
    is unique per (identity, kind, value), so two identities can share TWO distinct
    personal emails with no name agreement at all.
    (Migration 397's header overclaims here — do not encode 'email-only == one
    rung' as an invariant.)

    Until 2026-08-18 that cleared the >=2-values bar and auto-merged. It no longer
    does: contact count alone never authorises a merge. 'J. Novak' still is not a
    conflict, so this goes to the operator rather than the bin."""
    ids = _ids((1, "sreality", "Jan Novak"), (2, "remax", "J. Novak"))
    bridges = [R.Bridge(1, 2, "email", "jan.novak@re-max.cz"),
               R.Bridge(1, 2, "email", "j.novak@re-max.cz")]
    assert R.names_match("Jan Novak", "J. Novak") is False
    d0 = R.decide_merges(ids, bridges, ["sreality", "remax"])
    assert d0.auto_merge_groups == [] and d0.review_pairs == [(1, 2)]
    d = R.decide_merges(ids, bridges, ["sreality", "remax"], suppressed_pairs={(1, 2)})
    assert d.auto_merge_groups == [] and d.suppressed == [(1, 2)]


def test_suppression_defaults_to_off_for_every_existing_caller():
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"))
    bridges = [R.Bridge(1, 2, "email", "jan@x.cz"), R.Bridge(1, 2, "phone", "420600111222")]
    d = R.decide_merges(ids, bridges, ["sreality", "idnes"])
    assert d.auto_merge_groups == [[1, 2]] and d.suppressed == []


def test_group_bridges_names_the_single_edge_that_merged_a_pair():
    """broker_merge_events.bridge_kind/bridge_value are NULL on all 7,689 live rows.
    The dominant shape — one pair, one contact — can carry its evidence forward,
    which is what the future remax validation (the D5 gate) has to audit."""
    ids = _ids((1, "sreality", "Jan Novak"), (2, "remax", "Novak Jan"))
    d = R.decide_merges(ids, [R.Bridge(1, 2, "email", "jan@re-max.cz")],
                        ["sreality", "remax"])
    assert d.group_bridges == {(1, 2): ("email", "jan@re-max.cz")}


def test_group_bridges_stays_empty_when_the_evidence_is_ambiguous():
    """Two values on one edge, or a multi-edge component: no single contact is THE
    reason, so nothing is stamped rather than picking one arbitrarily."""
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"))
    two_values = [R.Bridge(1, 2, "email", "jan@x.cz"), R.Bridge(1, 2, "phone", "420600111222")]
    assert R.decide_merges(ids, two_values, ["sreality", "idnes"]).group_bridges == {}

    chain = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"),
                 (3, "bazos", "Jan Novak"))
    edges = [R.Bridge(1, 2, "phone", "420600111222"), R.Bridge(2, 3, "phone", "420600333444")]
    d = R.decide_merges(chain, edges, ["sreality", "idnes", "bazos"])
    assert d.auto_merge_groups == [[1, 2, 3]] and d.group_bridges == {}


def test_a_suppressed_pair_never_reaches_review_through_an_oversized_component():
    """decide_merges downgrades a component over MAX_AUTO_MERGE_COMPONENT by
    expanding it pairwise — a path that never passes through the per-pair edge loop
    where the rail was enforced. An unmerge-origin suppression therefore came back as
    a BRAND-NEW review card every sweep (no prior candidate row blocks it: the
    status='proposed' guard only stops re-proposing a row that already exists), and
    was counted in queued_for_review and suppressed at the same time."""
    sources = ["sreality", "idnes"]
    ids = _ids(*((i, sources[i % 2], f"Person {i}") for i in range(1, 8)))
    bridges = []
    for a in range(1, 7):  # a 7-identity chain: one over the cap
        bridges += [R.Bridge(a, a + 1, "email", f"e{a}@x.cz"),
                    R.Bridge(a, a + 1, "phone", f"42060011122{a}")]
    plain = R.decide_merges(ids, bridges, sources)
    assert plain.auto_merge_groups == [] and len(plain.review_pairs) == 21

    # (1, 4) is cross-source and shares no bridge — it exists only as a transitive
    # pair of the downgraded component
    d = R.decide_merges(ids, bridges, sources, suppressed_pairs={(1, 4)})
    assert (1, 4) not in d.review_pairs
    assert d.suppressed == [(1, 4)]
    assert len(d.review_pairs) == 20
    assert len(d.review_pairs) + len(d.suppressed) == 21   # counted once, not twice


def test_a_suppressed_edge_is_not_re_queued_for_review_either():
    """The direct case, same guarantee: the pair the operator rejected is on record,
    so asking again — as an auto-merge or as a review card — is the same failure."""
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"))
    d = R.decide_merges(ids, [R.Bridge(1, 2, "email", "jan@x.cz")], ["sreality"],
                        suppressed_pairs={(1, 2)})
    # both_enabled is False here, so without the rail this pair lands in review
    assert d.review_pairs == [] and d.suppressed == [(1, 2)]


def test_removing_an_edge_does_not_stop_the_group_forming_around_it():
    """The responsibility split, pinned: the pure layer only removes the suppressed
    EDGE. With A-C and C-B corroborated, union-find still emits [A, B, C] — A and B
    land on one broker with no suppressed edge anywhere in the input. That is why the
    apply-time backstop exists, and why it is not redundant with this filter."""
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Jan Novak"),
               (3, "bazos", "Jan Novak"))
    bridges = [R.Bridge(1, 2, "email", "a@x.cz"), R.Bridge(1, 2, "phone", "420600111222"),
               R.Bridge(1, 3, "email", "b@x.cz"), R.Bridge(1, 3, "phone", "420600333444"),
               R.Bridge(3, 2, "email", "c@x.cz"), R.Bridge(3, 2, "phone", "420600555666")]
    d = R.decide_merges(ids, bridges, ["sreality", "idnes", "bazos"],
                        suppressed_pairs={(1, 2)})
    assert d.auto_merge_groups == [[1, 2, 3]]      # emitted DESPITE the suppression
    assert d.suppressed == [(1, 2)] and d.review_pairs == []


# --- Name is the deciding axis (2026-08-18) --------------------------------------
#
# The bar used to auto-merge on >=2 shared contacts REGARDLESS of name, and to queue
# a name-conflicting single bridge for review. Both were wrong: a colleague's mobile
# on your card is not evidence you are the same person, and "Ondřej Kadlec" vs
# "Monika Kadlecová" is not a question worth an operator's time.


def test_titles_and_diacritics_do_not_break_a_name_match():
    assert R.name_relation("Jan Novák", "Ing. Jan Novák") == "same"
    assert R.name_relation("Bc. Ondřej Kadlec", "ONDREJ KADLEC") == "same"
    assert R.name_relation("Mgr. Petra Malá, Ph.D.", "Petra Mala") == "same"


def test_a_genuine_name_conflict_is_different():
    # The live pair from the review queue: bridged by one phone, plainly two people.
    assert R.name_relation("Bc. Ondřej Kadlec", "Bc. Monika Kadlecová") == "different"


def test_partial_overlap_is_unknown_never_dismissed():
    """An initial or an extra token must reach the operator, not the bin: a dismissal
    is what stops the pair ever being proposed again."""
    assert R.name_relation("J. Novák", "Jan Novák") == "unknown"
    assert R.name_relation("Jan Novák", "Jan Novák ml.") == "unknown"
    assert R.name_relation("Jan Novák", None) == "unknown"
    assert R.name_relation("Ing.", "Jan Novák") == "unknown"  # titles-only -> no tokens


def _ident(i: int, source: str, name: str | None) -> R.Identity:
    return R.Identity(i, source, name)


def test_matching_names_plus_one_bridge_auto_merges():
    d = R.decide_merges(
        [_ident(1, "sreality", "Jan Novák"), _ident(2, "idnes", "Ing. Jan Novák")],
        [R.Bridge(1, 2, "phone", "420731404040")],
        ["sreality", "idnes"],
    )
    assert d.auto_merge_groups == [[1, 2]]
    assert d.review_pairs == [] and d.dismiss_pairs == []


def test_conflicting_names_are_dismissed_never_merged_or_queued():
    d = R.decide_merges(
        [_ident(1, "ceskereality", "Bc. Ondřej Kadlec"),
         _ident(2, "sreality", "Bc. Monika Kadlecová")],
        [R.Bridge(1, 2, "phone", "420774614199")],
        ["ceskereality", "sreality"],
    )
    assert d.dismiss_pairs == [(1, 2)]
    assert d.auto_merge_groups == [] and d.review_pairs == []


def test_two_shared_contacts_no_longer_override_a_name_conflict():
    """The regression this change exists to stop."""
    d = R.decide_merges(
        [_ident(1, "sreality", "Ondřej Kadlec"), _ident(2, "idnes", "Monika Kadlecová")],
        [R.Bridge(1, 2, "phone", "420774614199"), R.Bridge(1, 2, "email", "a@b.cz")],
        ["sreality", "idnes"],
    )
    assert d.auto_merge_groups == []
    assert d.dismiss_pairs == [(1, 2)]


def test_an_unknown_name_stays_a_review_pair_even_with_two_bridges():
    d = R.decide_merges(
        [_ident(1, "sreality", "J. Novák"), _ident(2, "idnes", "Jan Novák")],
        [R.Bridge(1, 2, "phone", "420731404040"), R.Bridge(1, 2, "email", "a@b.cz")],
        ["sreality", "idnes"],
    )
    assert d.auto_merge_groups == []
    assert d.review_pairs == [(1, 2)] and d.dismiss_pairs == []


def test_a_suppressed_pair_is_never_reclassified_as_dismissed():
    """An operator's standing NO outranks the name rules in both directions."""
    d = R.decide_merges(
        [_ident(1, "sreality", "Ondřej Kadlec"), _ident(2, "idnes", "Monika Kadlecová")],
        [R.Bridge(1, 2, "phone", "420774614199")],
        ["sreality", "idnes"],
        suppressed_pairs={(1, 2)},
    )
    assert d.suppressed == [(1, 2)]
    assert d.dismiss_pairs == [] and d.review_pairs == []
