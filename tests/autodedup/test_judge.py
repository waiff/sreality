"""The judge core: the forced-tool schema, both digests, image selection, messages, verdicts.

Nothing here talks to a provider or the database — the whole point of `autodedup.judge` is
that every prompt shape is decidable offline, so the paid lane never discovers a formatting
bug at $0.006 a call.
"""

from __future__ import annotations

import json

import pytest

from autodedup import judge, judge_prompts
from autodedup.dataset import Image, Listing


def make_listing(**kwargs: object) -> Listing:
    base: dict[str, object] = {"id": 1, "block": "turnov"}
    base.update(kwargs)
    return Listing(**base)  # type: ignore[arg-type]


def make_image(
    image_id: int,
    listing_id: int = 1,
    seq: int | None = 0,
    phash: int | None = None,
    pop: int | None = 1,
    tags: list[tuple[str, float | None]] | None = None,
) -> Image:
    return Image(
        listing_id=listing_id,
        image_id=image_id,
        seq=seq,
        storage_path=f"{listing_id}/{image_id}.jpg",
        phash=phash,
        pop=pop,
        clip=None,
        tags=tags or [],
    )


# --- tool schema ----------------------------------------------------------------------------


def test_tool_schema_is_serialisable_and_matches_the_contract() -> None:
    encoded = json.dumps(judge.TOOL_SCHEMA)
    assert judge.TOOL_NAME in encoded
    schema = judge.TOOL_SCHEMA["input_schema"]
    assert judge.TOOL_SCHEMA["name"] == "record_pair_verdict"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert tuple(schema["properties"]["verdict"]["enum"]) == judge.VERDICTS
    assert schema["properties"]["confidence"]["minimum"] == 0.0
    assert schema["properties"]["confidence"]["maximum"] == 1.0
    # unit_discriminator is required by the CALLER (E27), not by the schema: a model that must
    # emit it even for same_property would invent one.
    assert "unit_discriminator" not in schema["required"]
    for name in ("verdict", "confidence", "deal_or_category_conflict",
                 "key_evidence", "contradicting_evidence", "developer_project_suspected"):
        assert name in schema["required"]


def test_system_prompt_states_the_adversarial_rules() -> None:
    text = judge.SYSTEM_PROMPT
    for needle in (
        "same_building_different_unit",
        "insufficient_evidence",
        "přízemí",
        "užitná",
        "prodej",
        "pronájem",
        "komerční",
        "unit_discriminator",
        "deal_or_category_conflict",
    ):
        assert needle in text
    assert "developer" in text.lower()


# --- listing digest -------------------------------------------------------------------------


def test_digest_names_every_absent_field_instead_of_blanking_it() -> None:
    digest = judge.listing_digest(make_listing(id=77, source="bazos"))
    rendered = judge.render_digest(digest)
    assert "disposition" in digest.absent
    assert "area" in digest.absent
    assert "ownership" in digest.absent
    assert f"disposition: {judge_prompts.ABSENT_TOKEN}" in rendered
    assert f"area: {judge_prompts.ABSENT_TOKEN}" in rendered
    summary = [line for line in rendered.splitlines() if line.startswith("fields absent")]
    assert len(summary) == 1
    for name in ("disposition", "area", "price", "ownership", "energy rating"):
        assert name in summary[0]
    assert "listing id: 77" in rendered


def test_digest_says_none_when_every_tracked_field_is_present() -> None:
    attrs = {key: "ano" for key, _ in judge.DIGEST_ATTRS}
    listing = make_listing(
        disposition="2+kk", area_m2=51.0, floor=1, total_floors=4, price=4_100_000.0,
        description="Byt 2+kk.", first_seen_at="2025-02-01", last_seen_at="2025-03-01",
        attrs=attrs,
    )
    digest = judge.listing_digest(listing)
    assert digest.absent == []
    assert judge_prompts.NO_ABSENT_SUMMARY in judge.render_digest(digest)


def test_digest_renders_the_known_facts_and_the_price_path() -> None:
    listing = make_listing(
        id=5,
        source="sreality",
        category_type="prodej",
        category_main="byt",
        disposition="3+kk",
        area_m2=74.0,
        floor=3,
        total_floors=5,
        price=8_900_000.0,
        attrs={"ownership": "osobni", "condition": "velmi dobry", "area_basis": "uzitna"},
        price_history=[("2025-01-04T00:00:00+00:00", 9_200_000.0),
                       ("2025-04-01T00:00:00+00:00", 8_900_000.0)],
        first_seen_at="2025-01-04T09:00:00+00:00",
        last_seen_at="2025-06-12T09:00:00+00:00",
        is_active=False,
    )
    rendered = judge.render_digest(judge.listing_digest(listing))
    assert "deal type: prodej" in rendered
    assert "disposition: 3+kk" in rendered
    assert "area: 74 m²" in rendered
    assert "floor: 3 of 5" in rendered
    assert "8 900 000" in rendered
    assert "2025-01-04: 9 200 000" in rendered
    assert "currently active: no" in rendered
    assert "ownership: osobni" in rendered


def test_the_digest_never_states_portal_VOCABULARY_as_if_it_were_a_fact() -> None:
    """The two slots the engine dropped from the contradictions are dropped from the label
    source too, or the labels certifying that precision are read off the artefact: `price_unit`
    spells one fact `celkem` on sreality and `za nemovitost` elsewhere, `area_basis` is `unknown`
    on bazos and `usable` on the rest. 10 gold NEGATIVE votes and 47 abstentions cited them as
    the discriminator; deal type is already stated on its own line."""
    sreality = judge.render_digest(judge.listing_digest(make_listing(
        id=1, source="sreality", price=5_900_000.0,
        attrs={"price_unit": "celkem", "area_basis": "usable", "ownership": "osobni"})))
    bazos = judge.render_digest(judge.listing_digest(make_listing(
        id=2, source="bazos", price=5_900_000.0,
        attrs={"price_unit": "za nemovitost", "area_basis": "unknown", "ownership": "osobni"})))
    for rendered in (sreality, bazos):
        assert "price: 5 900 000 CZK" in rendered
        assert "ownership: osobni" in rendered
        for artefact in ("celkem", "za nemovitost", "area basis"):
            assert artefact not in rendered
    assert "area_basis" not in dict(judge.DIGEST_ATTRS)


def test_digest_keeps_at_most_eight_price_points() -> None:
    history = [(f"2025-{month:02d}-01T00:00:00+00:00", 1_000_000.0 + month) for month in range(1, 13)]
    digest = judge.listing_digest(make_listing(price_history=history))
    assert len(digest.price_history) == judge.MAX_PRICE_POINTS
    assert digest.price_history[-1][0].startswith("2025-12")


def test_digest_scrubs_contact_pii_even_from_an_unscrubbed_description() -> None:
    raw = (
        "Nabízíme byt 3+kk v centru. Kontakt: Ing. Jan Novák, tel. +420 777 654 321, "
        "e-mail makler@rkdomov.cz. Volejte realitní makléř Petr Svoboda."
    )
    digest = judge.listing_digest(make_listing(description=raw))
    rendered = judge.render_digest(digest)
    for secret in ("777 654 321", "rkdomov.cz", "Novák", "Svoboda", "makler@"):
        assert secret not in rendered
    assert "[telefon]" in rendered
    assert "[email]" in rendered
    assert "[jmeno]" in rendered
    assert "byt 3+kk" in rendered


@pytest.mark.parametrize("raw, leaked", [
    ("Jan Novák, realitní makléř. Tel: 777-123-456", ("Novák", "777")),
    ("Volejte Jana Nováková, tel 606123456 nebo pište na j.novakova@remax-czech.cz",
     ("Nováková", "606123456", "remax-czech.cz")),
    ("Prohlídky domlouvá Petra Dvořáková na čísle 720 555 111, petra@dvorakova.eu",
     ("Dvořáková", "720 555 111", "dvorakova.eu")),
    ("Kontaktní osoba: Marie Nová, tel 601 202 303", ("Nová,", "601 202 303")),
])
def test_digest_scrubs_a_name_that_precedes_its_role(raw: str, leaked: tuple[str, ...]) -> None:
    rendered = judge.render_digest(judge.listing_digest(make_listing(description=raw)))
    for secret in leaked:
        assert secret not in rendered
    assert "[jmeno]" in rendered


def test_scrub_for_prompt_leaves_ordinary_advert_prose_alone() -> None:
    text = "Byt 2+kk v Novém Městě, Rezidence Vysočany, kolaudace 2025. Cena 4 500 000 Kč."
    assert judge.scrub_for_prompt(text) == text


def test_digest_truncates_the_description_after_scrubbing() -> None:
    raw = "a" * 400 + " tel. +420 777 654 321 " + "b" * 2000
    digest = judge.listing_digest(make_listing(description=raw))
    assert digest.description is not None
    assert len(digest.description) == judge.DESCRIPTION_MAX_CHARS
    assert digest.description_truncated is True
    assert "777 654 321" not in digest.description
    assert judge_prompts.TRUNCATED_SUFFIX in judge.render_digest(digest)


def test_digest_names_the_window_as_our_crawl_and_states_the_portal_date() -> None:
    listing = make_listing(
        first_seen_at="2025-03-04T09:00:00+00:00",
        last_seen_at="2025-07-19T09:00:00+00:00",
        attrs={"published_at": "2025-02-28T06:11:00+00:00"},
    )
    rendered = judge.render_digest(judge.listing_digest(listing))
    assert "AS OBSERVED BY OUR CRAWLER" in rendered
    assert "first sighting 2025-03-04" in rendered
    assert "published by the portal: 2025-02-28" in rendered
    # The two portal-convention caveats are stated once, in the system prompt, not per line.
    assert judge_prompts.FLOOR_CONVENTION_NOTE not in rendered
    assert judge_prompts.AREA_BASIS_NOTE not in rendered
    flat = judge.SYSTEM_PROMPT.replace("\n", " ")
    assert "floor 0 on some portals and floor 1 on others" in flat
    assert "užitná plocha (usable) or as celková / podlahová plocha (total)" in flat


def test_digest_carries_no_broker_field_at_all() -> None:
    listing = make_listing(
        broker_key="c0ffee" * 10, broker_identity_id=4242, broker_firm_id=99,
        source_url="https://www.sreality.cz/detail/1234",
    )
    rendered = judge.render_digest(judge.listing_digest(listing))
    assert "c0ffee" not in rendered
    assert "4242" not in rendered
    assert "sreality.cz/detail" not in rendered


# --- evidence digest ------------------------------------------------------------------------


def test_evidence_digest_states_the_engine_numbers_as_facts() -> None:
    feats = {
        "area_rel_diff": (0.012, True),
        "dispo_equal": (1.0, True),
        "floor_diff": (1.0, True),
        "attr_contradictions": (2.0, True),
        "phash_tight_matches": (5.0, True),
        "interior_match_ratio": (0.0, True),
        "plan_match_ratio": (1.0, True),
        "catalog_ratio_max": (0.92, True),
        "same_broker_key": (1.0, True),
        "overlap_days": (61.0, True),
        "dist_norm": (0.0, False),
    }
    text = judge.evidence_digest(
        feats, ["K1", "K5"], ["IMG", "TXT"], "vysocany",
        attr_conflicts=[
            ("energy_rating", "B", "C"),
            ("ownership", "osobní", "družstevní"),
        ],
        pins=(judge.Pin("obec", 40, 2500.0), judge.Pin("address_point", 100, 10.0)),
    )
    assert "block: vysocany" in text
    assert "K1, K5" in text
    assert "(2 of 5): IMG, TXT" in text
    assert "1.20%" in text
    assert (
        "- contradicting attributes: energy_rating A=B vs B=C;"
        " ownership A=osobní vs B=družstevní" in text
    )
    assert judge_prompts.CATALOG_WARNING in text
    assert judge_prompts.DISTANCE_NOT_COMPARABLE in text
    # The coarse side is NAMED: "at least one pin" sends the model hunting for a second one.
    assert "(a municipality-grade pin on side A)" in text
    assert judge_prompts.FLOOR_CONVENTION_NOTE in text
    assert "- same broker: yes" in text
    assert "61 days" in text
    assert judge_prompts.CRAWL_WINDOW_NOTE in text
    # The corroboration COUNT is the number that most suggests a merge: last, and annotated.
    assert text.rstrip().endswith(judge_prompts.FAMILY_COUNT_NOTE)
    # Raw feature identifiers are internal names, never prompt text.
    for internal in ("dist_norm", "attr_agreements_rare", "simhash_hamming"):
        assert internal not in text


def test_evidence_digest_states_ppm2_and_the_price_ratio_in_readable_units() -> None:
    text = judge.evidence_digest({
        "ppm2_rel_diff": (0.0116, True),
        "price_last_ratio": (1.0, True),
        "rare_token_overlap": (5.0, True),
    })
    assert "price per m2 difference: 1.16%" in text
    assert "smaller price / larger price: 1.00" + judge_prompts.PRICE_RATIO_NOTE in text
    assert "5" + judge_prompts.RARE_TOKEN_CAP_NOTE in text


def test_evidence_digest_says_once_that_the_galleries_cannot_be_compared() -> None:
    text = judge.evidence_digest({
        "phash_tight_matches": (0.0, False),
        "clip_max_cos": (0.0, False),
        "catalog_ratio_max": (1.0, True),
        "n_images_min": (0.0, True),
    })
    assert judge_prompts.PHOTOS_NOT_COMPARABLE in text
    assert judge_prompts.ALL_CATALOG_WARNING in text
    assert "non-catalogue photos on the smaller side: 0" in text
    photos = text.split("PHOTOGRAPHS")[1].split("LOCATION")[0]
    assert judge_prompts.NOT_MEASURED not in photos


def test_evidence_digest_states_how_many_listings_share_the_pin() -> None:
    import math

    crowded = judge.evidence_digest({
        "pin_pop": (math.log1p(40.0), True), "same_exact_pin": (1.0, True),
        "dist_norm": (0.0, True),
    })
    assert "sharing this exact address pin: about 40" in crowded
    assert judge_prompts.PIN_POP_WARNING in crowded
    lonely = judge.evidence_digest({
        "pin_pop": (math.log1p(1.0), True), "dist_norm": (0.0, True),
    })
    assert "about 1" in lonely
    assert judge_prompts.PIN_POP_WARNING not in lonely


def test_evidence_digest_names_the_denominator_of_the_family_ratios() -> None:
    text = judge.evidence_digest({
        "phash_tight_matches": (5.0, True),
        "interior_match_ratio": (0.0, True),
        "exterior_match_ratio": (1.0, True),
        "catalog_ratio_max": (0.86, True),
    })
    assert "share of the smaller side's INTERIOR photos that matched" in text
    assert "share of the smaller side's EXTERIOR photos that matched" in text
    assert "catalogue share of the MORE catalogue-heavy of the two galleries" in text


def test_evidence_digest_prints_metres_when_the_pins_are_comparable() -> None:
    text = judge.evidence_digest(
        {"dist_norm": (0.4, True), "same_exact_pin": (0.0, True)},
        distance_m=412.0,
        pins=(judge.Pin("address_point", 100, 10.0), judge.Pin("street", 60, 50.0)),
    )
    assert "- distance: 412 m (pin radii 10 m + 50 m)" in text
    assert judge_prompts.DISTANCE_NOT_COMPARABLE not in text


def test_evidence_digest_still_renders_without_the_optional_pin_facts() -> None:
    """A re-judge from stored features alone has no listings to measure: the digest degrades to
    the normalised number rather than inventing metres."""
    text = judge.evidence_digest({"dist_norm": (0.4, True), "same_exact_pin": (0.0, True)})
    assert "- distance:" not in text
    assert "normalised by both pins' uncertainty radii: 0.40" in text
    coarse = judge.evidence_digest({"dist_norm": (0.0, False)})
    assert judge_prompts.DISTANCE_NOT_COMPARABLE in coarse
    assert f"- {judge_prompts.DISTANCE_NOT_COMPARABLE}\n" in coarse + "\n"


def test_evidence_digest_names_every_coarse_pin_when_both_sides_are_coarse() -> None:
    text = judge.evidence_digest(
        {"dist_norm": (0.0, False)},
        pins=(judge.Pin("obec", 40, 2500.0), judge.Pin(None, None, 20000.0)),
    )
    assert (
        "(a municipality-grade pin on side A, a pin of unknown grain on side B)" in text
    )


# --- image selection ------------------------------------------------------------------------


def test_select_images_puts_the_matched_pair_first() -> None:
    images_a = [
        make_image(1, 1, seq=0, phash=0xAAAA_BBBB_CCCC_DDDD),
        make_image(2, 1, seq=1, phash=0x1234_5678_9ABC_DEF0, tags=[("kitchen", 0.9)]),
    ]
    images_b = [
        make_image(11, 2, seq=0, phash=0x0F0F_0F0F_0F0F_0F0F),
        make_image(12, 2, seq=1, phash=0x1234_5678_9ABC_DEF0, tags=[("kitchen", 0.8)]),
    ]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=2
    )
    assert left[0].image_id == 2
    assert right[0].image_id == 12
    assert len(left) == 2 and len(right) == 2


def test_select_images_strips_catalogue_stock_before_matching() -> None:
    shared = 0x1234_5678_9ABC_DEF0
    images_a = [
        make_image(1, 1, seq=0, phash=shared, pop=40),
        make_image(2, 1, seq=1, phash=0xFFFF_0000_FFFF_0000, tags=[("bedroom", 0.7)]),
    ]
    images_b = [
        make_image(11, 2, seq=0, phash=shared, pop=40),
        make_image(12, 2, seq=1, phash=0x0000_FFFF_0000_FFFF, tags=[("bedroom", 0.7)]),
    ]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=4
    )
    assert [img.image_id for img in left] == [2]
    assert [img.image_id for img in right] == [12]


def test_select_images_respects_n_per_side_and_the_sequence_strategy() -> None:
    images_a = [make_image(i, 1, seq=i, tags=[("bedroom", 0.6)]) for i in range(6)]
    images_b = [make_image(10 + i, 2, seq=i, tags=[("bedroom", 0.6)]) for i in range(6)]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {},
        n_per_side=3, strategy="sequence_first",
    )
    assert [img.seq for img in left] == [0, 1, 2]
    assert [img.image_id for img in right] == [10, 11, 12]
    with pytest.raises(ValueError):
        judge.select_images(
            make_listing(id=1), images_a, make_listing(id=2), images_b, {}, strategy="nope"
        )


def test_select_images_falls_back_to_plans_and_labels_them() -> None:
    images_a = [make_image(1, 1, seq=0, tags=[("floor_plan", 0.95)])]
    images_b = [make_image(11, 2, seq=0, tags=[("exterior_facade", 0.9)])]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=4
    )
    assert [img.image_id for img in left] == [1]
    assert [img.image_id for img in right] == [11]
    assert judge_prompts.PLAN_LABEL in judge.image_caption(left[0], "A", 1)
    assert judge_prompts.EXTERIOR_LABEL in judge.image_caption(right[0], "B", 1)
    assert judge.image_captions(left, "A")[0].startswith("A-1 ")


def test_select_images_prefers_a_matched_interior_over_a_matched_facade() -> None:
    facade = 0xAAAA_BBBB_CCCC_DDDD
    interior = 0x1234_5678_9ABC_DEF0
    images_a = [
        make_image(1, 1, seq=0, phash=facade, tags=[("exterior_facade", 0.99)]),
        make_image(2, 1, seq=1, tags=[("bathroom", 0.7)]),
        make_image(3, 1, seq=3, phash=interior, tags=[("living_room", 0.8)]),
    ]
    images_b = [
        make_image(11, 2, seq=0, phash=facade, tags=[("exterior_facade", 0.99)]),
        make_image(12, 2, seq=1, tags=[("bathroom", 0.7)]),
        make_image(13, 2, seq=3, phash=interior, tags=[("living_room", 0.8)]),
    ]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=2
    )
    assert left[0].image_id == 3
    assert right[0].image_id == 13


def test_select_images_sequence_first_still_puts_the_plan_last() -> None:
    images_a = [
        make_image(1, 1, seq=0, tags=[("exterior_facade", 0.99)]),
        make_image(2, 1, seq=1, tags=[("floor_plan", 0.95)]),
        make_image(3, 1, seq=2, tags=[("bathroom", 0.7)]),
        make_image(4, 1, seq=3, tags=[("kitchen", 0.7)]),
    ]
    left, _ = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), [], {},
        n_per_side=3, strategy="sequence_first",
    )
    assert [img.image_id for img in left] == [3, 4, 1]


def test_select_images_does_not_spend_four_slots_on_one_room() -> None:
    """Four bathrooms and one of everything else: the fourth bathroom is a paid image that says
    nothing the second one did not, so the kitchen and the living room take those slots."""
    tags = [("bathroom", 0.9)] * 4 + [("kitchen", 0.9), ("living_room", 0.9)]
    images_a = [make_image(i + 1, 1, seq=i, tags=[tags[i]]) for i in range(6)]
    images_b = [make_image(11 + i, 2, seq=i, tags=[tags[i]]) for i in range(6)]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=4
    )
    for side in (left, right):
        assert len(side) == 4
        rooms = [judge.room_tag(img) for img in side]
        assert rooms.count("bathroom") == 2
        assert set(rooms) == {"bathroom", "kitchen", "living_room"}


def test_select_images_keeps_filling_when_every_frame_is_the_same_room() -> None:
    """The cap yields rather than shortens the side: a gallery of six bathrooms still sends four
    frames, because half an evidence budget is worse than a repeated room."""
    images = [make_image(i + 1, 1, seq=i, tags=[("bathroom", 0.9)]) for i in range(6)]
    left, _ = judge.select_images(
        make_listing(id=1), images, make_listing(id=2), [], {}, n_per_side=4
    )
    assert [img.image_id for img in left] == [1, 2, 3, 4]


def test_select_images_prefers_an_unmatched_interior_frame_over_the_facade() -> None:
    images_a = [
        make_image(1, 1, seq=0, tags=[("exterior_facade", 0.99)]),
        make_image(2, 1, seq=1, tags=[("living_room", 0.8)]),
    ]
    images_b = [
        make_image(11, 2, seq=0, tags=[("exterior_facade", 0.99)]),
        make_image(12, 2, seq=1, tags=[("living_room", 0.8)]),
    ]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=1
    )
    assert [img.image_id for img in left] == [2]
    assert [img.image_id for img in right] == [12]


# --- messages -------------------------------------------------------------------------------


def _digests() -> tuple[judge.ListingDigest, judge.ListingDigest]:
    return (
        judge.listing_digest(make_listing(id=3, source="sreality")),
        judge.listing_digest(make_listing(id=9, source="idnes")),
    )


def _block(index: int) -> dict[str, object]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": f"payload{index}"},
    }


def test_build_messages_is_text_only_for_the_text_tier() -> None:
    messages = judge.build_messages({"lo": 3, "hi": 9}, _digests(), "EVIDENCE", tier="text")
    assert len(messages) == 1 and messages[0]["role"] == "user"
    kinds = {block["type"] for block in messages[0]["content"]}
    assert kinds == {"text"}
    joined = "\n".join(block["text"] for block in messages[0]["content"])
    assert "PAIR 3 / 9" in joined
    assert "LISTING A" in joined and "LISTING B" in joined
    assert "EVIDENCE" in joined
    assert judge_prompts.TASK_INSTRUCTION in joined


def test_build_messages_refuses_images_on_the_text_tier() -> None:
    with pytest.raises(ValueError):
        judge.build_messages(
            {"lo": 1, "hi": 2}, _digests(), "E", [_block(1)], [], tier="text"
        )


def test_build_messages_interleaves_captions_with_image_blocks() -> None:
    messages = judge.build_messages(
        {"lo": 1, "hi": 2},
        _digests(),
        "EVIDENCE",
        [("interior photo (kitchen)", _block(1)), _block(2)],
        [("interior photo (kitchen)", _block(3))],
        tier="vision",
    )
    content = messages[0]["content"]
    sequence = [block["type"] for block in content]
    assert sequence.count("image") == 3
    captions = [block["text"] for block in content if block["type"] == "text"]
    assert any(text.startswith("A-1 interior photo") for text in captions)
    assert any(text == "A-2" for text in captions)
    assert any(text.startswith("B-1 interior photo") for text in captions)
    for index, block in enumerate(content):
        if block["type"] == "image":
            assert content[index - 1]["type"] == "text"
    assert content[-1]["text"] == judge_prompts.TASK_INSTRUCTION
    with pytest.raises(ValueError):
        judge.build_messages({"lo": 1, "hi": 2}, _digests(), "E", tier="deep")


def test_build_messages_says_so_when_a_gallery_is_all_catalogue_stock() -> None:
    messages = judge.build_messages(
        {"lo": 1, "hi": 2}, _digests(), "EVIDENCE", [], [], tier="vision"
    )
    texts = [block["text"] for block in messages[0]["content"]]
    assert judge_prompts.NO_IMAGES_NOTE.format(side="A") in texts
    assert judge_prompts.NO_IMAGES_NOTE.format(side="B") in texts
    with_images = judge.build_messages(
        {"lo": 1, "hi": 2}, _digests(), "EVIDENCE",
        [("interior photo (kitchen)", _block(1))], [], tier="vision",
    )
    later = [b["text"] for b in with_images[0]["content"] if b["type"] == "text"]
    assert judge_prompts.NO_IMAGES_NOTE.format(side="A") not in later
    assert judge_prompts.NO_IMAGES_NOTE.format(side="B") in later


def test_build_messages_tells_the_gold_tier_that_nothing_escalates() -> None:
    def coda(tier: str) -> list[str]:
        messages = judge.build_messages({"lo": 1, "hi": 2}, _digests(), "E", tier=tier)
        return [block["text"] for block in messages[0]["content"]]

    assert judge_prompts.ESCALATION_CODA in coda("text")
    assert judge_prompts.GOLD_CODA not in coda("text")
    assert judge_prompts.GOLD_CODA in coda("gold")
    assert judge_prompts.ESCALATION_CODA not in coda("gold")
    assert "escalates" not in judge.SYSTEM_PROMPT


def test_build_messages_restamps_captions_after_the_lane_shuffles_them() -> None:
    captions = judge.image_captions(
        [make_image(1, 1, seq=0, tags=[("kitchen", 0.8)]),
         make_image(2, 1, seq=1, tags=[("bedroom", 0.8)])],
        "A",
    )
    blocks = list(zip(captions, [_block(1), _block(2)]))
    blocks.reverse()
    messages = judge.build_messages(
        {"lo": 1, "hi": 2}, _digests(), "E", blocks, [], tier="vision"
    )
    stamped = [
        block["text"] for block in messages[0]["content"]
        if block["type"] == "text" and block["text"].startswith("A-")
    ]
    assert stamped[0].startswith("A-1 ") and "bedroom" in stamped[0]
    assert stamped[1].startswith("A-2 ") and "kitchen" in stamped[1]


def test_build_messages_accepts_pre_rendered_digest_strings() -> None:
    messages = judge.build_messages(
        {"lo": 1, "hi": 2}, ("SIDE-A-TEXT", "SIDE-B-TEXT"), "E", tier="text"
    )
    joined = "\n".join(block["text"] for block in messages[0]["content"])
    assert "SIDE-A-TEXT" in joined and "SIDE-B-TEXT" in joined


# --- verdicts -------------------------------------------------------------------------------


def test_parse_verdict_accepts_a_well_formed_call() -> None:
    verdict = judge.parse_verdict({
        "verdict": "same_property",
        "confidence": 0.91,
        "deal_or_category_conflict": False,
        "key_evidence": ["identical 74 m²", "same floor 3", " "],
        "contradicting_evidence": [],
        "developer_project_suspected": False,
    })
    assert verdict.verdict == "same_property"
    assert verdict.confidence == pytest.approx(0.91)
    assert verdict.key_evidence == ["identical 74 m²", "same floor 3"]
    assert verdict.unit_discriminator is None
    assert verdict.downgraded_from is None
    assert verdict.to_json()["verdict"] == "same_property"


def test_parse_verdict_accepts_a_json_string() -> None:
    verdict = judge.parse_verdict(json.dumps({
        "verdict": "same_building_different_unit",
        "confidence": 0.8,
        "deal_or_category_conflict": False,
        "unit_discriminator": "floor 2 vs floor 5",
        "key_evidence": ["only the facade matched"],
        "contradicting_evidence": [],
        "developer_project_suspected": True,
    }))
    assert verdict.unit_discriminator == "floor 2 vs floor 5"
    assert verdict.developer_project_suspected is True


@pytest.mark.parametrize("payload", [
    {"verdict": "maybe", "confidence": 0.5},
    {"verdict": "same_property", "confidence": 1.5, "key_evidence": ["x"]},
    {"verdict": "same_property", "confidence": "high", "key_evidence": ["x"]},
    "{not json",
])
def test_parse_verdict_rejects_invalid_calls(payload: object) -> None:
    """Only a verdict that cannot be READ is refused: a bad verdict name, a confidence that is
    not a probability, broken JSON. Shape is handled leniently — see the tests below."""
    with pytest.raises(judge.JudgeParseError):
        judge.parse_verdict(payload)  # type: ignore[arg-type]


def test_parse_verdict_accepts_the_qwen_shape_that_lost_ten_gold_votes() -> None:
    """The gold run binned 10 qwen3-vl votes on `key_evidence must be a list of strings, got
    str` (judge.json errors) — a billed, parsable call. A scalar is one element now."""
    verdict = judge.parse_verdict({
        "verdict": "same_property",
        "confidence": 0.9,
        "deal_or_category_conflict": False,
        "unit_discriminator": None,
        "key_evidence": "identical floor plan and the same 4. patro, unit 705",
        "contradicting_evidence": "",
        "developer_project_suspected": False,
    })
    assert verdict.verdict == "same_property"
    assert verdict.downgraded_from is None
    assert verdict.key_evidence == ["identical floor plan and the same 4. patro, unit 705"]
    assert verdict.contradicting_evidence == []


def test_parse_verdict_joins_a_list_valued_unit_discriminator() -> None:
    verdict = judge.parse_verdict({
        "verdict": "same_building_different_unit",
        "confidence": 0.8,
        "unit_discriminator": ["floor 2 vs floor 5", "unit A12 vs B31"],
        "key_evidence": ["one facade"],
        "contradicting_evidence": ["different floor"],
    })
    assert verdict.unit_discriminator == "floor 2 vs floor 5; unit A12 vs B31"
    assert verdict.downgraded_from is None


def test_parse_verdict_downgrades_same_property_with_no_evidence() -> None:
    verdict = judge.parse_verdict({
        "verdict": "same_property", "confidence": 0.99,
        "deal_or_category_conflict": False, "key_evidence": [],
    })
    assert verdict.verdict == "insufficient_evidence"
    assert verdict.downgraded_from == "same_property"


def test_parse_verdict_downgrades_a_flagged_deal_conflict() -> None:
    verdict = judge.parse_verdict({
        "verdict": "same_property", "confidence": 0.9,
        "deal_or_category_conflict": True, "key_evidence": ["same flat"],
    })
    assert verdict.verdict == "different_property"
    assert verdict.downgraded_from == "same_property"
    assert verdict.unit_discriminator == "deal or category conflict"


@pytest.mark.parametrize("payload", [
    {"verdict": "different_property", "confidence": 0.5},
    {"verdict": "different_property", "confidence": 0.5, "unit_discriminator": "  "},
])
def test_parse_verdict_downgrades_a_missing_discriminator_instead_of_discarding_the_call(
    payload: dict[str, object],
) -> None:
    verdict = judge.parse_verdict(payload)
    assert verdict.verdict == "insufficient_evidence"
    assert verdict.downgraded_from == "different_property"
    assert verdict.unit_discriminator == judge.MISSING_DISCRIMINATOR


def test_parse_verdict_keeps_insufficient_evidence_without_a_discriminator() -> None:
    verdict = judge.parse_verdict({"verdict": "insufficient_evidence", "confidence": 0.4})
    assert verdict.verdict == "insufficient_evidence"
    assert verdict.downgraded_from is None


def _vote(label: str, confidence: float = 0.9, discriminator: str = "floor 2 vs 5") -> judge.Verdict:
    return judge.Verdict(
        verdict=label,
        confidence=confidence,
        unit_discriminator=None if label == "same_property" else discriminator,
        key_evidence=["evidence"],
    )


def test_aggregate_gold_unanimous() -> None:
    gold = judge.aggregate_gold([_vote("same_property")] * 3)
    assert gold.verdict == "same_property"
    assert gold.confidence == 1.0
    assert gold.unanimous is True and gold.flagged is False
    assert gold.votes == ["same_property"] * 3


def test_aggregate_gold_two_to_one_is_flagged() -> None:
    gold = judge.aggregate_gold([
        _vote("different_property", 0.7, "area 74 vs 58 m²"),
        _vote("different_property", 0.95, "floor 2 vs floor 5"),
        _vote("same_property"),
    ])
    assert gold.verdict == "different_property"
    assert gold.confidence == pytest.approx(judge.GOLD_MAJORITY_CONFIDENCE)
    assert gold.unanimous is False and gold.flagged is True
    assert gold.unit_discriminator == "floor 2 vs floor 5"


def test_aggregate_gold_three_way_split_is_insufficient() -> None:
    gold = judge.aggregate_gold([
        _vote("same_property"),
        _vote("different_property"),
        _vote("same_building_different_unit"),
    ])
    assert gold.verdict == "insufficient_evidence"
    assert gold.confidence == 0.0
    assert gold.flagged is True
    with pytest.raises(judge.JudgeParseError):
        judge.aggregate_gold([])


def test_area_attributes_carry_their_unit() -> None:
    """`usable area: 136.0` beside `area: 136 m²` reads as two measurements in two units."""
    digest = judge.listing_digest(make_listing(
        area_m2=136.0,
        attrs={"usable_area": 136.0, "estate_area": "420", "garden_area": None},
    ))
    rendered = judge.render_digest(digest)
    assert "usable area: 136 m²" in rendered
    assert "plot area: 420 m²" in rendered
    assert "usable area: 136.0" not in rendered
    assert "garden area" in digest.absent


# --- j2: room pairing -------------------------------------------------------------------------


def test_judge_version_is_j2_so_j1_verdicts_stay_cached() -> None:
    assert judge.JUDGE_VERSION == "j2"


def test_select_images_pairs_the_same_room_in_priority_order() -> None:
    """The slot budget buys comparisons, not frames: plan against plan, kitchen against kitchen,
    bathroom against bathroom — and the facade only once the rooms have run out."""
    rooms = ["exterior_facade", "bathroom", "kitchen", "floor_plan"]
    images_a = [make_image(i + 1, 1, seq=i, tags=[(rooms[i], 0.9)]) for i in range(4)]
    images_b = [make_image(11 + i, 2, seq=i, tags=[(rooms[i], 0.9)]) for i in range(4)]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=3
    )
    assert [judge.room_key(img) for img in left] == ["floor_plan", "kitchen", "bathroom"]
    assert [judge.room_key(img) for img in right] == ["floor_plan", "kitchen", "bathroom"]
    assert judge.paired_prefix(left, right) == 3


def test_select_images_pairs_the_fine_anchors_of_one_logical_room() -> None:
    images_a = [make_image(1, 1, seq=0, tags=[("situation_plan", 0.9)])]
    images_b = [make_image(11, 2, seq=0, tags=[("cadastral_map", 0.9)])]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=4
    )
    assert judge.paired_prefix(left, right) == 1


def test_select_images_keeps_an_unpaired_room_out_while_a_pair_is_available() -> None:
    images_a = [
        make_image(1, 1, seq=0, tags=[("hallway", 0.9)]),
        make_image(2, 1, seq=1, tags=[("kitchen", 0.9)]),
    ]
    images_b = [
        make_image(11, 2, seq=0, tags=[("garden", 0.9)]),
        make_image(12, 2, seq=1, tags=[("kitchen", 0.9)]),
    ]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=2
    )
    assert [img.image_id for img in left] == [2, 1]
    assert [img.image_id for img in right] == [12, 11]
    assert judge.paired_prefix(left, right) == 1


def test_select_images_pairs_the_closest_frame_of_the_room_not_the_first() -> None:
    """Two kitchens on each side: the pair the judge has to separate is the most alike one."""
    near = 0x1234_5678_9ABC_DEF0
    images_a = [
        make_image(1, 1, seq=0, phash=0xFFFF_0000_FFFF_0000, tags=[("kitchen", 0.9)]),
        make_image(2, 1, seq=1, phash=near, tags=[("kitchen", 0.9)]),
    ]
    images_b = [
        make_image(11, 2, seq=0, phash=0x0000_FFFF_0000_FFFF, tags=[("kitchen", 0.9)]),
        make_image(12, 2, seq=1, phash=near ^ 0b111, tags=[("kitchen", 0.9)]),
    ]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=1
    )
    assert [img.image_id for img in left] == [2]
    assert [img.image_id for img in right] == [12]


def test_select_images_keeps_a_cross_room_phash_match_at_the_end() -> None:
    """A tight match tagged as two different rooms is still the strongest identity evidence in
    the gallery — but it is not a room pair and must not be presented as one."""
    shared = 0x1234_5678_9ABC_DEF0
    images_a = [
        make_image(1, 1, seq=0, phash=shared, tags=[("living_room", 0.9)]),
        make_image(2, 1, seq=1, tags=[("kitchen", 0.9)]),
    ]
    images_b = [
        make_image(11, 2, seq=0, phash=shared, tags=[("bedroom", 0.9)]),
        make_image(12, 2, seq=1, tags=[("kitchen", 0.9)]),
    ]
    left, right = judge.select_images(
        make_listing(id=1), images_a, make_listing(id=2), images_b, {}, n_per_side=2
    )
    assert [img.image_id for img in left] == [2, 1]
    assert [img.image_id for img in right] == [12, 11]
    assert judge.paired_prefix(left, right) == 1


def test_paired_prefix_stops_at_the_first_mismatch_and_at_an_untagged_frame() -> None:
    kitchen_a = make_image(1, 1, tags=[("kitchen", 0.9)])
    kitchen_b = make_image(11, 2, tags=[("kitchen", 0.9)])
    bath_b = make_image(12, 2, tags=[("bathroom", 0.9)])
    untagged_a = make_image(2, 1)
    untagged_b = make_image(13, 2)
    assert judge.paired_prefix([kitchen_a], [bath_b]) == 0
    assert judge.paired_prefix([untagged_a], [untagged_b]) == 0
    assert judge.paired_prefix([kitchen_a, untagged_a], [kitchen_b, untagged_b]) == 1
    assert judge.paired_prefix([kitchen_a], []) == 0


def test_build_messages_presents_the_pairs_adjacently_after_both_digests() -> None:
    blocks_a = [("interior photo (kitchen)", _block(1)), ("interior photo (bedroom)", _block(2))]
    blocks_b = [("interior photo (kitchen)", _block(3)), ("interior photo (hallway)", _block(4))]
    messages = judge.build_messages(
        {"lo": 1, "hi": 2}, _digests(), "EVIDENCE", blocks_a, blocks_b, "vision", paired=1
    )
    content = messages[0]["content"]
    texts = [block["text"] for block in content if block["type"] == "text"]
    assert judge_prompts.PAIRED_HEADER in "\n".join(texts)
    assert judge_prompts.UNPAIRED_NOTE in texts
    order = [
        block["text"] for block in content
        if block["type"] == "text" and block["text"][:2] in ("A-", "B-")
    ]
    assert [text.split()[0] for text in order] == ["A-1", "B-1", "A-2", "B-2"]
    # Both digests come before the first photograph: the pairs are a section, not a side.
    first_image = next(i for i, block in enumerate(content) if block["type"] == "image")
    joined = "\n".join(
        block["text"] for block in content[:first_image] if block["type"] == "text"
    )
    assert "LISTING A" in joined and "LISTING B" in joined


def test_build_messages_pairs_nothing_when_the_caller_promises_more_than_it_sent() -> None:
    messages = judge.build_messages(
        {"lo": 1, "hi": 2}, _digests(), "E",
        [("interior photo (kitchen)", _block(1))], [], "vision", paired=4,
    )
    texts = [block["text"] for block in messages[0]["content"] if block["type"] == "text"]
    assert judge_prompts.PAIRED_HEADER not in "\n".join(texts)
    assert judge_prompts.NO_IMAGES_NOTE.format(side="B") in texts


def test_paired_prompt_names_the_pairing_and_what_a_shared_facade_is_worth() -> None:
    text = judge_prompts.PAIRED_INTRO
    assert "PAIRED" in text
    for needle in ("floor plan", "kitchen", "facade", "catalogue", "BUILDING"):
        assert needle in text
    assert "unit_discriminator" in text
