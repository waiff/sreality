"""Per-source parser smoke tests.

Each source module exposes the same surface (build_messages, post_process).
The tests assert that build_messages embeds the URL, embeds the HTML,
and includes source-specific cues so the LLM knows which layout to expect.
Hermetic — no live HTTP, no LLM.

Once we have anonymized HTML fixtures (see Part F handoff), richer
field-extraction tests live in test_source_parsers/test_<source>.py
files alongside the saved fixture for that source.
"""

from __future__ import annotations

import pytest

from scraper.source_parsers import (
    bezrealitky,
    ceskereality,
    generic,
    idnes_reality,
    remax,
)


@pytest.mark.parametrize("module,source_cue", [
    (bezrealitky, "bezrealitky.cz"),
    (idnes_reality, "reality.idnes.cz"),
    (remax, "remax-czech.cz"),
    (ceskereality, "ceskereality.cz"),
    (generic, "UNKNOWN"),
])
def test_build_messages_embeds_url_and_html_and_source_cue(module, source_cue):
    url = "https://example.com/listing/abc"
    html = "<html><body>spec table</body></html>"
    msgs = module.build_messages(url, html)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert url in content
    assert html in content
    assert source_cue in content


@pytest.mark.parametrize("module", [bezrealitky, idnes_reality, remax, ceskereality, generic])
def test_post_process_passthrough_default(module):
    extraction = {"area_m2": {"value": 65, "confidence": "high"}}
    warnings = ["one"]
    out_extraction, out_warnings = module.post_process(extraction, warnings)
    assert out_extraction == extraction
    assert out_warnings == warnings


def test_bezrealitky_prompt_mentions_jsonld_and_fee_split():
    msgs = bezrealitky.build_messages("https://www.bezrealitky.cz/x", "<html/>")
    content = msgs[0]["content"]
    assert "json-ld" in content.lower() or "JSON-LD" in content
    # The fee-vs-rent ambiguity is a known parsing trap on bezrealitky.
    assert "Cena nájmu" in content
    assert "Poplatky" in content


def test_idnes_prompt_mentions_NP_floor_convention():
    msgs = idnes_reality.build_messages("https://reality.idnes.cz/x", "<html/>")
    content = msgs[0]["content"]
    assert "NP" in content


def test_remax_prompt_warns_about_nbsp_and_default_assumption():
    msgs = remax.build_messages("https://www.remax-czech.cz/x", "<html/>")
    content = msgs[0]["content"]
    assert "NBSP" in content or "\\u00A0" in content
    assert "prodej" in content.lower()


def test_ceskereality_prompt_mentions_jsonld_and_ignores_agent_address():
    msgs = ceskereality.build_messages("https://www.ceskereality.cz/x", "<html/>")
    content = msgs[0]["content"]
    assert "json-ld" in content.lower() or "JSON-LD" in content
    # The agent's office address must not be mistaken for the listing's location.
    assert "offeredby.address" in content


def test_generic_prompt_admits_unknown_layout():
    msgs = generic.build_messages("https://example.com/x", "<html/>")
    content = msgs[0]["content"]
    assert "UNKNOWN" in content or "no layout knowledge" in content
