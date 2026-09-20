"""No lane artifact carries the operator's identity.

Every lane uploads `out/` to a GitHub Actions artifact of a PUBLIC repository, so anything a
lane writes is readable by anyone who can see the run. Advert-side PII has had a rail since
W2 (`export_sql`'s broker columns, E28). The OPERATOR's own identity did not: `mode=labels`
shipped `autodedup.verdicts.decided_by` — a login, which is an e-mail address — verbatim in
its first run, and the artifact had to be deleted.

The fix is the same idiom the broker columns use: a salted digest, because what a label needs
from `decided_by` is only whether two rows came from the same person. This census keeps the
rail general — a lane module may MENTION an identity column only where it is declared to be
digested or excluded, so the next writer that selects one has to come through here first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from autodedup import labels_lane

PACKAGE = Path(labels_lane.__file__).resolve().parent

# The modules that produce a file under `out/`, and the statements they run to fill it.
LANE_WRITERS: tuple[str, ...] = (
    "census.py", "compare.py", "errors.py", "evaluate.py", "export.py", "export_sql.py",
    "harness.py", "incremental_lane.py", "incremental_sql.py", "iterations.py", "judge.py",
    "judge_lane.py", "judge_prompts.py",
    "judge_sql.py", "labels.py", "labels_lane.py", "labels_sql.py", "lane.py",
    "progress_sql.py", "replay.py", "score_lane.py", "score_sql.py", "seals.py",
    "structural_truth.py",
)

# Columns and claim names that identify a PERSON — the operator or a broker.
# `claims.get` rather than `claims`: location data has its own, unrelated "claims" and the
# JWT reader is what matters here (api/routes reads the operator's e-mail out of one).
IDENTITY_TOKENS: tuple[str, ...] = (
    "decided_by", "created_by", "broker_name", "broker_email", "broker_phone",
    "user_email", "user_id", "claims.get",
)

# module -> the tokens it may name, because that module is where the scrub happens.
DECLARED: dict[str, frozenset[str]] = {
    # `decided_by` is read and immediately digested by `decider()` before any row is written.
    "labels_lane.py": frozenset({"decided_by"}),
    "labels_sql.py": frozenset({"decided_by"}),
    # Reads the artifact, where the value is ALREADY a digest.
    "labels.py": frozenset({"decided_by"}),
    # E28's broker rail: two columns selected as hash inputs, one never selected at all.
    "export.py": frozenset({"broker_email", "broker_phone"}),
    "export_sql.py": frozenset({"broker_name", "broker_email", "broker_phone"}),
}


def _mentions(path: Path, token: str) -> list[int]:
    return [n for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if token in line]


def test_only_the_declared_modules_touch_an_identity_column() -> None:
    offenders: list[str] = []
    for name in LANE_WRITERS:
        path = PACKAGE / name
        assert path.is_file(), f"{name} is listed as a lane writer but does not exist"
        allowed = DECLARED.get(name, frozenset())
        for token in IDENTITY_TOKENS:
            if token in allowed:
                continue
            lines = _mentions(path, token)
            if lines:
                offenders.append(f"{name}:{lines[0]} {token}")
    assert not offenders, (
        "a lane writer names an identity column without declaring how it is scrubbed "
        f"(add it to DECLARED with the reason, or digest it): {', '.join(offenders)}"
    )


def test_the_lane_writer_census_covers_the_package() -> None:
    """A new module under autodedup/ that writes an artifact must join the list rather than
    quietly sit outside the rail. Modules that only compute are listed as exempt."""
    exempt = {
        "__init__.py", "agreement.py", "blocking.py", "candidates.py", "cluster.py",
        "cohort.py", "dataset.py", "decide.py", "ensembles.py", "features.py",
        "fingerprint.py", "guards.py", "hazard_context.py", "incremental.py",
        "incremental_scope.py", "incremental_store.py",
        "model.py", "normalize.py", "oss_pod.py", "settings.py", "stock.py",
            "text_facts.py",
        "ui_sql.py", "verdict_reasons.py",
    }
    present = {path.name for path in PACKAGE.glob("*.py")}
    unclassified = present - set(LANE_WRITERS) - exempt
    assert not unclassified, (
        "classify these modules as lane writers (scanned) or exempt (compute only): "
        f"{sorted(unclassified)}"
    )


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def test_a_login_never_survives_into_the_labels_artifact(tmp_path: Path) -> None:
    """The end-to-end half of the rail: the store's value is an e-mail and the file's is not."""
    digest = labels_lane.decider("petr.hejtmanek@example.cz")
    assert digest and digest.startswith(labels_lane.DECIDED_BY_PREFIX)
    assert not EMAIL.search(digest)
    record = labels_lane.build_label_record(
        1, 2, verdict="same", source="explicit", reasons=[], note="stejny byt",
        decided_by="petr.hejtmanek@example.cz", decided_at=None, cluster_key=None,
        sample_rank=None, must_not_link=False, engine=None,
    )
    assert not EMAIL.search(json.dumps(record, ensure_ascii=False))
    assert record["decided_by"] == digest


@pytest.mark.parametrize("value", ["A@b.cz", " a@b.cz ", "a@b.cz"])
def test_the_digest_answers_same_person_and_nothing_else(value: str) -> None:
    """Stable across runs (a constant salt) and across the whitespace a login arrives with,
    but not reversible and not comparable to a different address."""
    assert labels_lane.decider(value.strip().lower()) == labels_lane.decider("a@b.cz")
    assert labels_lane.decider("a@b.cz") != labels_lane.decider("a@c.cz")
