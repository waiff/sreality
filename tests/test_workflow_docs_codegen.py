"""Hermetic tests for scripts/generate_workflow_docs.py.

Covers the codegen end to end (one doc per workflow file, no empty
names, determinism) plus the two parsing helpers with non-trivial logic
(`_describe_cron` and the leading-comment `_leading_description`). No
network, no DB — everything reads the committed YAML on disk.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _load_module() -> Any:
    if "generate_workflow_docs" in sys.modules:
        return sys.modules["generate_workflow_docs"]
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "generate_workflow_docs.py"
    spec = importlib.util.spec_from_file_location("generate_workflow_docs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_workflow_docs"] = module
    spec.loader.exec_module(module)
    return module


MOD = _load_module()
ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _docs() -> list[dict[str, Any]]:
    text = MOD.generate()
    marker = "export const WORKFLOW_DOCS: WorkflowDoc[] = "
    body = text[text.index(marker) + len(marker):].rstrip().rstrip(";")
    return json.loads(body)


def test_one_doc_per_workflow_file() -> None:
    files = sorted(p.name for p in WORKFLOWS.glob("*.yml"))
    docs = _docs()
    assert [d["filename"] for d in docs] == files
    assert len(docs) == len(files)


def test_every_doc_has_name_and_description() -> None:
    for d in _docs():
        assert d["name"], f"{d['filename']} has no name"
        assert d["description"], f"{d['filename']} has an empty description"


def test_links_point_at_the_workflow() -> None:
    for d in _docs():
        assert d["runsUrl"].endswith(f"/actions/workflows/{d['filename']}")
        assert d["sourceUrl"].endswith(f"/.github/workflows/{d['filename']}")


def test_generate_is_deterministic() -> None:
    assert MOD.generate() == MOD.generate()


def test_describe_cron() -> None:
    assert MOD._describe_cron("0 * * * *") == "Every hour (on the hour)"
    assert MOD._describe_cron("0 */2 * * *") == "Every 2 hours"
    assert MOD._describe_cron("0 22 * * *") == "Daily at 22:00 UTC"
    assert MOD._describe_cron("*/15 * * * *") == "Every 15 minutes"
    # Unrecognised shapes fall back to the raw expression.
    assert MOD._describe_cron("0 9 * * 1") == "0 9 * * 1"
    assert MOD._describe_cron("not a cron") == "not a cron"


def test_leading_description_first_paragraph_only() -> None:
    text = (
        'name: "Demo"\n'
        "\n"
        "# First line of the summary.\n"
        "# Second line of the same paragraph.\n"
        "#\n"
        "# A later paragraph that must be excluded.\n"
        "\n"
        "on:\n"
        "  workflow_dispatch:\n"
    )
    assert (
        MOD._leading_description(text)
        == "First line of the summary. Second line of the same paragraph."
    )


def test_on_boolean_key_gotcha_is_handled() -> None:
    # PyYAML parses a bare `on:` as the boolean True; the parser must
    # still find the trigger block.
    import yaml

    data = yaml.safe_load("name: x\non:\n  schedule:\n    - cron: '0 * * * *'\n")
    on = MOD._on_block(data)
    assert on.get("schedule") == [{"cron": "0 * * * *"}]


def test_portal_tag_drives_the_per_portal_schedule() -> None:
    # The `# portal: <source>` tag on a scrape workflow is what the Health
    # dashboard groups on; shared/source-agnostic jobs stay untagged (None).
    by_file = {d["filename"]: d for d in _docs()}
    assert by_file["idnes_index_walk.yml"]["portal"] == "idnes"
    assert by_file["idnes_detail_drain.yml"]["portal"] == "idnes"
    assert by_file["scrape_bazos.yml"]["portal"] == "bazos"
    assert by_file["index_walk.yml"]["portal"] == "sreality"
    # Shared platform jobs and non-portal ingests are NOT tagged.
    assert by_file["images.yml"]["portal"] is None
    assert by_file["scrape_price_stats.yml"]["portal"] is None


def test_portal_tag_is_stripped_from_the_description() -> None:
    text = (
        'name: "Demo"\n'
        "\n"
        "# portal: idnes\n"
        "# First line of the summary.\n"
        "# Second line of the same paragraph.\n"
        "\n"
        "on:\n"
        "  workflow_dispatch:\n"
    )
    assert MOD._portal(text) == "idnes"
    assert (
        MOD._leading_description(text)
        == "First line of the summary. Second line of the same paragraph."
    )


def test_untagged_workflow_has_no_portal() -> None:
    assert MOD._portal('name: "x"\n\n# Just a description.\non:\n  push:\n') is None
