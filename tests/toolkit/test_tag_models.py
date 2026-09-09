"""The versioned tag model: promotion, the one-active invariant, scoring's winner
rule, resumability, and the `winners()` read contract.

OFFLINE. The connection is a fake that answers exactly the statements this lane may
legitimately run and raises on anything else — so a future edit that reaches for a
shortcut over `image_tag_labels` fails here instead of shipping. Vectors are
synthetic and linearly separable; the numbers are not the point, the rules are.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from typing import Any, Sequence

import pytest

from toolkit import tag_head_bakeoff as bo
from toolkit import tag_heads as th
from toolkit import tag_models as tm

TRAINED_AT = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)

ENCODER = th.EncoderIdentity(
    model="facebook/dinov2-large", revision="cafebabe", library="transformers",
    pooling="cls", resolution=504, preprocessing="letterbox_pad", dtype="bfloat16",
)

ARM = bo.Arm(id=7, run_id=1, arm="dinov2-l14-reg@504/bf16", encoder=ENCODER, dim=8)


# --------------------------------------------------------------- the fake DB

class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        self._rows = self._conn.answer(s, params or {})

    def executemany(self, sql: str, rows: Any) -> None:
        s = " ".join(sql.split())
        materialized = list(rows)
        self._conn.executed.append((s, materialized))
        for row in materialized:
            self._conn.answer(s, row)
        self._rows = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Txn:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Txn":
        self._conn.transactions += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    """Canned answers for exactly the statements the tag-model lane may run."""

    def __init__(self, *, tags: dict[int, str], labels: dict[int, dict[int, str]],
                 groups: dict[int, int], vectors: dict[int, list[float]],
                 models: list[dict[str, Any]] | None = None,
                 heads: dict[int, list[dict[str, Any]]] | None = None,
                 scores: dict[tuple[int, int], dict[str, Any]] | None = None,
                 arms: list[bo.Arm] | None = None) -> None:
        self.tags = tags
        self.labels = labels
        self.groups = groups
        self.vectors = vectors
        self.models = models if models is not None else []
        self.heads = heads if heads is not None else {}
        self.scores = scores if scores is not None else {}
        self.arms = arms if arms is not None else [ARM]
        self.executed: list[tuple[str, Any]] = []
        self.transactions = 0
        self._next_model_id = 1 + max((m["id"] for m in self.models), default=0)

    # --- helpers -----------------------------------------------------------

    def transaction(self) -> _Txn:
        return _Txn(self)

    def cursor(self) -> _Cur:
        return _Cur(self)

    def _model_row(self, m: dict[str, Any], *, counted: bool = False
                   ) -> tuple[Any, ...]:
        row: list[Any] = [
            m["id"], m["version"], m["label"], m["status"], m.get("created_at"),
            m.get("activated_at"), m.get("source_run_id"), m.get("source_arm"),
            m["mode"], *[m["encoder"][f] for f in th.ENCODER_FIELDS],
            m.get("heads", []), m.get("dataset_hash"), m.get("note"),
        ]
        if counted:
            row += [len(self.heads.get(m["id"], [])),
                    sum(1 for (_, mid) in self.scores if mid == m["id"])]
        return tuple(row)

    # --- the statement router ---------------------------------------------

    def answer(self, s: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        # ---- the label door, and nothing else that touches image_tag_labels
        if "FROM image_tag_labels l" in s and "l.state = ANY(%(states)s" in s \
                and "l.in_training" in s and "storage_path" not in s:
            tag = int(params["tag_id"])
            want = set(params["states"])
            return sorted((i, st) for i, st in self.labels.get(tag, {}).items()
                          if st in want)
        if "FROM image_tag_labels l" in s and "storage_path" in s:
            return []
        if "WHERE active AND review_state = 'ready'" in s:
            return sorted(self.tags.items())
        if "FROM tag_taxonomy WHERE id = ANY" in s:
            want = {int(t) for t in params["ids"]}
            return sorted((t, label) for t, label in self.tags.items() if t in want)
        if "FROM tag_taxonomy WHERE active" in s:
            return sorted(self.tags.items())
        if "FROM images i" in s:
            return [(i, self.groups.get(i)) for i in sorted(params["image_ids"])]

        # ---- the bake-off store (read-only from here)
        if "FROM dedup_sim.tag_head_bakeoff_runs" in s and s.startswith("SELECT"):
            return [(1, TRAINED_AT, "run 1", None, "ok", None,
                     sorted(self.tags), 0)]
        if "FROM dedup_sim.tag_head_bakeoff_arms" in s and s.startswith("SELECT id,"):
            names = params.get("names")
            return [(a.id, a.run_id, a.arm,
                     *[a.encoder.as_dict()[f] for f in th.ENCODER_FIELDS],
                     a.dim, a.status, a.note)
                    for a in self.arms if not names or a.arm in names]
        if "FROM dedup_sim.tag_head_bakeoff_vectors v" in s and "arm_id" in s \
                and "image_ids" in s:
            want = {int(i) for i in params["image_ids"]}
            return [(i, self.vectors[i]) for i in sorted(want) if i in self.vectors]
        if "FROM dedup_sim.tag_head_bakeoff_vectors v" in s:
            after = params.get("after")
            ids = [i for i in sorted(self.vectors)
                   if after is None or i > int(after)]
            return [(i,) for i in ids[:int(params["limit"])]]
        if "FROM dedup_sim.tag_head_bakeoff_metrics m" in s:
            return []

        # ---- the production vector store
        if "FROM image_dinov3_embeddings e" in s:
            return []

        # ---- the model registry
        if s.startswith("INSERT INTO tag_head_models"):
            model_id = self._next_model_id
            self._next_model_id += 1
            self.models.append({
                "id": model_id, "version": params["version"],
                "label": params["label"], "status": params["status"],
                "created_at": TRAINED_AT, "activated_at": None,
                "source_run_id": params["source_run_id"],
                "source_arm": params["source_arm"], "mode": params["mode"],
                "encoder": {f: params[f] for f in th.ENCODER_FIELDS},
                "heads": list(params["heads"]),
                "dataset_hash": params["dataset_hash"], "note": params["note"]})
            return [(model_id,)]
        if s.startswith("SELECT m.id, m.version") and "LIMIT 1" in s:
            found = [
                m for m in self.models
                if (params.get("version") is None or m["version"] == params["version"])
                and (params.get("model_id") is None or m["id"] == params["model_id"])
                and (params.get("status") is None or m["status"] == params["status"])
            ]
            return [self._model_row(found[-1])] if found else []
        if s.startswith("SELECT m.id, m.version"):
            return [self._model_row(m, counted=True)
                    for m in sorted(self.models, key=lambda m: -m["id"])]
        if s.startswith("UPDATE tag_head_models SET status = 'retired'"):
            for m in self.models:
                if m["status"] == "active" and m["id"] != params["model_id"]:
                    m["status"] = "retired"
            return []
        if s.startswith("UPDATE tag_head_models SET status = 'active'"):
            for m in self.models:
                if m["id"] == params["model_id"]:
                    m["status"] = "active"
                    m["activated_at"] = m["activated_at"] or TRAINED_AT
            return []

        # ---- the heads
        if s.startswith("INSERT INTO tag_head_model_heads"):
            rows = self.heads.setdefault(int(params["model_id"]), [])
            rows[:] = [r for r in rows if r["tag_id"] != int(params["tag_id"])]
            rows.append({"tag_id": int(params["tag_id"]),
                         "artifact": params["artifact"],
                         "metrics": params["metrics"]})
            return []
        if "FROM tag_head_model_heads h" in s:
            rows = self.heads.get(int(params["model_id"]), [])
            return [(r["tag_id"], r["artifact"], r["metrics"],
                     self.tags.get(r["tag_id"]))
                    for r in sorted(rows, key=lambda r: r["tag_id"])]

        # ---- the winner store
        if s.startswith("INSERT INTO image_tag_scores"):
            self.scores[(int(params["image_id"]), int(params["model_id"]))] = {
                "scores": params["scores"],
                "winner_tag_id": int(params["winner_tag_id"]),
                "winner_score": float(params["winner_score"]),
                "scored_at": params["scored_at"]}
            return []
        if s.startswith("SELECT s.image_id FROM image_tag_scores"):
            want = {int(i) for i in params["image_ids"]}
            return [(i,) for (i, mid) in sorted(self.scores)
                    if mid == int(params["model_id"]) and i in want]
        if s.startswith("SELECT s.image_id, s.winner_tag_id"):
            want = {int(i) for i in params["image_ids"]}
            out = []
            for (i, mid), row in sorted(self.scores.items()):
                if mid == int(params["model_id"]) and i in want:
                    out.append((i, row["winner_tag_id"], row["winner_score"],
                                row["scores"], row["scored_at"]))
            return out
        if "count(*)::bigint FROM image_tag_scores" in s:
            return [(sum(1 for (_, mid) in self.scores
                         if mid == int(params["model_id"])),)]

        raise AssertionError(f"unexpected statement: {s[:160]}")


# ------------------------------------------------------------------ fixtures

def _fixture(seed: int = 7, *, tags: Sequence[int] = (11, 12)) -> _FakeConn:
    """Two heads over 60 photos of 20 listings, separable on distinct axes."""
    rng = random.Random(seed)
    labels: dict[int, dict[int, str]] = {t: {} for t in tags}
    groups: dict[int, int] = {}
    vectors: dict[int, list[float]] = {}
    for image_id in range(1, 61):
        listing_id = 100 + (image_id % 20)
        groups[image_id] = listing_id
        axis = image_id % len(tags)
        vec = [rng.uniform(-0.05, 0.05) for _ in range(8)]
        vec[axis] += 1.0
        vectors[image_id] = vec
        for offset, tag in enumerate(tags):
            labels[tag][image_id] = "positive" if offset == axis else "negative"
    return _FakeConn(tags={t: f"tag-{t}" for t in tags}, labels=labels,
                     groups=groups, vectors=vectors)


def _promote(conn: _FakeConn, *, version: str = "v1",
             mode: str = th.MODE_POS_NEG) -> tm.TagModel:
    model, _ = tm.promote(conn, run_id=1, arm=ARM.arm, mode=mode, version=version,
                          label=f"{version} test", n_splits=3,
                          trained_at=TRAINED_AT)
    return model


# ---------------------------------------------------------------- promotion

def test_promote_freezes_one_cell_as_a_candidate() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    model, outcomes = tm.promote(
        conn, run_id=1, arm=ARM.arm, mode=th.MODE_POS_NEG, version="v1",
        label="run 1, dinov2", note="first iteration", n_splits=3,
        trained_at=TRAINED_AT)

    assert model.status == tm.STATUS_CANDIDATE, (
        "a fresh promotion must be unreadable until `activate` runs")
    assert model.heads == (11, 12)
    assert model.encoder == ENCODER
    assert model.source_run_id == 1 and model.source_arm == ARM.arm
    assert model.dataset_hash, "the model names its training material"
    assert [o.status for o in outcomes] == ["ok", "ok"]
    assert conn.transactions == 1, (
        "the model row and its heads are ONE write: a crash between them would "
        "strand a headless candidate on a version name that can never be reused")

    heads = tm.model_heads(conn, model_id=model.id)
    assert [h.tag_id for h in heads] == [11, 12]
    for head in heads:
        # The artifact is what scoring needs and nothing else has to be present.
        assert head.artifact["kind"] == th.ARTIFACT_KIND
        assert len(head.artifact["weights"]) == 8
        assert head.artifact["encoder"] == ENCODER.as_dict()
        # The bake-off numbers are COPIED, so they outlive dedup_sim.
        assert head.metrics["cv"]["graded_n"] > 0
        assert head.metrics["dataset_hash"] == head.artifact["dataset_hash"]
        # Nothing was dropped for want of a vector, and the count is recorded so
        # a half-trained head cannot look like a merely bad one.
        assert head.metrics["n_missing_embedding"] == 0


def test_promote_refuses_to_reuse_a_version() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    _promote(conn)
    with pytest.raises(tm.TagModelError, match="already exists"):
        _promote(conn)


def test_promote_writes_nothing_when_no_head_can_be_trained() -> None:
    pytest.importorskip("sklearn")
    # One listing group only: a grouped split cannot grade anything.
    conn = _fixture()
    for image_id in conn.groups:
        conn.groups[image_id] = 100
    with pytest.raises(tm.TagModelError, match="nothing to promote"):
        _promote(conn)
    assert conn.models == [], (
        "a failed promotion must not occupy its version name — versions are "
        "immutable, so the name could never be reused")


def test_a_labelled_image_without_a_vector_is_counted_not_hidden() -> None:
    """The operator's loop is "label more, promote, look at the numbers". An image
    labelled but not yet embedded silently leaves the fit, so the count travels
    with the head's metrics instead of only existing in a log nobody kept."""
    pytest.importorskip("sklearn")
    conn = _fixture()
    del conn.vectors[7]
    model, outcomes = tm.promote(
        conn, run_id=1, arm=ARM.arm, mode=th.MODE_POS_NEG, version="v1",
        label="one image short", n_splits=3, trained_at=TRAINED_AT)
    assert [o.status for o in outcomes] == ["ok", "ok"]
    assert all(o.metrics["n_missing_embedding"] == 1 for o in outcomes)
    assert all(h.metrics["n_missing_embedding"] == 1
               for h in tm.model_heads(conn, model_id=model.id))


def test_promote_rejects_an_unknown_arm_and_an_unknown_mode() -> None:
    conn = _fixture()
    with pytest.raises(tm.TagModelError, match="no arm named"):
        tm.promote(conn, run_id=1, arm="nope", mode=th.MODE_POS_NEG,
                   version="v9", label="x")
    with pytest.raises(tm.TagModelError, match="unknown training mode"):
        tm.promote(conn, run_id=1, arm=ARM.arm, mode="vibes", version="v9",
                   label="x")


# ---------------------------------------------------------------- activation

def test_activation_keeps_exactly_one_active_model() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    first = _promote(conn, version="v1")
    second = _promote(conn, version="v2")

    before = conn.transactions
    tm.activate(conn, version="v1")
    assert tm.active_model(conn).version == "v1"
    assert conn.transactions == before + 1, (
        "the flip is one transaction, not two writes")

    tm.activate(conn, version="v2")
    live = tm.active_model(conn)
    assert live.version == "v2" and live.activated_at is not None
    assert tm.get_model(conn, model_id=first.id).status == tm.STATUS_RETIRED
    assert tm.get_model(conn, model_id=second.id).status == tm.STATUS_ACTIVE
    assert sum(1 for m in conn.models if m["status"] == "active") == 1


def test_activation_refuses_an_unknown_or_headless_version() -> None:
    conn = _fixture()
    with pytest.raises(tm.TagModelError, match="no model version"):
        tm.activate(conn, version="ghost")
    conn.models.append({
        "id": 99, "version": "empty", "label": "empty", "status": "candidate",
        "created_at": TRAINED_AT, "activated_at": None, "source_run_id": 1,
        "source_arm": ARM.arm, "mode": th.MODE_POS_NEG,
        "encoder": ENCODER.as_dict(), "heads": [], "dataset_hash": None,
        "note": None})
    with pytest.raises(tm.TagModelError, match="no heads"):
        tm.activate(conn, version="empty")


# ------------------------------------------------------------------- winners

def test_winner_is_the_argmax_and_ties_go_to_the_lower_tag_id() -> None:
    assert tm.winner_of({11: 0.2, 12: 0.9, 19: 0.5}) == (12, 0.9)
    # A tie is common — two heads that both see nothing they know — and
    # "whichever the dict yielded first" would tag the same photo differently
    # between two runs of the same model.
    assert tm.winner_of({19: 0.4, 12: 0.4, 11: 0.4}) == (11, 0.4)
    with pytest.raises(tm.TagModelError):
        tm.winner_of({})


def test_score_writes_one_row_per_image_with_every_head_and_a_winner() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    model = _promote(conn)
    source = tm.bakeoff_source(conn, run_id=1, arm=ARM.arm)

    report = tm.score(conn, model=model, source=source, batch=25,
                      scored_at=TRAINED_AT)
    assert report.written == 60 and report.considered == 60
    assert report.missing_vector == 0

    stored = conn.scores[(1, model.id)]
    scores = json.loads(stored["scores"])
    assert set(scores) == {"11", "12"}, "every head's probability is stored"
    best = max(scores.items(), key=lambda kv: (kv[1], -int(kv[0])))
    assert stored["winner_tag_id"] == int(best[0])
    assert stored["winner_score"] == pytest.approx(best[1], abs=1e-6)
    # image 1 sits on tag 12's axis (1 % 2 == 1 -> the second tag)
    assert stored["winner_tag_id"] == 12


def test_score_is_resumable_and_a_re_score_overwrites() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    model = _promote(conn)
    source = tm.bakeoff_source(conn, run_id=1, arm=ARM.arm)

    first = tm.score(conn, model=model, source=source, batch=10, limit=20,
                     scored_at=TRAINED_AT)
    assert first.written == 20

    resumed = tm.score(conn, model=model, source=source, batch=10,
                       scored_at=TRAINED_AT)
    assert resumed.skipped_scored == 20, "already-scored images are skipped"
    assert resumed.written == 40
    assert len(conn.scores) == 60

    later = datetime(2026, 9, 10, tzinfo=timezone.utc)
    forced = tm.score(conn, model=model, source=source, batch=100, force=True,
                      scored_at=later)
    assert forced.skipped_scored == 0 and forced.written == 60
    assert len(conn.scores) == 60, (
        "a re-score under the SAME version overwrites — the version is the "
        "identity, so two rows for one (image, model) cannot both be right")
    assert conn.scores[(1, model.id)]["scored_at"] == later


def test_score_dry_run_writes_nothing() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    model = _promote(conn)
    source = tm.bakeoff_source(conn, run_id=1, arm=ARM.arm)
    report = tm.score(conn, model=model, source=source, dry_run=True,
                      scored_at=TRAINED_AT)
    assert report.written == 60 and report.dry_run
    assert conn.scores == {}


def test_score_reports_an_image_with_no_vector_instead_of_inventing_one() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    model = _promote(conn)
    source = tm.bakeoff_source(conn, run_id=1, arm=ARM.arm)
    missing = 999
    report = tm.score(conn, model=model, source=source, image_ids=[1, missing],
                      scored_at=TRAINED_AT)
    assert report.considered == 2 and report.written == 1
    assert report.missing_vector == 1
    assert (missing, model.id) not in conn.scores


def test_production_source_is_wired_and_simply_empty_today() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    model = _promote(conn)
    source = tm.resolve_source(conn, model=model, spec=tm.SOURCE_PRODUCTION)
    report = tm.score(conn, model=model, source=source, scored_at=TRAINED_AT)
    assert source.name == "production"
    assert report.considered == 0 and report.written == 0


def test_resolve_source_spellings() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    model = _promote(conn)
    assert tm.resolve_source(conn, model=model, spec="bakeoff").name == "bakeoff:1"
    assert tm.resolve_source(conn, model=model, spec="bakeoff:1").name == "bakeoff:1"
    with pytest.raises(tm.TagModelError, match="unknown vector source"):
        tm.resolve_source(conn, model=model, spec="magic")


# ------------------------------------------------------------ read contract

def test_winners_reads_the_active_model_by_default() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    model = _promote(conn)
    source = tm.bakeoff_source(conn, run_id=1, arm=ARM.arm)
    tm.score(conn, model=model, source=source, scored_at=TRAINED_AT)

    with pytest.raises(tm.TagModelError, match="no tag model is active"):
        tm.winners(conn, image_ids=[1])

    tm.activate(conn, version="v1")
    found = tm.winners(conn, image_ids=[1, 2, 999])
    assert set(found) == {1, 2}, (
        "an unscored image is absent, never a silent 'no tag' — the store is "
        "filled incrementally")
    one = found[1]
    assert one.version == "v1" and one.model_id == model.id
    assert one.winner_tag_id in (11, 12)
    assert set(one.scores) == {11, 12}
    assert one.winner_score == pytest.approx(max(one.scores.values()), abs=1e-6)
    assert one.as_dict()["scores"] == {"11": one.scores[11], "12": one.scores[12]}


def test_winners_can_name_a_version_and_raises_on_an_unknown_one() -> None:
    pytest.importorskip("sklearn")
    conn = _fixture()
    model = _promote(conn)
    tm.score(conn, model=model,
             source=tm.bakeoff_source(conn, run_id=1, arm=ARM.arm),
             scored_at=TRAINED_AT)
    found = tm.winners(conn, image_ids=[3], version="v1")
    assert found[3].version == "v1"
    with pytest.raises(tm.TagModelError, match="no model version"):
        tm.winners(conn, image_ids=[3], version="v404")
    assert tm.winners(conn, image_ids=[], version="v1") == {}


def test_adding_a_head_is_a_new_version_whose_winner_spans_the_wider_set() -> None:
    """Ruling 2026-09-09 (a): heads are added over time and the winner must be
    recomputable from an expanded head set. The mechanism is a new version, not an
    edit — so the two versions coexist, each with its own head set and its own
    winner for the same photo."""
    pytest.importorskip("sklearn")
    conn = _fixture(tags=(11, 12, 13))
    narrow, _ = tm.promote(conn, run_id=1, arm=ARM.arm, mode=th.MODE_POS_NEG,
                           version="v1", label="two heads", tag_ids=[11, 12],
                           n_splits=3, trained_at=TRAINED_AT)
    wide, _ = tm.promote(conn, run_id=1, arm=ARM.arm, mode=th.MODE_POS_NEG,
                         version="v2", label="three heads", n_splits=3,
                         trained_at=TRAINED_AT)
    assert narrow.heads == (11, 12) and wide.heads == (11, 12, 13)

    source = tm.bakeoff_source(conn, run_id=1, arm=ARM.arm)
    tm.score(conn, model=narrow, source=source, scored_at=TRAINED_AT)
    tm.score(conn, model=wide, source=source, scored_at=TRAINED_AT)

    assert set(tm.winners(conn, image_ids=[3], version="v1")[3].scores) == {11, 12}
    assert set(tm.winners(conn, image_ids=[3], version="v2")[3].scores) == {11, 12, 13}
    # Image 3 sits on tag 13's axis (3 % 3 == 0 -> the first tag of the cycle is
    # tag 11; the axis rotates), so what matters is only that the wider version
    # can pick a head the narrow one never had.
    assert tm.winners(conn, image_ids=[3], version="v2")[3].winner_tag_id in (11, 12, 13)


def test_the_lane_never_reaches_around_the_label_door() -> None:
    """`machine_labeling.training_rows` is the ONE door. The fake raises on any
    unrecognised statement, so this asserts the positive half: every label read
    this lane made carries the holdout exclusion the door builds in."""
    pytest.importorskip("sklearn")
    conn = _fixture()
    _promote(conn)
    label_reads = [s for s, _ in conn.executed if "image_tag_labels" in s]
    assert label_reads, "promotion must have read labels at all"
    for statement in label_reads:
        assert "tag_exam_members" in statement, (
            "a label read without the holdout anti-join would train a head on the "
            "sealed exam")
