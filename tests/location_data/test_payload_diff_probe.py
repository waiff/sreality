"""Hermetic tests for the W2a-3b live diff probe's pure differ.

The probe's fetching half (discover_refs / fetch_rounds / probe_source) needs a
socket and a portal client and is exercised by hand, on demand, against real
portals — that is the whole point of the tool. What CAN and MUST be pinned
without a network is the differ: given N already-fetched bodies, does it name
the right divergences, in the shape a `VolatileProfile` can consume? A wrong
answer here does not crash anything — it just makes the next profile-tuning
pass chase the wrong selector, silently.
"""

from __future__ import annotations

import json
from unittest import mock

from location_data.payload_norm import VolatileProfile, normalise
from scripts import location_payload_diff_probe as diff_probe
from scripts.location_payload_refetch_probe import Fetched
from scripts.location_payload_diff_probe import (
    Divergence,
    KeyResult,
    diff_bodies,
    profile_residue,
    raw_matches,
)

_HTML = "text/html; charset=utf-8"
_JSON = "application/json"


def test_diff_bodies_needs_at_least_two_bodies() -> None:
    assert diff_bodies([], content_type=_JSON) == []
    assert diff_bodies([b'{"a":1}'], content_type=_JSON) == []


def test_diff_bodies_json_reports_only_the_changed_pointer() -> None:
    a = json.dumps({"price": 100, "views": 41}).encode()
    b = json.dumps({"price": 100, "views": 99}).encode()

    out = diff_bodies([a, b], content_type=_JSON)

    assert [d.path for d in out] == ["/views"]
    assert out[0].kind == "json_pointer"
    assert out[0].samples == ("41", "99")


def test_diff_bodies_json_ignores_key_order_and_unchanged_fields() -> None:
    a = b'{"b":2,"a":1}'
    b = b'{"a":1,"b":2}'

    assert diff_bodies([a, b], content_type=_JSON) == []


def test_diff_bodies_json_collapses_array_index_only_with_two_movers() -> None:
    # Two indices move -> wildcard. This is what lets a profile express
    # "any advert_images[i].url" without enumerating every photo count seen.
    a = json.dumps({"images": [{"url": "a1"}, {"url": "b1"}]}).encode()
    b = json.dumps({"images": [{"url": "a2"}, {"url": "b2"}]}).encode()

    out = diff_bodies([a, b], content_type=_JSON)

    assert len(out) == 1
    assert out[0].path == "/images/-/url"
    assert out[0].nodes == 2


def test_diff_bodies_json_leaves_a_single_mover_as_a_literal_pointer() -> None:
    # Only ONE index differs -> stays literal (widening on one observation
    # would strip every other array element sight unseen).
    a = json.dumps({"images": [{"url": "a1"}, {"url": "same"}]}).encode()
    b = json.dumps({"images": [{"url": "a2"}, {"url": "same"}]}).encode()

    out = diff_bodies([a, b], content_type=_JSON)

    assert [d.path for d in out] == ["/images/0/url"]


def test_diff_bodies_json_unparseable_body_reports_one_divergence_not_a_crash() -> None:
    out = diff_bodies([b'{"a":1}', b"not json"], content_type=_JSON)

    assert len(out) == 1
    assert out[0].kind == "json_pointer"
    assert out[0].detail == "unparseable"


def test_diff_bodies_html_attribute_divergence_is_named_by_path_and_attribute() -> None:
    a = b'<html><body><input name="tshee" value="111"></body></html>'
    b = b'<html><body><input name="tshee" value="222"></body></html>'

    out = diff_bodies([a, b], content_type=_HTML)

    assert len(out) == 1
    assert out[0].kind == "attribute"
    assert out[0].detail == "value"
    assert out[0].samples == ("111", "222")


def test_diff_bodies_html_text_divergence_and_stable_text_is_silent() -> None:
    a = b'<html><body><h1>Byt 2+1</h1><span class="views">40</span></body></html>'
    b = b'<html><body><h1>Byt 2+1</h1><span class="views">41</span></body></html>'

    out = diff_bodies([a, b], content_type=_HTML)

    assert len(out) == 1
    assert out[0].kind == "text"
    assert "span.views" in out[0].path


def test_diff_bodies_html_element_present_in_only_one_fetch() -> None:
    a = b'<html><body><h1>Byt</h1></body></html>'
    b = b'<html><body><h1>Byt</h1><div class="grid-similar-offers">x</div></body></html>'

    out = diff_bodies([a, b], content_type=_HTML)

    # A brand-new node reports from every angle it carries (its own presence,
    # its class attribute, its text) — all of them anchored to its own path,
    # never fanned out onto unrelated siblings.
    assert out
    assert {d.path for d in out} == {"html > body > div.grid-similar-offers"}
    assert {d.kind for d in out} >= {"element"}


def test_diff_bodies_html_identity_is_class_id_path_not_sibling_index() -> None:
    """Inserting one sibling must not renumber-and-flag every node after it —
    only the inserted node itself should show up as a divergence."""
    a = (
        b'<html><body><ul>'
        b'<li class="x">one</li><li class="y">two</li>'
        b'</ul></body></html>'
    )
    b = (
        b'<html><body><ul>'
        b'<li class="new">inserted</li>'
        b'<li class="x">one</li><li class="y">two</li>'
        b'</ul></body></html>'
    )

    out = diff_bodies([a, b], content_type=_HTML)

    # Every reported divergence must anchor to the INSERTED node's own path —
    # if the differ were sibling-index-keyed instead of class/id-path-keyed,
    # `li.x` and `li.y` would ALSO show up here as spuriously "renumbered".
    assert out
    assert {d.path for d in out} == {"html > body > ul > li.new"}


def test_diff_bodies_html_identical_bodies_report_nothing() -> None:
    page = b'<html><body><h1>Byt</h1><p>same</p></body></html>'

    assert diff_bodies([page, page, page], content_type=_HTML) == []


def test_raw_matches() -> None:
    same = KeyResult(key="1", bodies=[b'{"a":1}', b'{"a":1}'], content_type=_JSON)
    different = KeyResult(key="2", bodies=[b'{"a":1}', b'{"a":2}'], content_type=_JSON)

    assert raw_matches(same)
    assert not raw_matches(different)


def test_profile_residue_true_and_empty_when_the_profile_covers_everything() -> None:
    result = KeyResult(
        key="1",
        bodies=[
            json.dumps({"price": 1, "views": 40}).encode(),
            json.dumps({"price": 1, "views": 41}).encode(),
        ],
        content_type=_JSON,
    )
    profile = VolatileProfile(json_pointers=("/views",))

    covered, residue = profile_residue(result, profile)

    assert covered is True
    assert residue == []


def test_profile_residue_names_exactly_what_survives_the_profile() -> None:
    result = KeyResult(
        key="1",
        bodies=[
            json.dumps({"price": 1, "views": 40, "token": "a"}).encode(),
            json.dumps({"price": 1, "views": 41, "token": "b"}).encode(),
        ],
        content_type=_JSON,
    )
    # Covers views but not token: residue should name only /token.
    profile = VolatileProfile(json_pointers=("/views",))

    covered, residue = profile_residue(result, profile)

    assert covered is False
    assert [d.path for d in residue] == ["/token"]


def test_profile_residue_is_computed_over_normalised_not_raw_bodies() -> None:
    """profile_residue must report what the PROFILE leaves moving, not what
    raw diffing would show — a residue check that regressed to raw bodies
    would falsely fail every profile that works."""
    result = KeyResult(
        key="1",
        bodies=[
            json.dumps({"price": 1, "noise": "x"}, sort_keys=False).encode(),
            json.dumps({"noise": "y", "price": 1}, sort_keys=False).encode(),
        ],
        content_type=_JSON,
    )
    profile = VolatileProfile(json_pointers=("/noise",))

    covered, residue = profile_residue(result, profile)

    assert covered is True
    assert residue == []


def test_divergence_render_truncates_the_path_to_its_tail() -> None:
    deep = Divergence(
        kind="text",
        path=" > ".join(["div"] * 3 + ["span.views"]),
        samples=("40", "41"),
    )

    rendered = deep.render(tail=2)

    assert rendered.startswith("[text] ... > div > span.views")
    assert "1: 40" in rendered
    assert "2: 41" in rendered


def test_normalise_agrees_with_profile_residue_on_a_measured_profile() -> None:
    """Cross-check against the shipped module: a profile that DEFAULT_VOLATILE_
    PROFILES ships for idnes should leave profile_residue with nothing to say
    on the exact snippet the profile was written to cover."""
    from location_data.payload_norm import DEFAULT_VOLATILE_PROFILES

    a = b'<html><body><h1>Byt</h1><input name="tshee" value="1"></body></html>'
    b = b'<html><body><h1>Byt</h1><input name="tshee" value="2"></body></html>'
    result = KeyResult(key="1", bodies=[a, b], content_type=_HTML)

    covered, residue = profile_residue(result, DEFAULT_VOLATILE_PROFILES["idnes"])

    assert covered is True, residue
    # cross-check: normalise() directly agrees with the residue check
    normed = [normalise(x, content_type=_HTML, volatile=DEFAULT_VOLATILE_PROFILES["idnes"])
              for x in (a, b)]
    assert normed[0].norm_sha256 == normed[1].norm_sha256


# --- the session axis (W2a-3c) ---


def test_session_factory_hands_out_a_new_client_per_round_by_default() -> None:
    """The finding this flag exists for: remax answers three fetches eight seconds
    apart with byte-identical bodies and still measured 100% in production, because
    its CSRF material is minted per HTTP SESSION. A probe that reuses one session
    measures a strictly weaker thing than the instrument it is explaining."""
    built: list[object] = []

    def fake_build(source: str, limiter: object) -> object:
        client = object()
        built.append((client, limiter))
        return client

    with mock.patch.object(diff_probe, "build_client", fake_build):
        factory = diff_probe.session_factory("remax", 1.0, fresh_per_round=True)
        clients = [factory(i) for i in range(3)]

    assert len(set(map(id, clients))) == 3
    # ...but ONE limiter across all of them: it carries the pacing and the adaptive
    # 429/403 penalty, and a fresh one per round would hand a portal that just
    # throttled us a clean slate three times over.
    assert len({id(limiter) for _client, limiter in built}) == 1


def test_session_factory_reuses_one_client_when_the_axis_is_switched_off() -> None:
    with mock.patch.object(diff_probe, "build_client", lambda s, limiter: object()):
        factory = diff_probe.session_factory("remax", 1.0, fresh_per_round=False)

        assert factory(0) is factory(1) is factory(2)


def test_fetch_rounds_asks_for_a_client_once_per_round_not_once_per_key() -> None:
    """A new session per KEY would be neither what production does nor polite."""
    rounds: list[int] = []
    keys = [
        diff_probe.SampleKey(source="remax", native_id="1", detail_ref="/a"),
        diff_probe.SampleKey(source="remax", native_id="2", detail_ref="/b"),
    ]

    def client_for_round(index: int) -> str:
        rounds.append(index)
        return f"client{index}"

    used: list[str] = []

    def fake_fetch(source: str, client: str, key: object) -> object:
        used.append(client)
        return Fetched(kind=diff_probe.KIND_OK, body=b"<html/>", content_type=_HTML)

    pacer = diff_probe.Pacer(min_interval_s=0.0, monotonic=lambda: 0.0,
                             sleep=lambda _s: None)
    with mock.patch.object(diff_probe, "fetch_body", fake_fetch):
        results = diff_probe.fetch_rounds(
            "remax", client_for_round, keys, fetches=3, pacer=pacer,
        )

    assert rounds == [0, 1, 2]
    assert used == ["client0"] * 2 + ["client1"] * 2 + ["client2"] * 2
    assert all(len(r.bodies) == 3 for r in results.values())
