"""E963: the calibration payload is stored as zlib over its canonical JSON in a bytea.

A jsonb value stops at 268,435,455 bytes of binary (~1.57 times its text), and a nationwide
calibration is ~591 MB of text; a bytea holds 1 GB and the compressed payload is ~3.6 times
smaller. These tests hold the stored form to the cut's calibration byte for byte, the digest with
it; the reader to one; a row cut before the column to a refusal; and a payload past the jsonb
ceiling to a write and a read.
"""

from __future__ import annotations

import ast
import json
import re
import zlib
from pathlib import Path

import pytest

from autodedup import calibration_cut, incremental_lane
from autodedup import incremental_sql as S
from autodedup.calibration_cut import decode_payload, encode_payload
from autodedup.features import FEATURE_VERSION
from autodedup.incremental import Calibration
from autodedup.incremental_lane import cut_calibration, read_calibration, run_incremental
from autodedup.settings import Settings
from tests.autodedup.fake_pg import EMPTY_PAYLOAD, FakePg
from tests.autodedup.lane_world import GENERATION, seed_lane
from tests.autodedup.lane_world import world as lane_world

ROOT = Path(__file__).resolve().parents[2]
# `JENTRY_OFFLENMASK` (src/include/utils/jsonb.h): convertJsonbObject refuses a container whose
# binary is longer.
JSONB_MAX = 0x0FFFFFFF


@pytest.fixture()
def world() -> FakePg:
    return lane_world()


@pytest.fixture(autouse=True)
def _no_held_calibration(monkeypatch) -> None:
    monkeypatch.setattr(incremental_lane, "_CALIBRATIONS", {})


def _canonical(calibration: Calibration) -> bytes:
    return json.dumps(calibration.to_json(), ensure_ascii=False, sort_keys=True).encode("utf-8")


def test_the_stored_payload_is_the_cuts_canonical_json_and_reads_back_to_its_digest(
        world, tmp_path) -> None:
    out = seed_lane(world, tmp_path)
    row = world.calibration[GENERATION]
    blob = row["payload_zlib"]
    assert isinstance(blob, bytes) and "payload" not in row and "artifact_url" not in row
    _row, calibration = read_calibration(world)
    assert out["calibration"]["digest"] == row["digest"] == calibration.digest()
    assert zlib.decompress(blob) == _canonical(calibration), "the bytes are the canonical JSON"
    assert encode_payload(calibration) == blob, "and re-encode to themselves"
    assert decode_payload(blob).to_json() == calibration.to_json()


def test_the_digest_is_the_calibrations_not_the_stored_bytes(world, tmp_path,
                                                            monkeypatch) -> None:
    """`built_at` is in the payload and out of the digest (E70): two cuts of one scope store
    different bytes under one digest, and each reads back to it."""
    seed_lane(world, tmp_path)
    first = dict(world.calibration[GENERATION])
    monkeypatch.setattr(calibration_cut, "_now", lambda: "2099-01-01T00:00:00+00:00")
    cut_calibration(world, Settings(), "m", GENERATION)
    second = world.calibration[GENERATION]
    assert first["payload_zlib"] != second["payload_zlib"]
    assert first["digest"] == second["digest"]
    assert (decode_payload(first["payload_zlib"]).digest()
            == decode_payload(second["payload_zlib"]).digest() == second["digest"])


def test_the_reader_reads_the_payload_once_a_digest_and_hands_back_no_bytes(
        world, tmp_path, monkeypatch) -> None:
    seed_lane(world, tmp_path)
    reads: list[bool] = []
    original = incremental_lane._rows

    def spy(conn, sql, params=None):
        rows = original(conn, sql, params)
        if sql == S.RT_CALIBRATION_READ_SQL:
            reads.append(rows[0][3] is not None)
        return rows

    monkeypatch.setattr(incremental_lane, "_rows", spy)
    first_row, first = read_calibration(world)
    again_row, again = read_calibration(world)
    assert reads == [True, False]
    assert again is first
    assert first_row[3] is None and again_row[3] is None, "a pass keeps no compressed bytes"
    assert first_row[1] == world.calibration[GENERATION]["digest"]
    assert len(first_row) == 7


def test_a_row_cut_before_616_is_refused_not_read_as_an_empty_calibration(world,
                                                                         tmp_path) -> None:
    seed_lane(world, tmp_path)
    world.calibration[GENERATION].pop("payload_zlib")
    with pytest.raises(SystemExit, match="payload_zlib.*E963.*fresh=true"):
        read_calibration(world)
    pairs = dict(world.pairs)
    with pytest.raises(SystemExit, match="payload_zlib"):
        run_incremental(lambda: world)
    assert world.pairs == pairs, "no pass decided under an empty calibration"


def test_a_payload_that_decodes_to_another_digest_is_refused_and_never_held(world,
                                                                           tmp_path) -> None:
    """The reader checks the bytes against the row before it memoises them (E963): an encoding
    that drifted from its decoding is a loud refusal, not a calibration held under a digest it
    does not have."""
    seed_lane(world, tmp_path)
    row = world.calibration[GENERATION]
    row["payload_zlib"] = EMPTY_PAYLOAD
    assert decode_payload(EMPTY_PAYLOAD).digest() != row["digest"]
    with pytest.raises(SystemExit, match="decodes to digest"):
        read_calibration(world)
    assert incremental_lane._CALIBRATIONS == {}, "nothing memoised under the row's digest"
    pairs = dict(world.pairs)
    with pytest.raises(SystemExit, match="decodes to digest"):
        run_incremental(lambda: world)
    assert world.pairs == pairs and incremental_lane._CALIBRATIONS == {}


def test_a_database_without_616_is_store_absent(tmp_path) -> None:
    """The presence check names the column, so code merged before 616 is applied skips its
    passes (`store_absent` in the worker) instead of failing each one on it."""
    from scraper import realtime_worker as rw
    from tests.autodedup.test_reject_ledger import _seeded_reject_world

    assert "attname = 'payload_zlib'" in S.RT_STORE_PRESENT_SQL
    world = _seeded_reject_world(tmp_path)
    world.absent_columns = {"rt_calibration.payload_zlib"}
    assert rw._autodedup_store_present(world) is False
    with pytest.raises(SystemExit, match="616"):
        run_incremental(lambda: world)
    world.absent_columns = set()
    assert rw._autodedup_store_present(world) is True
    assert not run_incremental(lambda: world)["aborted"]


def test_a_payload_past_the_jsonb_ceiling_is_written_and_read(world, tmp_path,
                                                              monkeypatch) -> None:
    """257 codes of 1 MiB: canonical JSON past 256 MiB, the size a jsonb value refuses. The
    cut writes it through the lane's own statement and the one reader reads it to its digest.
    (~0.8 GB at the peak; the codes compress ~1000:1, so FakePg stores a few hundred kB.)"""
    seed_lane(world, tmp_path)
    code = "c" * (1 << 20)
    big = Calibration(generation=GENERATION, feature_version=FEATURE_VERSION, built_at="t",
                      n_listings=1, certifying_codes=[code] * 257)
    monkeypatch.setattr(calibration_cut.CalibrationCut, "build",
                        lambda self, generation=GENERATION: big)
    out = cut_calibration(world, Settings(), "m", GENERATION)
    blob = world.calibration[GENERATION]["payload_zlib"]
    assert len(zlib.decompress(blob)) > JSONB_MAX and len(blob) < 1 << 20
    del big
    _row, read = read_calibration(world)
    assert read.certifying_codes == [code] * 257
    assert read.digest() == out["digest"] == world.calibration[GENERATION]["digest"]


# --------------------------------------------------------------------- one reader, one writer

def _python_sources() -> list[Path]:
    return [path for top in ("autodedup", "scraper", "api", "scripts", "toolkit")
            for path in sorted((ROOT / top).rglob("*.py"))]


def _sql_constants() -> dict[str, str]:
    return {name: value for name, value in vars(S).items()
            if name.endswith("_SQL") and isinstance(value, str)}


def test_one_statement_reads_the_payload_and_one_writes_it() -> None:
    naming = {name for name, sql in _sql_constants().items() if "payload_zlib" in sql}
    assert naming == {"RT_CALIBRATION_READ_SQL", "RT_CALIBRATION_WRITE_SQL",
                      "RT_STORE_PRESENT_SQL"}, naming
    assert "attname = 'payload_zlib'" in S.RT_STORE_PRESENT_SQL
    assert "c.payload_zlib" in S.RT_CALIBRATION_READ_SQL
    assert "%(payload_zlib)s::bytea" in S.RT_CALIBRATION_WRITE_SQL
    users = sorted(str(path.relative_to(ROOT)) for path in _python_sources()
                   if "payload_zlib" in path.read_text(encoding="utf-8"))
    assert users == ["autodedup/calibration_cut.py", "autodedup/incremental_lane.py",
                     "autodedup/incremental_sql.py"], users


def test_no_statement_names_the_retired_columns() -> None:
    """`payload` (jsonb) and `artifact_url` are read and written by nothing, so a later
    migration drops them without a code change."""
    statements = 0
    for path in _python_sources():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and any(isinstance(t, ast.Name) and t.id.endswith("_SQL")
                            for t in node.targets)
                    and "autodedup.rt_calibration" in node.value.value):
                statements += 1
                assert not re.search(r"\bpayload\b", node.value.value), (path, node.value.value)
                assert "artifact_url" not in node.value.value, (path, node.value.value)
    assert statements == 5, "PRESENT, READ, WRITE, SCORER and the store's presence check"
    for top in ("api", "frontend/src"):
        for path in sorted((ROOT / top).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".ts", ".tsx"}:
                assert "rt_calibration" not in path.read_text(encoding="utf-8"), path


def test_the_payload_statement_is_used_by_read_calibration_alone() -> None:
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(ROOT))
        if rel == "autodedup/incremental_sql.py":
            continue
        tree = ast.parse(text)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = {node.id for node in ast.walk(fn) if isinstance(node, ast.Name)}
            calls = {node.func.id for node in ast.walk(fn)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            if "RT_CALIBRATION_READ_SQL" in names or "decode_payload" in calls:
                assert (rel, fn.name) == ("autodedup/incremental_lane.py",
                                          "read_calibration"), (rel, fn.name)
