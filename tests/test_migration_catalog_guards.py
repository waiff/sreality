"""A catalog-existence guard in a migration must be able to FIRE.

Migration 432 guarded its `cron.unschedule` with
`to_regproc('cron.unschedule(text)')`, which is NULL two independent ways over:

  * `to_regproc` takes a BARE function name — the argument list belongs to
    `to_regprocedure`. Given a string containing parentheses it returns NULL.
  * even spelled bare, `cron.unschedule` is ambiguous across
    `cron.unschedule(bigint)` and `cron.unschedule(text)`, and `to_regproc` returns
    NULL rather than pick.

So the guarded branch could never run. The migration reported success, the drops went
through, and the cron job survived the teardown pointing at objects that no longer
existed. **A guard that cannot fire is worse than no guard: it looks like protection and
is not**, and nothing in CI could see it — the migration applies cleanly either way, and
the CI replay container has no pg_cron at all, so the branch is skipped there for a
legitimate reason that is indistinguishable from this bug.

This is an offline text rail, deliberately: the defect is in the *spelling*, and the one
environment that would execute the branch is production.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_migration_rls_grants import _statements, _strip_comments

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

# `to_regproc('anything(...)')` — an argument list handed to the bare-name lookup.
_TO_REGPROC_WITH_ARGS = re.compile(
    r"to_regproc\s*\(\s*'[^']*\([^']*'", re.IGNORECASE
)

# Functions this repo guards on that carry more than one overload, so the bare-name form
# is ambiguous and returns NULL. cron.unschedule is the one that bit.
_AMBIGUOUS_BARE = re.compile(
    r"to_regproc\s*\(\s*'\s*cron\.unschedule\s*'\s*\)", re.IGNORECASE
)


def _migration_files() -> list[Path]:
    return sorted(_MIGRATIONS.glob("*.sql")) + sorted(
        (_MIGRATIONS / "reverts").glob("*.sql")
    )


def test_no_to_regproc_is_given_an_argument_list():
    """RED by: writing `to_regproc('cron.unschedule(text)')` in any migration."""
    offenders: list[str] = []
    for path in _migration_files():
        body = _strip_comments(path.read_text())
        for match in _TO_REGPROC_WITH_ARGS.finditer(body):
            offenders.append(f"{path.name}: {match.group(0)}")
    assert not offenders, (
        "to_regproc() takes a BARE function name and returns NULL when handed an argument "
        "list — use to_regprocedure() for a signature. These guards can never fire:\n"
        + "\n".join(offenders)
    )


def test_no_bare_to_regproc_on_an_overloaded_function():
    """RED by: writing `to_regproc('cron.unschedule')`.

    Bare-name lookup returns NULL for an overloaded name rather than picking, so the
    guard silently never fires.
    """
    offenders: list[str] = []
    for path in _migration_files():
        body = _strip_comments(path.read_text())
        if _AMBIGUOUS_BARE.search(body):
            offenders.append(path.name)
    assert not offenders, (
        "cron.unschedule is overloaded (bigint, text), so to_regproc() on the bare name "
        f"is NULL and the guard never fires — use to_regprocedure: {offenders}"
    )


def test_432_unschedules_the_dedup_funnel_job():
    """The teardown's whole premise is that the refresh job stops.

    Without the unschedule the job keeps firing every 15 minutes against dropped
    matviews. RED by: deleting the `cron.unschedule` call from 432.
    """
    body = _strip_comments((_MIGRATIONS / "432_new_dedup_teardown.sql").read_text())
    statements = " ".join(_statements(body)).lower()
    assert "cron.unschedule" in statements, (
        "migration 432 drops the dedup matviews but no longer unschedules their refresh "
        "job — it would fire every 15 minutes against objects that do not exist"
    )
    assert "'dedup-funnel-mv-refresh'" in statements, (
        "432 must unschedule by JOB NAME — the jobid is sequence-assigned and is not 8 "
        "after any restore"
    )
