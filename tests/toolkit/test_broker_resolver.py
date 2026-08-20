"""Unit tests for the pure, portal-agnostic broker identity-resolution rules.

The rule under test (2026-08-20): merge two identities when their NAMES MATCH and
either (A) they share a discriminating contact — one whose carriers corpus-wide all
carry that single name — or (B) they share a firm and that name appears at only one
firm corpus-wide. Portals are not a factor in any direction.
"""

from __future__ import annotations

from toolkit import broker_resolver as R


def _ident(i: int, source: str = "sreality", name: str | None = None, *,
           firm: int | None = None, mergeable: bool = True) -> R.Identity:
    return R.Identity(i, source, name, firm, mergeable)


def _contacts(kind: str, value: str, *identity_ids: int) -> list[R.Contact]:
    return [R.Contact(i, kind, value) for i in identity_ids]


# --- normalisation helpers (unchanged by the rewrite) ---------------------------


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


# --- the name gate ---------------------------------------------------------------


def test_name_key_is_order_and_diacritics_insensitive():
    assert R.name_key("Jan Novák") == R.name_key("novak jan") == "jan novak"
    assert R.names_match("Jan Novák", "Novák Jan") is True
    assert R.names_match("Jan Novák", "Petr Svoboda") is False
    assert R.names_match(None, None) is False  # no name never matches, not even itself


def test_academic_titles_are_stripped_from_the_key():
    """A degree is not identity: the same human is 'Ondřej Kadlec' on one portal and
    'Bc. Ondřej Kadlec' on another, and a strict-equality gate that keeps the title
    is a merge silently not made."""
    assert R.name_key("Bc. Ondřej Kadlec") == R.name_key("Ondřej Kadlec")
    assert R.name_key("Ing. arch. Jiří Harák") == R.name_key("Harák Jiří")
    assert R.name_key("Mgr. Petra Malá, Ph.D.") == R.name_key("Petra Mala")
    assert R.names_match("Bc. Ondřej Kadlec", "KADLEC ONDREJ") is True


def test_a_title_only_string_has_no_identity_content():
    assert R.name_key("Ing.") is None
    assert R.name_key("Ing. Mgr.") is None
    assert R.names_match("Ing.", "Ing.") is False


def test_name_relation_still_grades_the_three_way_comparison():
    """Kept for the auto-dismissal path (#1096); 'different' stays hard to reach."""
    assert R.name_relation("Jan Novák", "Ing. Jan Novák") == "same"
    assert R.name_relation("Bc. Ondřej Kadlec", "Bc. Monika Kadlecová") == "different"
    assert R.name_relation("J. Novák", "Jan Novák") == "unknown"
    assert R.name_relation("Jan Novák", None) == "unknown"
    assert R.name_relation("Ing.", "Jan Novák") == "unknown"


# --- path A: a discriminating contact --------------------------------------------


def test_mizjuk_six_same_source_duplicates_merge_on_one_personal_email():
    """The case the old engine could not touch, for two independent reasons: the six
    records are all sreality (never-merge-within-a-source) and duplication itself
    made his own e-mail look shared (frequency 6, not 1). Under the discrimination
    test the duplication REINFORCES the evidence — every carrier is the same name."""
    ids = [_ident(i, "sreality", "Alexandr Mizjuk") for i in range(1, 7)]
    contacts = _contacts("email", "a.mizjuk@byty.cz", 1, 2, 3, 4, 5, 6)
    d = R.decide_merges(ids, contacts)
    assert d.auto_merge_groups == [[1, 2, 3, 4, 5, 6]]
    assert d.group_reasons == {(1, 2, 3, 4, 5, 6): R.REASON_CONTACT_NAME}
    assert d.review_pairs == []


def test_kaderkova_two_portals_one_phone_no_allowlist_left():
    """Cross-portal is now just another pair — no source ever has to be enabled."""
    ids = [_ident(1, "sreality", "Jana Kaderková"),
           _ident(2, "ceskereality", "Kaderková Jana")]
    d = R.decide_merges(ids, _contacts("phone", "420602111222", 1, 2))
    assert d.auto_merge_groups == [[1, 2]]
    assert d.group_reasons[(1, 2)] == R.REASON_CONTACT_NAME


def test_kadlec_and_kadlecova_share_a_phone_and_nothing_happens():
    """Two names on one number: the number discriminates nothing, so there is no
    edge — and no review card either, because a cross-name pair is not a question."""
    ids = [_ident(1, "ceskereality", "Bc. Ondřej Kadlec"),
           _ident(2, "sreality", "Bc. Monika Kadlecová")]
    d = R.decide_merges(ids, _contacts("phone", "420774614199", 1, 2))
    assert d.auto_merge_groups == [] and d.review_pairs == [] and d.dismiss_pairs == []


def test_a_role_inbox_carried_by_many_names_discriminates_nothing():
    """353 names behind info@… — the shape the frequency guard existed for, still
    excluded, now because it carries many NAMES rather than many rows."""
    ids = [_ident(i, "sreality", f"Osoba {i}", firm=10) for i in range(1, 354)]
    d = R.decide_merges(ids, _contacts("email", "info@velkark.cz", *range(1, 354)))
    assert d.auto_merge_groups == []
    assert d.review_pairs == []  # different names -> nothing to ask


def test_a_common_name_still_merges_on_a_discriminating_contact():
    """Path A never consults firm spread: the shared contact IS the evidence."""
    ids = [_ident(i, "sreality", "Jan Novák", firm=10 * i) for i in range(1, 6)]
    d = R.decide_merges(ids, _contacts("phone", "420603111222", 1, 2))
    assert d.auto_merge_groups == [[1, 2]]


def test_an_unnamed_carrier_does_not_break_discrimination():
    ids = [_ident(1, "sreality", "Eva Dvořáková"),
           _ident(2, "idnes", "Dvořáková Eva"),
           _ident(3, "bazos", None)]
    d = R.decide_merges(ids, _contacts("email", "eva@dvorakova.cz", 1, 2, 3))
    assert d.auto_merge_groups == [[1, 2]]   # the unnamed carrier abstains...
    assert 3 not in {i for g in d.auto_merge_groups for i in g}  # ...and never merges


def test_a_differently_named_carrier_kills_the_contact():
    ids = [_ident(1, "sreality", "Eva Dvořáková"),
           _ident(2, "idnes", "Dvořáková Eva"),
           _ident(3, "bazos", "Petr Svoboda")]
    d = R.decide_merges(ids, _contacts("email", "eva@dvorakova.cz", 1, 2, 3))
    assert d.auto_merge_groups == []


def test_a_merged_away_identity_is_evidence_but_never_a_member():
    """mergeable=False identities count for both corpus-wide maps (their name is a
    fact about who a contact belongs to) and take no edges of their own."""
    poisoned = R.decide_merges(
        [_ident(1, "sreality", "Eva Dvořáková"), _ident(2, "idnes", "Dvořáková Eva"),
         _ident(3, "bazos", "Petr Svoboda", mergeable=False)],
        _contacts("email", "eva@dvorakova.cz", 1, 2, 3))
    assert poisoned.auto_merge_groups == []

    same_name = R.decide_merges(
        [_ident(1, "sreality", "Eva Dvořáková"), _ident(2, "idnes", "Dvořáková Eva"),
         _ident(3, "bazos", "Eva Dvořáková", mergeable=False)],
        _contacts("email", "eva@dvorakova.cz", 1, 2, 3))
    assert same_name.auto_merge_groups == [[1, 2]]


# --- path B: a name that exists at exactly one firm -------------------------------


def test_harak_six_records_at_one_firm_merge_without_a_personal_contact():
    """The role-inbox-only shape: every record is reachable only at the firm inbox
    and the switchboard (both carried by many names, so both discriminate nothing),
    but the name exists at no other firm — so the firm IS the evidence."""
    ids = [_ident(i, "sreality", "Ing. arch. Jiří Harák", firm=10) for i in range(1, 7)]
    ids += [_ident(20 + i, "sreality", f"Kolega {i}", firm=10) for i in range(1, 4)]
    everyone = [i.id for i in ids]
    contacts = (_contacts("email", "info@harakreality.cz", *everyone)
                + _contacts("phone", "420800100200", *everyone))
    d = R.decide_merges(ids, contacts)
    assert d.auto_merge_groups == [[1, 2, 3, 4, 5, 6]]
    assert d.group_reasons == {(1, 2, 3, 4, 5, 6): R.REASON_NAME_FIRM}
    assert d.group_bridges == {}          # no contact can be named as the reason


def test_a_name_at_two_firms_never_merges_on_the_firm_alone():
    """'Jan Novák' twice at one firm plus once elsewhere: the name proves nothing,
    and the same-name-same-firm pair is the name_firm candidate tab's job."""
    ids = [_ident(1, "sreality", "Jan Novák", firm=10),
           _ident(2, "idnes", "Novák Jan", firm=10),
           _ident(3, "bazos", "Jan Novák", firm=20)]
    d = R.decide_merges(ids, [])
    assert d.auto_merge_groups == []
    assert [1, 2] not in d.auto_merge_groups
    assert d.review_pairs == []


def test_a_firmless_identity_takes_no_firm_edge():
    ids = [_ident(1, "sreality", "Jiří Harák", firm=10),
           _ident(2, "idnes", "Harák Jiří", firm=None)]
    assert R.decide_merges(ids, []).auto_merge_groups == []


def test_a_mixed_component_records_both_evidence_paths():
    ids = [_ident(1, "sreality", "Jiří Harák", firm=10),
           _ident(2, "idnes", "Harák Jiří", firm=10),
           _ident(3, "bazos", "Jiří Harák", firm=10)]
    # 1+2+3 chain on the firm; 1 and 3 additionally share a discriminating mobile.
    d = R.decide_merges(ids, _contacts("phone", "420605111222", 1, 3))
    assert d.auto_merge_groups == [[1, 2, 3]]
    assert d.group_reasons[(1, 2, 3)] == f"{R.REASON_CONTACT_NAME}+{R.REASON_NAME_FIRM}"


# --- what the operator is asked ---------------------------------------------------


def test_a_shared_non_discriminating_contact_across_firms_is_a_review_pair():
    ids = [_ident(1, "sreality", "Jan Novák", firm=10),
           _ident(2, "idnes", "Novák Jan", firm=20),
           _ident(3, "bazos", "Petr Svoboda", firm=30)]
    d = R.decide_merges(ids, _contacts("email", "info@sdilena.cz", 1, 2, 3))
    assert d.auto_merge_groups == []
    assert d.review_pairs == [(1, 2)]     # same name, two firms, ambiguous contact
    assert (1, 3) not in d.review_pairs   # cross-name pairs are never asked about


def test_the_same_pair_inside_one_firm_is_left_to_the_name_firm_tab():
    """Two cards for one question is worse than none: the name_firm generator
    already proposes same-name-same-firm groups."""
    ids = [_ident(1, "sreality", "Eva Malá", firm=40),
           _ident(2, "idnes", "Malá Eva", firm=40),
           _ident(3, "bazos", "Eva Malá", firm=50),      # name at 2 firms -> no B-merge
           _ident(4, "remax", "Petr Svoboda", firm=40)]
    d = R.decide_merges(ids, _contacts("email", "info@firma40.cz", 1, 2, 4))
    assert d.auto_merge_groups == []
    assert d.review_pairs == []


def test_an_oversized_component_is_downgraded_whole():
    """21 records of one generic label chained by one switchboard: discriminating by
    the letter of the test, not one human. Nothing merges; every pair is queued."""
    ids = [_ident(i, "sreality", "Zákaznická linka") for i in range(1, 22)]
    d = R.decide_merges(ids, _contacts("phone", "420800123456", *range(1, 22)))
    assert d.auto_merge_groups == []
    assert len(d.review_pairs) == 21 * 20 // 2
    assert (1, 21) in d.review_pairs


def test_a_pool_too_large_to_expand_is_queued_as_its_real_edges():
    """The OOM rail. A downgraded component is expanded pairwise, which is n(n-1)/2
    — fine for the 464-record pool the cap was sized against, ruinous an order of
    magnitude up (a 5,000-record label would be 12.5M pairs, more rows than the
    whole corpus). Past MAX_REVIEW_EXPANSION the component is queued as the edges
    that actually formed it: still genuine same-name shared-contact pairs, just
    n-1 of them."""
    n = R.MAX_REVIEW_EXPANSION + 1
    ids = [_ident(i, "sreality", "Zákaznická linka") for i in range(1, n + 1)]
    d = R.decide_merges(ids, _contacts("phone", "420800123456", *range(1, n + 1)))
    assert d.auto_merge_groups == []
    assert len(d.review_pairs) == n - 1          # the chain, not the clique
    assert all(a == 1 for a, _ in d.review_pairs)
    # ...and one identity below the ceiling still expands in full
    smaller = [_ident(i, "sreality", "Zákaznická linka")
               for i in range(1, R.MAX_REVIEW_EXPANSION + 1)]
    full = R.decide_merges(
        smaller, _contacts("phone", "420800123456",
                           *range(1, R.MAX_REVIEW_EXPANSION + 1)))
    expected = R.MAX_REVIEW_EXPANSION * (R.MAX_REVIEW_EXPANSION - 1) // 2
    assert len(full.review_pairs) == expected


def test_the_cap_is_twenty_and_a_fan_that_size_still_merges():
    assert R.MAX_AUTO_MERGE_COMPONENT == 20
    ids = [_ident(i, "sreality", "Alexandr Mizjuk") for i in range(1, 21)]
    d = R.decide_merges(ids, _contacts("email", "a.mizjuk@byty.cz", *range(1, 21)))
    assert d.auto_merge_groups == [list(range(1, 21))]


# --- the suppression rail (migration 401) ----------------------------------------


def test_a_suppressed_pair_reaches_neither_auto_merge_nor_review():
    ids = [_ident(1, "sreality", "Jan Novák"), _ident(2, "idnes", "Novák Jan")]
    contacts = _contacts("phone", "420600111222", 1, 2)
    assert R.decide_merges(ids, contacts).auto_merge_groups == [[1, 2]]   # the control
    d = R.decide_merges(ids, contacts, suppressed_pairs={(1, 2)})
    assert d.auto_merge_groups == [] and d.review_pairs == []
    assert d.suppressed == [(1, 2)]


def test_a_suppression_downgrades_the_whole_component_it_sits_inside():
    """The transitive case: 1-2 and 2-3 are the only edges, so dropping the
    suppressed (1, 3) edge removes nothing — union-find would still land all three
    on one broker. The component becomes review pairs instead, minus the pair the
    operator already answered."""
    ids = [_ident(i, "sreality", "Jan Novák") for i in (1, 2, 3)]
    contacts = (_contacts("email", "jan@a.cz", 1, 2)
                + _contacts("email", "jan@b.cz", 2, 3))
    assert R.decide_merges(ids, contacts).auto_merge_groups == [[1, 2, 3]]
    d = R.decide_merges(ids, contacts, suppressed_pairs={(1, 3)})
    assert d.auto_merge_groups == []
    assert d.review_pairs == [(1, 2), (2, 3)]
    assert d.suppressed == [(1, 3)]
    assert len(d.review_pairs) + len(d.suppressed) == 3  # counted once, not twice


def test_suppressing_one_pair_leaves_the_others_alone():
    ids = [_ident(1, "sreality", "Jan Novák"), _ident(2, "idnes", "Novák Jan"),
           _ident(3, "sreality", "Petr Svoboda"), _ident(4, "idnes", "Svoboda Petr")]
    contacts = (_contacts("phone", "420600111222", 1, 2)
                + _contacts("phone", "420600333444", 3, 4))
    d = R.decide_merges(ids, contacts, suppressed_pairs={(1, 2)})
    assert d.auto_merge_groups == [[3, 4]]
    assert d.suppressed == [(1, 2)]


def test_a_suppressed_firm_edge_is_blocked_too():
    """The rail is evidence-blind: it rejects the PAIR, whichever path proposed it."""
    ids = [_ident(1, "sreality", "Jiří Harák", firm=10),
           _ident(2, "idnes", "Harák Jiří", firm=10)]
    assert R.decide_merges(ids, []).auto_merge_groups == [[1, 2]]
    d = R.decide_merges(ids, [], suppressed_pairs={(1, 2)})
    assert d.auto_merge_groups == [] and d.suppressed == [(1, 2)]


def test_suppression_defaults_to_off_for_every_caller():
    ids = [_ident(1, "sreality", "Jan Novák"), _ident(2, "idnes", "Novák Jan")]
    d = R.decide_merges(ids, _contacts("email", "jan@x.cz", 1, 2))
    assert d.auto_merge_groups == [[1, 2]] and d.suppressed == []


# --- what a merge records ---------------------------------------------------------


def test_group_bridges_names_the_single_contact_that_merged_a_pair():
    """broker_merge_events.bridge_kind/bridge_value: the dominant shape (one pair,
    one contact) carries its evidence forward."""
    ids = [_ident(1, "sreality", "Jan Novák"), _ident(2, "remax", "Novák Jan")]
    d = R.decide_merges(ids, _contacts("email", "jan@re-max.cz", 1, 2))
    assert d.group_bridges == {(1, 2): ("email", "jan@re-max.cz")}


def test_group_bridges_stays_empty_when_the_evidence_is_ambiguous():
    ids = [_ident(1, "sreality", "Jan Novák"), _ident(2, "idnes", "Novák Jan")]
    two_values = (_contacts("email", "jan@x.cz", 1, 2)
                  + _contacts("phone", "420600111222", 1, 2))
    assert R.decide_merges(ids, two_values).group_bridges == {}

    chain = [_ident(i, "sreality", "Jan Novák") for i in (1, 2, 3)]
    edges = (_contacts("phone", "420600111222", 1, 2)
             + _contacts("phone", "420600333444", 2, 3))
    d = R.decide_merges(chain, edges)
    assert d.auto_merge_groups == [[1, 2, 3]] and d.group_bridges == {}


def test_nothing_at_all_is_a_no_op():
    d = R.decide_merges([], [])
    assert d.auto_merge_groups == [] and d.review_pairs == [] and d.suppressed == []
    d = R.decide_merges([_ident(1, "sreality", "Jan Novák")], [])
    assert d.auto_merge_groups == []


def test_contacts_for_unknown_identities_are_ignored():
    """The two reads are separate statements; a contact row for an identity the
    identity read did not return must not crash the sweep."""
    d = R.decide_merges([_ident(1, "sreality", "Jan Novák")],
                        _contacts("email", "jan@x.cz", 1, 999))
    assert d.auto_merge_groups == []
