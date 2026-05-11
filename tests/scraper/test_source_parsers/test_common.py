"""Tests for the shared bits of scraper.source_parsers (tool schema + helpers)."""

from __future__ import annotations

from scraper.source_parsers import common


def test_record_listing_tool_has_all_required_fields():
    schema = common.RECORD_LISTING_TOOL["input_schema"]
    expected = {
        "area_m2", "disposition", "price_czk", "price_unit",
        "locality", "district", "category_main", "category_type",
        "floor", "total_floors", "has_balcony", "has_lift",
        "has_parking", "building_type", "condition",
        "energy_rating", "description", "warnings",
    }
    assert set(schema["properties"].keys()) == expected
    assert set(schema["required"]) == expected


def test_record_listing_value_confidence_envelope():
    """Each non-warnings field is {value, confidence: enum}."""
    props = common.RECORD_LISTING_TOOL["input_schema"]["properties"]
    for name, schema in props.items():
        if name == "warnings":
            continue
        assert schema["type"] == "object"
        assert set(schema["properties"].keys()) == {"value", "confidence"}
        assert schema["properties"]["confidence"]["enum"] == ["high", "medium", "low"]
        assert schema["required"] == ["value", "confidence"]


def test_truncate_html_below_cap_unchanged():
    html, was = common.truncate_html("<html/>", cap=100)
    assert html == "<html/>"
    assert was is False


def test_truncate_html_above_cap_truncated():
    html, was = common.truncate_html("x" * 200, cap=50)
    assert len(html) == 50
    assert was is True


def test_render_messages_appends_truncation_note_when_truncated():
    msgs = common.render_messages("PROMPT\n", "x" * (common.HTML_CHAR_CAP + 10))
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert content.startswith("PROMPT\n")
    assert "truncated" in content


def test_render_messages_no_note_when_within_cap():
    msgs = common.render_messages("PROMPT\n", "small")
    assert "truncated" not in msgs[0]["content"]
