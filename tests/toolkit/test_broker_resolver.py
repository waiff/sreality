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


def test_single_bridge_name_mismatch_queues_not_merges():
    # A recycled/ported phone shared by two DIFFERENT people: must NOT auto-merge.
    ids = _ids((1, "sreality", "Jan Novak"), (2, "idnes", "Petr Svoboda"))
    d = R.decide_merges(ids, [R.Bridge(1, 2, "phone", "420600111222")], ["sreality", "idnes"])
    assert d.auto_merge_groups == []
    assert d.review_pairs == [(1, 2)]


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
