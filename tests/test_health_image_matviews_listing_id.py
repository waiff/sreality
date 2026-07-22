"""Offline guard: the Health + image matviews must key on the surrogate listing
identity, not the portal-native sreality_id (listing-identity refactor, Gate 2).

No DB here — like tests/test_listings_pk_swap_migration.py and
tests/test_migration_rls_grants.py this is a fast regex gate over the migration
SQL. The CI schema-replay (.github/workflows/migrations.yml) proves the DDL
applies; this proves the CURRENT (latest) tracked definition of each matview uses
the right join/group key, so a revert or a future edit that reintroduces the
sreality_id key turns red here.

Post-Gate-2 a new non-sreality listing inserts sreality_id = NULL, so any of these
keys reading the wrong id-space silently drops or mis-buckets those rows (the
churn check would even grade a thrashing portal 'pass'). The id-spaces overlap
numerically but are disjoint, so the failure is silent, never an error — hence a
static contract test.
"""
from __future__ import annotations

import re
from pathlib import Path

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_WS = re.compile(r"\s+")


def _strip_sql_comments(sql: str) -> str:
    """Drop `--` line comments that are NOT inside a single-quoted string, so the
    header prose (which discusses sreality_id) can never satisfy an assertion about
    the executed SQL. No dollar-quotes exist in these migrations."""
    out: list[str] = []
    i, n, in_str = 0, len(sql), False
    while i < n:
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append(sql[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            out.append(ch)
            i += 1
            continue
        if sql[i : i + 2] == "--":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _norm(text: str) -> str:
    return _WS.sub(" ", _strip_sql_comments(text)).lower()


_MATVIEWS = (
    "snapshot_churn_24h_mv",
    "images_failure_overview_mv",
    "image_storage_overview_mv",
    "health_summary_mv",
    "scraper_health_checks_mv",
)


def _create_re(mv: str) -> re.Pattern[str]:
    return re.compile(rf"create materialized view (?:if not exists )?{re.escape(mv)} as")


def _latest_creator(mv: str) -> tuple[str, str]:
    """(filename, normalized-full-text) of the LAST migration that runs
    `create materialized view <mv>` — the definition in force today. Matches the
    `if not exists` form too, so an older definition is still reasoned about."""
    pat = _create_re(mv)
    hit: tuple[str, str] | None = None
    for p in sorted(_MIGRATIONS.glob("*.sql")):
        norm = _norm(p.read_text(encoding="utf-8"))
        if pat.search(norm):
            hit = (p.name, norm)
    assert hit is not None, f"no migration creates materialized view {mv}"
    return hit


def _body(mv: str) -> str:
    """The `create materialized view <mv> as …` statement body, bounded to the
    following `create unique index` so a sibling matview's join (e.g.
    scraper_health_checks_mv.fails_agg, deliberately still on sreality_id) can
    never bleed into this matview's assertions."""
    _name, norm = _latest_creator(mv)
    start = _create_re(mv).search(norm).start()
    end = norm.index("create unique index", start)
    return norm[start:end]


# --- the four repoints this item owns --------------------------------------

def test_snapshot_churn_joins_on_listing_id():
    body = _body("snapshot_churn_24h_mv")
    assert "join listings l on l.id = s.listing_id" in body
    assert "l.sreality_id = s.sreality_id" not in body, (
        "snapshot_churn_24h_mv still joins snapshots↔listings on sreality_id — "
        "post-Gate-2 NULL-sreality snapshots vanish, churn ratio → 0, and a "
        "thrashing portal grades 'pass' (fails open)"
    )


def test_images_failure_joins_on_listing_id():
    body = _body("images_failure_overview_mv")
    assert "join listings l on l.id = i.listing_id" in body
    assert "l.sreality_id = i.sreality_id" not in body, (
        "images_failure_overview_mv still joins images↔listings on sreality_id — "
        "failed images on NULL-sreality listings vanish from the dashboard"
    )


def test_image_storage_joins_on_listing_id():
    body = _body("image_storage_overview_mv")
    assert "left join images i on i.listing_id = l.id" in body
    assert "i.sreality_id = l.sreality_id" not in body, (
        "image_storage_overview_mv still LEFT joins images↔listings on sreality_id "
        "— new listings undercount stored=0/total=0 (rule #6)"
    )


def test_health_summary_snap_density_groups_on_listing_id():
    body = _body("health_summary_mv")
    assert "from listing_snapshots_public group by listing_id" in body, (
        "health_summary_mv.snap_density must count snapshots per listing_id"
    )
    assert "group by sreality_id" not in body, (
        "health_summary_mv.snap_density still GROUPs BY sreality_id — post-Gate-2 "
        "every NULL-keyed snapshot collapses into one bucket"
    )


# --- structural safety of the DROP+CREATE migration -------------------------

def test_drops_dependent_matview_before_its_input():
    # scraper_health_checks_mv reads snapshot_churn_24h_mv, so the dependent must
    # be dropped first or the DROP of the input is blocked.
    _name, norm = _latest_creator("snapshot_churn_24h_mv")
    dep = norm.index("drop materialized view if exists scraper_health_checks_mv")
    inp = norm.index("drop materialized view if exists snapshot_churn_24h_mv")
    assert dep < inp, "must drop scraper_health_checks_mv before snapshot_churn_24h_mv"


def test_every_recreated_matview_is_dark_to_browser_roles():
    # A fresh matview inherits anon+authenticated grants from the project default
    # ACL; the live post-remediation posture is dark. Each recreate must revoke.
    _name, norm = _latest_creator("snapshot_churn_24h_mv")
    for mv in _MATVIEWS:
        assert f"revoke all on {mv} from anon, authenticated" in norm, (
            f"{mv} recreated without `revoke all … from anon, authenticated` — it "
            f"would leak to browser roles via the default ACL"
        )
        assert not re.search(
            rf"grant\s+[^;]*\b{re.escape(mv)}\b[^;]*\bto\b[^;]*\b(anon|authenticated)\b",
            norm,
        ), f"{mv} must not be re-granted to a browser role"


def test_creates_carry_the_ungated_exemption():
    # test_migration_rls_grants.py flags a create that names an admin-only relation
    # (each matview's own name) with no is_platform_admin() gate; a matview cannot
    # embed one, so each create needs the escape-hatch annotation.
    name, _norm_txt = _latest_creator("snapshot_churn_24h_mv")
    raw = (_MIGRATIONS / name).read_text(encoding="utf-8").lower()
    exempt = set(re.findall(r"--\s*ci-allow-ungated:\s*([a-z0-9_]+)", raw))
    for mv in _MATVIEWS:
        assert mv in exempt, f"{mv} create needs a `-- ci-allow-ungated:` annotation"
