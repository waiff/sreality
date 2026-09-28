"""scripts/tag_model.py dump — the listing_ids_file dump reads SCORES only.

G4's measurement pass may keep its vectors in R2 (--vectors-to r2), so the dump the offline
arms read (tag_model.yml stage=dump) must not need image_dinov3_embeddings: here every
statement that touches that table fails, and the dump still writes every scored image of
the listed listings.
"""

from __future__ import annotations

import argparse
import gzip
import json

from scripts import tag_model
from toolkit import tag_models as tm


class _Model:
    id = 3
    version = "v1"

    def as_dict(self):
        return {"id": self.id, "version": self.version}


class _Conn:
    def __init__(self) -> None:
        self.rolled_back = 0
        self.listing_batches: list[list[int]] = []

    def cursor(self):
        return _Cur(self)

    def rollback(self) -> None:
        self.rolled_back += 1


class _Cur:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        if "image_dinov3_embeddings" in sql:
            raise RuntimeError("the vectors live in R2 in this mode")
        if "FROM images i" in sql and "image_tag_scores s" in sql and "listing_id = any" in sql:
            ids = list(params["ids"])
            self._conn.listing_batches.append(ids)
            self._rows = [(10 * lid + k, lid, None, 0.9, json.dumps({"1": 0.9, "2": 0.05}),
                           "kitchen") for lid in ids for k in (1, 2)]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)


def test_the_listing_dump_needs_only_the_scores(tmp_path, monkeypatch):
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("7\n5\n")
    monkeypatch.setattr(tm, "active_model", lambda conn: _Model())
    conn = _Conn()
    out = tmp_path / "dump"
    args = argparse.Namespace(version=None, listing_ids_file=str(ids_file), out=str(out))
    assert tag_model._do_dump(conn, args) == 0
    assert conn.listing_batches == [[5, 7]]
    rows = [json.loads(line) for line in gzip.open(out / "tag_dump.jsonl.gz", "rt")]
    assert sorted(r["image_id"] for r in rows) == [51, 52, 71, 72]
    assert all(r["winner"] == 0.9 and r["runner_up"] == 0.05 for r in rows)
    probes = json.loads((out / "probe.json").read_text())
    assert str(probes["dinov3_rows"]).startswith("error:")      # advisory, and survived
    assert conn.rolled_back == 1
