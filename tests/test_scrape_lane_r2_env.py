"""Every lane that fetches portal pages must carry the R2 credentials.

The W2a payload archive keeps bodies in R2 and only the metadata row in Postgres
(`location_data.payload_budget` carries the arithmetic that rules out the inline
alternative). So `location_data.payloads.open_store` returning None — which is what
four missing env vars look like — makes `append_payload` REFUSE the write.

That refusal is deliberately non-fatal: `scraper.db.append_payload_if_enabled` warns
and returns, because the archive must never be able to stop a scrape. The cost of that
choice is this failure mode, and it is the reason this test exists:

    a lane missing R2 archives NOTHING while its scrape stays green.

`portal_raw_pages` keeps filling, CI stays green, the dashboards stay green, and the
archive the whole location programme reads from is silently empty. It cost the fleet
once already — `payload_dual_write` was declared ready to enable while nine of the ten
page-fetching lanes had no bucket to write to.

The set is derived from `contracts/portals/`, not hardcoded, so a tenth portal is
covered on the day its contract lands rather than the day someone remembers this file.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

R2_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")

# `scraper.main` is the sreality Phase-2 fallback lane; it reaches the same
# `upsert_portal_raw_page` chokepoint as the split lanes, so it is in scope too.
EXTRA_ENTRYPOINTS = frozenset({"scraper.main"})

# `scraper.price_stats_main` scrapes aggregate price statistics, never a listing page,
# and reaches no archive call site — asserted below rather than trusted.
NON_ARCHIVING = frozenset({"scraper.price_stats_main"})


def _portal_entrypoints() -> frozenset[str]:
    sources = sorted(p.stem for p in (ROOT / "contracts" / "portals").glob("*.yaml"))
    assert sources, "no portal contracts found — the derivation is broken, not the fleet"
    found = {
        f"scraper.{s}_main" for s in sources if (ROOT / "scraper" / f"{s}_main.py").exists()
    }
    return frozenset(found) | EXTRA_ENTRYPOINTS


def _steps_running(module: str) -> list[tuple[pathlib.Path, dict, dict]]:
    """(path, workflow, step) for every step whose run line invokes `python -m <module>`."""
    pattern = re.compile(rf"python -m {re.escape(module)}(?!\w)")
    out: list[tuple[pathlib.Path, dict, dict]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if pattern.search(step.get("run") or ""):
                    out.append((path, doc, {"job": job, "step": step}))
    return out


def _visible_env(doc: dict, job: dict, step: dict) -> set[str]:
    """Env names visible to a step — workflow, then job, then step scope."""
    names: set[str] = set()
    for scope in (doc.get("env"), job.get("env"), step.get("env")):
        if isinstance(scope, dict):
            names |= set(scope)
    return names


@pytest.mark.parametrize("module", sorted(_portal_entrypoints()))
def test_page_fetching_lane_carries_r2_credentials(module: str) -> None:
    steps = _steps_running(module)
    assert steps, f"{module} is invoked by no workflow — stale entrypoint or renamed lane"
    for path, doc, ctx in steps:
        visible = _visible_env(doc, ctx["job"], ctx["step"])
        missing = [v for v in R2_VARS if v not in visible]
        assert not missing, (
            f"{path.name} runs {module} without {', '.join(missing)}. The payload archive "
            f"stores bodies in R2, so this lane would archive nothing while scraping "
            f"normally. Add the four R2_* secrets to the step's env."
        )


def test_price_stats_is_correctly_exempt() -> None:
    """The one lane left out is left out because it reaches no archive call site."""
    for module in NON_ARCHIVING:
        source = (ROOT / f"{module.replace('.', '/')}.py").read_text()
        assert "upsert_portal_raw_page" not in source
        assert "append_payload" not in source


def test_r2_secret_names_match_the_client() -> None:
    """The names asserted above are the ones `R2Client.from_env` actually reads."""
    source = (ROOT / "scraper" / "image_storage.py").read_text()
    for var in R2_VARS:
        assert f'_required("{var}")' in source, f"{var} is not read by R2Client.from_env"


# --- the same defect, one layer out -------------------------------------------------
#
# The scrape lanes reach R2 indirectly, through `scraper.db` -> `payloads.append_payload`.
# The batch lanes reach it DIRECTLY: `location_data.payload_backfill` calls
# `payloads.open_store()` itself, and it shipped with a verify-only env block written when
# bodies were still inline in Postgres. Its first real dispatch refused at id=0 and
# migrated nothing. Same class of bug, different lane, so the gate has to be stated over
# the property that actually matters — "this module opens the object store" — rather than
# over the one set of lanes that happened to be wrong first.


def _direct_store_openers() -> frozenset[str]:
    """Modules whose own source calls `open_store()` — dotted, `python -m` form."""
    found: set[str] = set()
    for base in ("location_data", "scripts", "scraper"):
        for path in sorted((ROOT / base).rglob("*.py")):
            text = path.read_text()
            if re.search(r"(?<!def )\bopen_store\(\)", text):
                found.add(f"{base}.{path.stem}")
    assert found, "no open_store() call sites found — the derivation is broken"
    return frozenset(found)


@pytest.mark.parametrize("module", sorted(_direct_store_openers()))
def test_lane_running_an_r2_module_carries_the_credentials(module: str) -> None:
    """Any workflow step running a module that opens the object store needs the bucket.

    Silent on modules no workflow runs via `python -m` — a library like
    `location_data.archive` is reached through its importer's lane, which this cannot see.
    """
    for path, doc, ctx in _steps_running(module):
        visible = _visible_env(doc, ctx["job"], ctx["step"])
        missing = [v for v in R2_VARS if v not in visible]
        assert not missing, (
            f"{path.name} runs {module}, which opens the R2 object store, without "
            f"{', '.join(missing)}. The step will refuse and do nothing."
        )
