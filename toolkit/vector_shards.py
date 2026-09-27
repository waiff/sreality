"""Embedding vectors kept as float16 shard files in object storage (R2), with a manifest.

WHY THIS EXISTS (G4, 2026-09-27). The head-scoring pass needs the vectors KEPT — a later head
version re-scores from them without re-embedding — but the engine reads only the scores, so
at measurement scale (1.21 M images) the vectors need not sit in Postgres: 1.21 M x 2,412 B a
row is ~2.9 GB on a disk that cannot shrink, against ~1.9 GB of float16 here.

LAYOUT under one prefix (by default one per encoder identity, so every scope and every
re-dispatch of the same population shares one store and resumes from it):

    <prefix>/identity.json                              the seven encoder facts; a pass under
                                                        any other identity is refused
    <prefix>/parts/s<k>of<n>/<first>-<last>-n<N>.npz    image_id (int64, N) + embedding (float16, N x dim)
    <prefix>/manifest/s<k>of<n>/<same stem>.csv         image_id,shard,row — one line per vector

The manifest line is the COMMIT RECORD: the writer uploads the part, then (optionally) the
scores, then the manifest, so a manifest entry always names a part that exists, and an image
is pending exactly when no manifest line names it. `s<k>of<n>` lets a watchdog count one
shard's progress without reading anything.

STDLIB ONLY. The .npz is numpy's own format (a stored zip of .npy v1.0 arrays), written and
read here with zipfile + struct, so the scoring path stays free of any ML library while
`numpy.load` still opens the files.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import re
import struct
import zipfile
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence

ROOT = "dinov3-vectors"
PARTS = "parts"
MANIFEST = "manifest"
IDENTITY_FILE = "identity.json"
MANIFEST_HEADER = ("image_id", "shard", "row")
IDENTITY_KEYS = ("model", "revision", "library", "pooling", "resolution", "preprocessing",
                 "dtype")

_NPY_MAGIC = b"\x93NUMPY"
_UNSAFE = re.compile(r"[^A-Za-z0-9._/-]")


class ShardStoreError(RuntimeError):
    """A shard store that is malformed, or that holds a different population."""


class ObjectStore(Protocol):
    """The three calls this module makes; `scraper.image_storage.R2Client` provides them."""

    def upload_bytes(self, key: str, data: bytes, content_type: str = ...) -> None: ...

    def download_bytes(self, key: str) -> bytes: ...

    def list_keys(self, prefix: str) -> list[str]: ...


def default_prefix(identity: Mapping[str, Any]) -> str:
    """`dinov3-vectors/<model>/<rev12>-<res>-<prep>-<pooling>-<dtype>-<library>`."""
    model = str(identity["model"]).rsplit("/", 1)[-1]
    tail = "-".join([str(identity["revision"])[:12], str(int(identity["resolution"])),
                     str(identity["preprocessing"]), str(identity["pooling"]),
                     str(identity["dtype"]), str(identity["library"])])
    return _UNSAFE.sub("_", f"{ROOT}/{model}/{tail}")


def canonical_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (int(identity[k]) if k == "resolution" else str(identity[k]))
            for k in IDENTITY_KEYS}


# --- the .npy / .npz codec ---------------------------------------------------------------

def _npy(descr: str, shape: tuple[int, ...], raw: bytes) -> bytes:
    header = "{'descr': '%s', 'fortran_order': False, 'shape': %r, }" % (descr, shape)
    header += " " * ((64 - (10 + len(header) + 1) % 64) % 64) + "\n"
    return _NPY_MAGIC + b"\x01\x00" + struct.pack("<H", len(header)) + header.encode("latin1") + raw


def _read_npy(data: bytes) -> tuple[str, tuple[int, ...], bytes]:
    if data[:6] != _NPY_MAGIC:
        raise ShardStoreError("not a .npy array")
    if data[6] == 1:
        (hlen,), start = struct.unpack_from("<H", data, 8), 10
    elif data[6] in (2, 3):
        (hlen,), start = struct.unpack_from("<I", data, 8), 12
    else:
        raise ShardStoreError(f"unsupported .npy version {data[6]}")
    header = ast.literal_eval(data[start:start + hlen].decode("latin1"))
    if header.get("fortran_order"):
        raise ShardStoreError("fortran-ordered arrays are not written here")
    return str(header["descr"]), tuple(int(d) for d in header["shape"]), data[start + hlen:]


def pack_f16(rows: Iterable[Sequence[float]]) -> bytes:
    """Row-major little-endian float16 bytes (IEEE half, round-to-nearest-even)."""
    return b"".join(struct.pack(f"<{len(r)}e", *r) for r in rows)


def encode_part(image_ids: Sequence[int], f16: bytes, dim: int) -> bytes:
    """One shard file: `image_id` int64 (N,) and `embedding` float16 (N, dim), numpy-loadable."""
    n = len(image_ids)
    if n == 0 or len(f16) != n * dim * 2:
        raise ShardStoreError(f"{len(f16)} bytes of float16 for {n} vectors of {dim} dims")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("image_id.npy", _npy("<i8", (n,), struct.pack(f"<{n}q", *image_ids)))
        zf.writestr("embedding.npy", _npy("<f2", (n, dim), f16))
    return buf.getvalue()


@dataclass(frozen=True)
class Part:
    """A decoded shard file; vectors stay packed until a row is asked for."""
    image_ids: tuple[int, ...]
    dim: int
    raw: bytes

    def vector(self, row: int) -> tuple[float, ...]:
        return struct.unpack_from(f"<{self.dim}e", self.raw, row * self.dim * 2)


def decode_part(data: bytes) -> Part:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        id_descr, id_shape, id_raw = _read_npy(zf.read("image_id.npy"))
        v_descr, v_shape, v_raw = _read_npy(zf.read("embedding.npy"))
    if id_descr != "<i8" or v_descr != "<f2" or len(v_shape) != 2 or id_shape != (v_shape[0],):
        raise ShardStoreError(f"unexpected arrays {id_descr}{id_shape} / {v_descr}{v_shape}")
    n, dim = v_shape
    return Part(image_ids=struct.unpack(f"<{n}q", id_raw[:8 * n]), dim=dim,
                raw=bytes(v_raw[:n * dim * 2]))


# --- keys and the manifest ---------------------------------------------------------------

def shard_dir(shard: int, shards: int) -> str:
    return f"s{int(shard)}of{int(shards)}"


def part_stem(image_ids: Sequence[int]) -> str:
    return f"{min(image_ids):012d}-{max(image_ids):012d}-n{len(image_ids)}"


def encode_manifest(image_ids: Sequence[int], part: str) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(MANIFEST_HEADER)
    writer.writerows((int(i), part, row) for row, i in enumerate(image_ids))
    return out.getvalue().encode()


def decode_manifest(data: bytes) -> list[tuple[int, str, int]]:
    rows = list(csv.reader(io.StringIO(data.decode())))
    if not rows or tuple(rows[0]) != MANIFEST_HEADER:
        raise ShardStoreError("a manifest part must start with image_id,shard,row")
    return [(int(r[0]), r[1], int(r[2])) for r in rows[1:] if r]


def read_identity(store: ObjectStore, prefix: str) -> dict[str, Any] | None:
    key = f"{prefix}/{IDENTITY_FILE}"
    if key not in store.list_keys(key):
        return None
    return json.loads(store.download_bytes(key).decode())


def ensure_identity(store: ObjectStore, prefix: str, identity: Mapping[str, Any]) -> None:
    """Stamp the prefix with its population, or refuse a pass that would mix two."""
    wanted = canonical_identity(identity)
    stored = read_identity(store, prefix)
    if stored is None:
        store.upload_bytes(f"{prefix}/{IDENTITY_FILE}",
                           json.dumps(wanted, sort_keys=True).encode(), "application/json")
    elif canonical_identity(stored) != wanted:
        raise ShardStoreError(f"{prefix} holds vectors of {stored}, this pass writes {wanted}: "
                              "a different population needs its own prefix")


def load_manifest(store: ObjectStore, prefix: str) -> dict[int, tuple[str, int]]:
    """{image_id: (part path relative to prefix, row)} over every committed part."""
    out: dict[int, tuple[str, int]] = {}
    for key in sorted(store.list_keys(f"{prefix}/{MANIFEST}/")):
        if key.endswith(".csv"):
            for image_id, part, row in decode_manifest(store.download_bytes(key)):
                out[image_id] = (part, row)
    return out


def count_parts(store: ObjectStore, prefix: str, *, shard: int, shards: int) -> int:
    """Committed parts of one shard: the watchdog's progress marker (a listing, no reads)."""
    return sum(1 for k in store.list_keys(f"{prefix}/{MANIFEST}/{shard_dir(shard, shards)}/")
               if k.endswith(".csv"))


# --- the writer and the reader -------------------------------------------------------------

@dataclass
class ShardWriter:
    """Two-phase: `put_part` uploads the vectors, `commit` uploads the manifest that names
    them. Whatever the caller does in between (the head scores) is covered by the commit."""
    store: ObjectStore
    prefix: str
    shard: int
    shards: int
    dim: int
    parts: int = 0
    rows: int = 0

    def put_part(self, image_ids: Sequence[int], f16: bytes) -> str:
        part = f"{PARTS}/{shard_dir(self.shard, self.shards)}/{part_stem(image_ids)}.npz"
        self.store.upload_bytes(f"{self.prefix}/{part}", encode_part(image_ids, f16, self.dim),
                                "application/octet-stream")
        return part

    def commit(self, image_ids: Sequence[int], part: str) -> str:
        stem = part.rsplit("/", 1)[-1][:-len(".npz")]
        key = f"{self.prefix}/{MANIFEST}/{shard_dir(self.shard, self.shards)}/{stem}.csv"
        self.store.upload_bytes(key, encode_manifest(image_ids, part), "text/csv")
        self.parts += 1
        self.rows += len(image_ids)
        return key


@dataclass
class ShardVectorReader:
    """{image_id: vector} for any ids the manifest holds, downloading each part once while
    it stays among the `cache_parts` most recently used (packed bytes, ~3 MB a part)."""
    store: ObjectStore
    prefix: str
    manifest: dict[int, tuple[str, int]] | None = None
    cache_parts: int = 64
    downloads: int = 0
    _cache: OrderedDict = field(default_factory=OrderedDict)

    def __post_init__(self) -> None:
        if self.manifest is None:
            self.manifest = load_manifest(self.store, self.prefix)
        self._ids = sorted(self.manifest)

    def ids(self) -> list[int]:
        return list(self._ids)

    def ids_after(self, after: int | None, limit: int) -> list[int]:
        start = 0 if after is None else bisect_right(self._ids, int(after))
        return self._ids[start:start + max(1, int(limit))]

    def _part(self, part: str) -> Part:
        if part in self._cache:
            self._cache.move_to_end(part)
            return self._cache[part]
        decoded = decode_part(self.store.download_bytes(f"{self.prefix}/{part}"))
        self.downloads += 1
        self._cache[part] = decoded
        while len(self._cache) > max(1, self.cache_parts):
            self._cache.popitem(last=False)
        return decoded

    def __call__(self, image_ids: Sequence[int]) -> dict[int, tuple[float, ...]]:
        assert self.manifest is not None
        out: dict[int, tuple[float, ...]] = {}
        for image_id in image_ids:
            hit = self.manifest.get(int(image_id))
            if hit is not None:
                out[int(image_id)] = self._part(hit[0]).vector(hit[1])
        return out
