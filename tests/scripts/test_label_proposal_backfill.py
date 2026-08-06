"""scripts/label_proposal_backfill.py — pure-function coverage.

Mirrors tests/scripts/test_clip_tag_backfill.py's shape for the analogous
production script: _chunks, _download_decode (mocked R2), and a sanity check
on the SQL text (join key + upsert conflict target) since a wrong key here
would silently mis-scope or double-propose without any DB to catch it in a
hermetic test.
"""

from __future__ import annotations

import io

from scripts import label_proposal_backfill as lpb


def test_chunks_splits_evenly_and_handles_remainder() -> None:
    assert list(lpb._chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(lpb._chunks([], 2)) == []
    assert list(lpb._chunks([1], 5)) == [[1]]


def test_select_pending_keys_on_sample_and_excludes_existing_proposals() -> None:
    # dedup_sim.labeling_sample.image_id is the scope; a proposal already on
    # file for the CURRENT model must be excluded so a rerun doesn't
    # re-download/re-score images it's already proposed a label for.
    assert "dedup_sim.labeling_sample s" in lpb._SELECT_PENDING
    assert "dedup_sim.label_proposals p" in lpb._SELECT_PENDING
    assert "p.model = %(model)s" in lpb._SELECT_PENDING
    assert "NOT EXISTS" in lpb._SELECT_PENDING


def test_upsert_conflict_target_matches_the_table_pk() -> None:
    # dedup_sim.label_proposals' PK is (image_id, model) (migration 373) —
    # ON CONFLICT DO NOTHING here relies on that exact target.
    assert "ON CONFLICT (image_id, model) DO NOTHING" in lpb._UPSERT_SQL


class _FakeR2:
    def __init__(self, data: dict) -> None:
        self._data = data

    def download_bytes(self, key: str) -> bytes:
        v = self._data[key]
        if isinstance(v, Exception):
            raise v
        return v


def test_download_decode_skips_both_corrupt_and_transient_failures() -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    good = buf.getvalue()
    r2 = _FakeR2({"good": good, "bad": b"not-an-image", "gone": RuntimeError("R2 blip")})

    decoded = lpb._download_decode(
        r2, [(1, "good"), (2, "bad"), (3, "gone")], workers=2,
    )
    assert {d[0] for d in decoded} == {1}
    # Unlike the production tagger, this script has no in-table marker to
    # terminal-mark a permanently-undecodable image against — both the
    # corrupt row and the transient failure just drop from this run's
    # result and get retried on the next dispatch.
    assert 2 not in {d[0] for d in decoded}
    assert 3 not in {d[0] for d in decoded}


def test_main_dry_run_no_op_when_r2_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://x")
    monkeypatch.setattr(lpb.image_storage, "is_configured", lambda: False)
    monkeypatch.setattr(lpb.sys, "argv", ["label_proposal_backfill", "--dry-run"])
    assert lpb.main() == 0


def test_main_errors_without_db_url(monkeypatch, capsys) -> None:
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(lpb.sys, "argv", ["label_proposal_backfill"])
    assert lpb.main() == 2
    assert "SUPABASE_DB_URL" in capsys.readouterr().err
