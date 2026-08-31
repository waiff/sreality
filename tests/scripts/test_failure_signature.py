"""The W3.1 normalizer, pinned against the two real incidents it exists for.

The whole mechanism is an asymmetry: the signature is derived from the error TEXT and
never from `workflow_path`, EXCEPT for the unreadable-red fallback, which is scoped by
it. Both halves are asserted here — one without the other is useless.
"""

from __future__ import annotations

import psycopg

from scripts import failure_signature as fs

# The 2026-08-26 outage, verbatim, as psycopg renders it (line 2 is the DETAIL line
# carrying a whole listing row — high-cardinality and PII-adjacent).
CHECK_VIOLATION = (
    'new row for relation "listings" violates check constraint '
    '"listings_area_basis_check"\n'
    "DETAIL:  Failing row contains (81234, mmreality, 4471203, Praha 5, 8900000, plot)."
)

# The 2026-08-27 claim-intake outage: a contract edited without a version bump.
CONTRACT_ERROR = (
    "ContractError: ceskereality is already loaded with a different sha256 on record "
    "9f1c4b7ae2d0aa31bb5e6f0c2d4e8a91c3b7d5f2e1a0c9b8d7e6f5a4b3c2d1e0; "
    "contract entries are immutable, bump contract_version"
)


def _actions_log(*lines: str) -> str:
    """Actions prefixes EVERY log line with an ISO timestamp; nothing is at column 0."""
    return "".join(f"2026-08-26T20:15:0{i % 10}.1234567Z {ln}\n" for i, ln in enumerate(lines))


# --- the two ground-truth incidents ---------------------------------------


def test_check_violation_is_one_signature_across_every_portal() -> None:
    """Eight workflows carried this text. If the key varied with the workflow the
    operator gets eight emails again, which is the entire thing W3 exists to stop."""
    from_exception = fs.signature_from_exception(
        psycopg.errors.CheckViolation(CHECK_VIOLATION)
    )
    logs = [
        _actions_log(
            f"DRAIN source={portal} claimed=50",
            f"psycopg.errors.CheckViolation: {CHECK_VIOLATION.splitlines()[0]}",
        )
        for portal in ("mmreality", "ceskereality", "maxima", "bezrealitky",
                       "realitymix", "remax", "idnes")
    ]
    from_logs = {fs.signature_from_log(t) for t in logs}
    assert len(from_logs) == 1
    assert from_logs == {from_exception}
    assert from_exception == (
        "checkviolation|new row for relation listings violates check constraint "
        "listings_area_basis_check"
    )


def test_check_violation_keeps_the_constraint_name_through_the_digit_strip() -> None:
    """The quoted identifier IS the key. A placeholder containing digits gets eaten by
    the digit-stripper and the constraint name silently vanishes — a real bug on the
    first pass of this module."""
    sig = fs.signature_from_exception(psycopg.errors.CheckViolation(CHECK_VIOLATION))
    assert "listings_area_basis_check" in sig
    assert "\x00" not in sig and "\x01" not in sig


def test_check_violation_drops_the_detail_row() -> None:
    sig = fs.signature_from_exception(psycopg.errors.CheckViolation(CHECK_VIOLATION))
    assert "failing row" not in sig
    assert "praha" not in sig and "4471203" not in sig


def test_contract_error_is_one_signature_regardless_of_the_hash() -> None:
    a = fs.signature_from_text(CONTRACT_ERROR)
    b = fs.signature_from_text(CONTRACT_ERROR.replace("9f1c4b7a", "0000dead"))
    assert a == b
    assert a is not None and a.startswith("contracterror|")
    assert "contract entries are immutable" in a
    assert "9f1c4b7a" not in a


# --- normalization rails ---------------------------------------------------


def test_exception_class_is_not_detected_by_an_error_suffix() -> None:
    """The corpus's most important classes all fail an `*Error` allowlist."""
    for cls in ("CheckViolation", "QueryCanceled", "AdminShutdown",
                "AmbiguousFunction", "InsufficientPrivilege"):
        sig = fs.signature_from_text(f"psycopg.errors.{cls}: something broke")
        assert sig == f"{cls.lower()}|something broke"


def test_all_caps_log_labels_are_not_mistaken_for_a_class() -> None:
    assert fs.signature_from_text("ERROR: could not connect") is None
    assert fs.signature_from_text("WARNING: slow query") is None


def test_http_status_codes_survive_the_digit_strip() -> None:
    """A blanket digit strip collapses these two into one useless `httperror|from`;
    an mmreality proxy 403 and an iDNES 500 are different incidents."""
    a = fs.signature_from_text("requests.exceptions.HTTPError: 403 from https://a.cz/x")
    b = fs.signature_from_text("requests.exceptions.HTTPError: 500 from https://b.cz/y")
    assert a != b
    assert "403" in (a or "") and "500" in (b or "")


def test_volatile_tokens_normalize_away() -> None:
    base = fs.signature_from_exception(RuntimeError("job failed"))
    noisy = fs.signature_from_exception(RuntimeError(
        "job 91827 failed at 2026-08-26T20:15:03Z for "
        "3f0a1b2c-4d5e-6f70-8192-a3b4c5d6e7f8 in /home/runner/work/repo/scraper/db.py"
    ))
    assert noisy.startswith("runtimeerror|job failed at for in")
    assert base.startswith("runtimeerror|job failed")


def test_signature_is_truncated() -> None:
    sig = fs.signature_from_exception(RuntimeError("word " * 400))
    assert len(sig) <= fs.MAX_SIGNATURE_LEN


# --- the log tiers ---------------------------------------------------------


def test_verify_pipeline_check_line_yields_one_signature_per_check() -> None:
    """Keying on the SET of failing checks fragmented 17 sampled runs into 14
    signatures; per-check keying collapses them to a handful of stable ones."""
    text = _actions_log(
        "2026-08-26 20:15:03,001 INFO verify_pipeline CHECK llm_errors status=ok value=0.0",
        "2026-08-26 20:15:03,002 INFO verify_pipeline CHECK property_maintenance status=fail value=1.0",
    )
    assert fs.signature_from_log(text) == "check:property_maintenance|fail"


def test_a_real_exception_outranks_the_check_line() -> None:
    text = _actions_log(
        "INFO verify_pipeline CHECK property_maintenance status=fail value=1.0",
        "psycopg.errors.QueryCanceled: canceling statement due to statement timeout",
    )
    assert fs.signature_from_log(text) == (
        "querycanceled|canceling statement due to statement timeout"
    )


def test_self_caught_errors_are_readable_without_a_traceback() -> None:
    """The bazos enrichment lane (14% of the corpus) prints its own error and exits 1
    with no traceback at all; without this tier the biggest LLM-outage signature is
    invisible to both producers."""
    aborting = _actions_log(
        "ENRICH id=771 error=openai call failed: HTTP 429 rate limited",
        "ENRICH aborting: 5 consecutive errors (provider outage?)",
    )
    assert fs.signature_from_log(aborting) == "aborting|consecutive errors provider outage"
    only_kv = _actions_log("ENRICH id=771 error=openai call failed: HTTP 429 rate limited")
    assert fs.signature_from_log(only_kv) == "error|openai call failed http 429 rate limited"


def test_annotation_is_the_last_tier() -> None:
    text = _actions_log("##[error]The action has timed out after 60 minutes.")
    assert fs.signature_from_log(text) == "annotation|the action has timed out after minutes"


def test_unreadable_log_returns_none() -> None:
    assert fs.signature_from_log("") is None
    assert fs.signature_from_log(_actions_log("Post job cleanup.", "Cleaning up orphan")) is None


# --- the fallback, which IS scoped ------------------------------------------


def test_fallback_never_merges_two_unrelated_workflows() -> None:
    """Unscoped, `step:|exit:1` merged 13 runs across 10 unrelated workflows into one
    meaningless mega-incident."""
    a = fs.fallback_signature(
        workflow_path=".github/workflows/test.yml", step_name="Run tests", exit_code=1)
    b = fs.fallback_signature(
        workflow_path=".github/workflows/images.yml", step_name="Run tests", exit_code=1)
    assert a != b
    assert a.endswith("@.github/workflows/test.yml")


def test_fallback_tolerates_a_missing_everything() -> None:
    sig = fs.fallback_signature(workflow_path=None)
    assert sig == "step:unknown|exit:unknown@unknown"


# --- the excerpt -----------------------------------------------------------


def test_excerpt_ends_at_the_error_not_at_the_file_tail() -> None:
    """The last ~25 lines of every Actions log are runner cleanup; a tail shows the
    operator git credential teardown instead of the exception."""
    text = _actions_log(
        "DRAIN source=mmreality claimed=50",
        "psycopg.errors.CheckViolation: violates check constraint",
        "Post job cleanup.",
        "[command]/usr/bin/git config --unset-all http.extraheader",
        "Cleaning up orphan processes",
    )
    excerpt = fs.excerpt_from_log(text)
    assert excerpt.endswith("psycopg.errors.CheckViolation: violates check constraint")
    assert "orphan processes" not in excerpt


def test_excerpt_respects_the_byte_cap() -> None:
    text = _actions_log(*["x" * 500 for _ in range(200)])
    assert len(fs.excerpt_from_log(text, max_bytes=1000).encode()) <= 1000
