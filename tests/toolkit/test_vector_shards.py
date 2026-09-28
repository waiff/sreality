"""toolkit/vector_shards.py — float16 shard files + manifest in object storage (G4 R2 mode).

The claims: a part round-trips exactly (and is numpy's own .npz, both ways), the manifest line
is the commit record and names (image id, shard, row), a prefix holds ONE population, a
shard's progress is countable from a listing, and the reader downloads each part once.

Hermetic: an in-memory object store standing in for R2. No network, no numpy required
(the numpy cross-checks skip without it).
"""

from __future__ import annotations

import io
import json
import struct

import pytest

from toolkit import vector_shards as vs

IDENTITY = {
    "model": "facebook/dinov3-vitb16-pretrain-lvd1689m",
    "revision": "5931719e67bbdb9737e363e781fb0c67687896bc",
    "library": "transformers",
    "pooling": "cls",
    "resolution": 768,
    "preprocessing": "letterbox_pad",
    "dtype": "bf16",
}


class FakeStore:
    """The three R2Client calls the module makes, over a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.types: dict[str, str] = {}
        self.uploads: list[str] = []
        self.downloads: list[str] = []

    def upload_bytes(self, key: str, data: bytes, content_type: str = "image/jpeg") -> None:
        self.objects[key] = bytes(data)
        self.types[key] = content_type
        self.uploads.append(key)

    def download_bytes(self, key: str) -> bytes:
        self.downloads.append(key)
        return self.objects[key]

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))


def _vectors(n: int, dim: int = 4, base: float = 0.0) -> list[list[float]]:
    # Values exactly representable in float16, so the round trip is exact.
    return [[base + (r * dim + c) / 64.0 for c in range(dim)] for r in range(n)]


# --- the codec -------------------------------------------------------------------------


def test_a_part_round_trips_exactly():
    ids = [10, 3, 99]
    rows = _vectors(3)
    part = vs.decode_part(vs.encode_part(ids, vs.pack_f16(rows), 4))
    assert part.image_ids == (10, 3, 99) and part.dim == 4
    assert [list(part.vector(i)) for i in range(3)] == rows


def test_float16_is_what_is_stored():
    part = vs.decode_part(vs.encode_part([1], vs.pack_f16([[0.1, -0.3333, 1.0, 0.0]]), 4))
    got = part.vector(0)
    assert got != (0.1, -0.3333, 1.0, 0.0)            # rounded to half precision ...
    assert got == pytest.approx((0.1, -0.3333, 1.0, 0.0), abs=5e-4)   # ... and only that


def test_numpy_reads_our_parts_and_we_read_numpys():
    np = pytest.importorskip("numpy")
    ids = [5, 6]
    rows = _vectors(2, dim=8)
    loaded = np.load(io.BytesIO(vs.encode_part(ids, vs.pack_f16(rows), 8)))
    assert loaded["image_id"].dtype == np.int64 and loaded["image_id"].tolist() == ids
    assert loaded["embedding"].dtype == np.float16
    assert loaded["embedding"].astype(float).tolist() == rows
    buf = io.BytesIO()
    np.savez(buf, image_id=np.array(ids, dtype="<i8"),
             embedding=np.array(rows, dtype="<f2"))
    part = vs.decode_part(buf.getvalue())
    assert part.image_ids == (5, 6) and list(part.vector(1)) == rows[1]


def test_a_part_refuses_bytes_that_do_not_match_its_ids():
    with pytest.raises(vs.ShardStoreError):
        vs.encode_part([1, 2], vs.pack_f16(_vectors(1)), 4)
    with pytest.raises(vs.ShardStoreError):
        vs.encode_part([], b"", 4)


def test_the_manifest_is_image_id_shard_row():
    data = vs.encode_manifest([7, 3], "parts/s0of1/x.npz")
    assert data.decode().splitlines() == ["image_id,shard,row", "7,parts/s0of1/x.npz,0",
                                          "3,parts/s0of1/x.npz,1"]
    assert vs.decode_manifest(data) == [(7, "parts/s0of1/x.npz", 0), (3, "parts/s0of1/x.npz", 1)]
    with pytest.raises(vs.ShardStoreError):
        vs.decode_manifest(b"id,part\n1,x\n")


# --- keys, identity, writer --------------------------------------------------------------


def test_the_default_prefix_is_one_per_encoder_identity():
    prefix = vs.default_prefix(IDENTITY)
    assert prefix == ("dinov3-vectors/dinov3-vitb16-pretrain-lvd1689m/"
                      "5931719e67bb-768-letterbox_pad-cls-bf16-transformers")
    assert vs.default_prefix({**IDENTITY, "resolution": 512}) != prefix
    assert vs.default_prefix({**IDENTITY, "revision": "b" * 40}) != prefix
    assert vs.default_prefix({**IDENTITY, "model": "x/y z;rm"}).split("/")[1] == "y_z_rm"


def test_a_prefix_holds_one_population():
    store = FakeStore()
    vs.ensure_identity(store, "p", IDENTITY)
    assert json.loads(store.objects["p/identity.json"]) == vs.canonical_identity(IDENTITY)
    vs.ensure_identity(store, "p", {**IDENTITY, "resolution": "768"})   # same facts: fine
    with pytest.raises(vs.ShardStoreError, match="different population"):
        vs.ensure_identity(store, "p", {**IDENTITY, "dtype": "fp32"})
    assert vs.read_identity(store, "q") is None


def test_the_writer_puts_the_part_then_commits_the_manifest():
    store = FakeStore()
    writer = vs.ShardWriter(store=store, prefix="p", shard=2, shards=8, dim=4)
    ids = [18, 26, 34]
    part = writer.put_part(ids, vs.pack_f16(_vectors(3)))
    assert part == "parts/s2of8/000000000018-000000000034-n3.npz"
    assert vs.load_manifest(store, "p") == {}       # uncommitted: nobody may read it yet
    key = writer.commit(ids, part)
    assert key == "p/manifest/s2of8/000000000018-000000000034-n3.csv"
    assert store.uploads == [f"p/{part}", key]
    assert store.types[f"p/{part}"] == "application/octet-stream"
    assert vs.load_manifest(store, "p") == {18: (part, 0), 26: (part, 1), 34: (part, 2)}
    assert (writer.parts, writer.rows) == (1, 3)


def test_a_shards_progress_is_counted_from_a_listing_alone():
    store = FakeStore()
    for shard in (0, 1):
        writer = vs.ShardWriter(store=store, prefix="p", shard=shard, shards=2, dim=4)
        for first in (0, 10):
            ids = [first + shard, first + shard + 2]
            writer.commit(ids, writer.put_part(ids, vs.pack_f16(_vectors(2))))
    store.downloads.clear()
    assert vs.count_parts(store, "p", shard=0, shards=2) == 2
    assert vs.count_parts(store, "p", shard=1, shards=2) == 2
    assert vs.count_parts(store, "p", shard=0, shards=4) == 0
    assert store.downloads == []
    assert sorted(vs.load_manifest(store, "p")) == [0, 1, 2, 3, 10, 11, 12, 13]


def test_the_reader_serves_vectors_by_id_and_downloads_each_part_once():
    store = FakeStore()
    writer = vs.ShardWriter(store=store, prefix="p", shard=0, shards=1, dim=4)
    a, b = _vectors(2, base=0.0), _vectors(2, base=1.0)
    writer.commit([1, 5], writer.put_part([1, 5], vs.pack_f16(a)))
    writer.commit([9, 12], writer.put_part([9, 12], vs.pack_f16(b)))
    reader = vs.ShardVectorReader(store, "p")
    assert reader.ids() == [1, 5, 9, 12]
    assert reader.ids_after(None, 2) == [1, 5] and reader.ids_after(5, 10) == [9, 12]
    got = reader([12, 1, 404])
    assert got == {12: tuple(b[1]), 1: tuple(a[0])}
    reader([5, 9])
    assert reader.downloads == 2


def test_the_reader_evicts_past_its_cache():
    store = FakeStore()
    writer = vs.ShardWriter(store=store, prefix="p", shard=0, shards=1, dim=4)
    for i in range(3):
        writer.commit([i], writer.put_part([i], vs.pack_f16(_vectors(1, base=i))))
    reader = vs.ShardVectorReader(store, "p", cache_parts=1)
    reader([0]), reader([1]), reader([0])
    assert reader.downloads == 3


def test_pack_f16_is_little_endian_ieee_half():
    assert vs.pack_f16([[1.0, -2.0]]) == struct.pack("<2e", 1.0, -2.0) == b"\x00\x3c\x00\xc0"
