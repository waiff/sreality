"""The walk must ask for every category its own parser knows how to read.

The bug this exists to catch is not a crash — it is a SILENCE. bazos's parser has
mapped `pozemek` / `zahrada` / `garaz` / `ostatni` since the beginning, but
`portals.categories` never listed them, so the index walk never visited those
sections. Nothing errors when a category is simply never requested: no failed
fetch, no `scrape_runs` anomaly, no drift row (the live-category seam of
migration 482 is optional and bazos has none). Migration 160 wrote the exclusion
down as temporary — "pozemek / garaz / ostatni are deliberately left out for
now" — and it survived for months until an operator noticed one missing ad.

So: for every portal whose parser exposes a slug->canonical `CATEGORY_MAIN` map
AND whose walk is driven by `scraper.portal.default_config`, every real URL slug
in that map must appear in the walked categories. Deliberate exclusions are
allowed but must be written here with a reason, which turns "we forgot" into a
diff someone has to justify.
"""

from __future__ import annotations

from scraper import (
    bazos_parser,
    ceskereality_parser,
    idnes_parser,
    realitymix_parser,
)
from scraper.portal import default_config

# slug -> why the walk does not ask for it. A slug is exempt ONLY as a defensive
# alias the parser accepts when reading a page, never as a section the portal
# actually publishes under its own path.
_EXEMPT: dict[str, dict[str, str]] = {
    "bazos": {
        "pozemky": "breadcrumb-text alias; the real URL slug is /pozemek/",
        "nebytove": "legacy alias for the commercial sections; not a live path",
        "komercni": "canonical-name alias; bazos splits commercial four ways",
    },
    "idnes": {},
    "ceskereality": {},
    "realitymix": {},
}

_PORTALS = {
    "bazos": bazos_parser.CATEGORY_MAIN,
    "idnes": idnes_parser.CATEGORY_MAIN,
    "ceskereality": ceskereality_parser.CATEGORY_MAIN,
    "realitymix": realitymix_parser.CATEGORY_MAIN,
}


def _walked(source: str) -> set[str]:
    return {c["category"] for c in default_config(source).categories}


def test_every_known_category_slug_is_walked():
    for source, category_main in _PORTALS.items():
        walked = _walked(source)
        missing = set(category_main) - walked - set(_EXEMPT[source])
        assert not missing, (
            f"{source}: parser maps {sorted(missing)} but the walk never asks for "
            f"them — listings filed there are silently never ingested. Add them to "
            f"_DEFAULTS[{source!r}].categories (+ a migration for the registry row), "
            f"or record an exemption with a reason in _EXEMPT."
        )


def test_every_walked_category_slug_is_understood_by_the_parser():
    # The other direction: `_resolve_scopes` drops a scope whose slug the parser
    # cannot label, so a typo in the config is silent too.
    for source, category_main in _PORTALS.items():
        unknown = _walked(source) - set(category_main)
        assert not unknown, f"{source}: walks {sorted(unknown)}, parser maps none of them"


def test_exemptions_are_live_and_covered():
    for source, exempt in _EXEMPT.items():
        category_main = _PORTALS[source]
        walked_canon = {category_main[s] for s in _walked(source)}
        for slug, reason in exempt.items():
            assert slug in category_main, f"{source}: stale exemption {slug!r}"
            assert reason, f"{source}: exemption {slug!r} needs a reason"
            # An alias is only safe to skip while some walked slug already reaches
            # the same canonical category.
            assert category_main[slug] in walked_canon, (
                f"{source}: {slug!r} is exempt but nothing walked reaches "
                f"category_main={category_main[slug]!r}"
            )
