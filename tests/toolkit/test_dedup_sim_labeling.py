"""Taxonomy/sample/proposal CRUD for the NEW DEDUP Labeling program.
Hermetic fake conn — no DB (migration 373 is verified separately, live)."""

from __future__ import annotations

from typing import Any

import pytest

from toolkit import dedup_sim_labeling as dsl


# --- fake conn: in-memory tables + a tiny SQL dispatcher ---------------------


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        c = self._conn

        # confirm_proposal's register-a-correction insert: 2 params, and a
        # duplicate is a silent no-op rather than a UniqueViolation.
        if s.startswith("INSERT INTO dedup_sim.taxonomy_labels") and "ON CONFLICT" in s:
            label, created_by = params
            if not any(t["label"] == label for t in c.taxonomy.values()):
                c.next_taxonomy_id += 1
                c.taxonomy[c.next_taxonomy_id] = {
                    "id": c.next_taxonomy_id, "label": label, "family": None,
                    "active": True, "created_at": "2026-08-06T00:00:00Z",
                }
            self._rows = []

        elif s.startswith("INSERT INTO dedup_sim.taxonomy_labels"):
            label, family, created_by = params
            if any(t["label"] == label for t in c.taxonomy.values()):
                raise c.UniqueViolation(f"duplicate label {label!r}")
            c.next_taxonomy_id += 1
            row = {
                "id": c.next_taxonomy_id, "label": label, "family": family,
                "active": True, "created_at": "2026-08-06T00:00:00Z",
            }
            c.taxonomy[row["id"]] = row
            self._rows = [(row["id"], row["label"], row["family"], row["active"], row["created_at"])]

        elif s.startswith("SELECT label FROM dedup_sim.taxonomy_labels WHERE id"):
            (label_id,) = params
            row = c.taxonomy.get(label_id)
            self._rows = [(row["label"],)] if row else []

        elif s.startswith("UPDATE dedup_sim.taxonomy_labels SET label"):
            new_label, label_id = params
            if any(t["label"] == new_label for lid, t in c.taxonomy.items() if lid != label_id):
                raise c.UniqueViolation(f"duplicate label {new_label!r}")
            c.taxonomy[label_id]["label"] = new_label
            self.rowcount = 1

        elif s.startswith("SELECT id, label, family, active, created_at"):
            (label_id,) = params
            row = c.taxonomy.get(label_id)
            self._rows = (
                [(row["id"], row["label"], row["family"], row["active"], row["created_at"])]
                if row else []
            )

        elif s.startswith("UPDATE image_training_examples SET label"):
            new_label, old_label = params
            n = 0
            for te in c.training_examples.values():
                if te["label"] == old_label:
                    te["label"] = new_label
                    n += 1
            self.rowcount = n

        elif s.startswith("UPDATE dedup_sim.label_proposals SET label"):
            new_label, old_label = params
            n = 0
            for p in c.proposals.values():
                if p["label"] == old_label:
                    p["label"] = new_label
                    n += 1
            self.rowcount = n

        elif s.startswith("DELETE FROM image_training_examples WHERE label"):
            (label,) = params
            before = len(c.training_examples)
            c.training_examples = {
                k: v for k, v in c.training_examples.items() if v["label"] != label
            }
            self.rowcount = before - len(c.training_examples)

        elif s.startswith("DELETE FROM dedup_sim.label_proposals WHERE label"):
            (label,) = params
            before = len(c.proposals)
            c.proposals = {k: v for k, v in c.proposals.items() if v["label"] != label}
            self.rowcount = before - len(c.proposals)

        elif s.startswith("DELETE FROM dedup_sim.taxonomy_labels WHERE id"):
            (label_id,) = params
            self.rowcount = 1 if c.taxonomy.pop(label_id, None) is not None else 0

        elif s.startswith("SELECT count(*) FROM dedup_sim.labeling_sample"):
            self._rows = [(len(c.sample),)]

        elif s.startswith("SELECT t.id, t.label, t.family"):
            rows = []
            for t in sorted(c.taxonomy.values(), key=lambda t: t["label"]):
                confirmed = sum(1 for te in c.training_examples.values() if te["label"] == t["label"])
                pending = sum(
                    1 for p in c.proposals.values()
                    if p["label"] == t["label"] and p["status"] == "pending"
                )
                dismissed = sum(
                    1 for p in c.proposals.values()
                    if p["label"] == t["label"] and p["status"] == "dismissed"
                )
                rows.append((
                    t["id"], t["label"], t["family"], t["active"], t["created_at"],
                    confirmed, pending, dismissed,
                ))
            self._rows = rows

        elif s.startswith("INSERT INTO dedup_sim.labeling_sample"):
            kw = params
            added = 0
            for img_id in sorted(c.images, reverse=True):
                if len(c.sample) - added >= 0 and img_id not in c.sample:
                    if kw.get("category_main") is not None and c.image_category.get(img_id) != kw["category_main"]:
                        continue
                    c.sample[img_id] = {"image_id": img_id, "added_by": kw["added_by"]}
                    added += 1
                    if added >= kw["count"]:
                        break
            self.rowcount = added

        elif s.startswith("SELECT lp.image_id, lp.model, lp.label, lp.confidence"):
            kw = params
            rows = [
                p for p in c.proposals.values()
                if (kw.get("status") is None or p["status"] == kw["status"])
                and (kw.get("label") is None or p["label"] == kw["label"])
            ]
            rows.sort(key=lambda p: (p["proposed_at"], p["image_id"]), reverse=True)
            rows = rows[: kw["limit"]]
            self._rows = [
                (p["image_id"], p["model"], p["label"], p["confidence"], p["proposed_at"],
                 p["status"], p.get("reviewed_at"), p.get("reviewed_by"),
                 (c.training_examples.get(p["image_id"]) or {}).get("label"))
                for p in rows
            ]

        elif s.startswith("WITH all_rows AS"):
            # Mirrors _LIST_ALL_SQL: every proposal row (a confirmed one showing
            # the CURRENT training label), plus training examples that never had
            # a proposal at all, as synthetic 'manual' rows.
            kw = params
            rows = []
            for p in c.proposals.values():
                trained = (c.training_examples.get(p["image_id"]) or {}).get("label")
                label = trained if (p["status"] == "confirmed" and trained) else p["label"]
                rows.append((
                    p["image_id"], p["model"], label, p["confidence"], p["proposed_at"],
                    p["status"], p.get("reviewed_at"), p.get("reviewed_by"), trained,
                ))
            proposed_image_ids = {p["image_id"] for p in c.proposals.values()}
            for te in c.training_examples.values():
                if te["image_id"] in proposed_image_ids:
                    continue
                rows.append((
                    te["image_id"], "manual", te["label"], None, te.get("created_at", "t"),
                    "confirmed", te.get("updated_at", "t"), te.get("created_by"), te["label"],
                ))
            if kw.get("label") is not None:
                rows = [r for r in rows if r[2] == kw["label"]]
            rows.sort(key=lambda r: (r[4], r[0]), reverse=True)
            self._rows = rows[: kw["limit"]]

        elif s.startswith("WITH confirmed AS"):
            # Mirrors _LIST_CONFIRMED_SQL: driven FROM training_examples (one row per
            # image, label always the CURRENT value) with the most-recently-proposed
            # confirmed proposal for that image supplying display provenance, or a
            # synthetic 'manual' row when none exists.
            kw = params
            label_filter = kw.get("label")
            rows = []
            for te in c.training_examples.values():
                if label_filter is not None and te["label"] != label_filter:
                    continue
                confirmed = [
                    p for p in c.proposals.values()
                    if p["image_id"] == te["image_id"] and p["status"] == "confirmed"
                ]
                if confirmed:
                    latest = max(confirmed, key=lambda p: p["proposed_at"])
                    model, confidence = latest["model"], latest["confidence"]
                    proposed_at = latest["proposed_at"]
                    reviewed_at, reviewed_by = latest.get("reviewed_at"), latest.get("reviewed_by")
                else:
                    model, confidence = "manual", None
                    proposed_at = te.get("created_at", "t")
                    reviewed_at = te.get("updated_at", "t")
                    reviewed_by = te.get("created_by")
                rows.append((
                    te["image_id"], model, te["label"], confidence, proposed_at,
                    "confirmed", reviewed_at, reviewed_by, te["label"],
                ))
            rows.sort(key=lambda r: (r[4], r[0]), reverse=True)
            self._rows = rows[: kw["limit"]]

        elif s.startswith("UPDATE dedup_sim.label_proposals SET status = 'confirmed'") \
                and "image_id = ANY" in s:
            reviewed_by, model, ids = params
            rows = []
            for image_id in ids:
                p = c.proposals.get((image_id, model))
                if p is not None and p["status"] == "pending":
                    p["status"] = "confirmed"
                    p["reviewed_by"] = reviewed_by
                    rows.append((image_id, p["label"]))
            self._rows = rows

        elif s.startswith("UPDATE dedup_sim.label_proposals SET status = 'dismissed'") \
                and "image_id = ANY" in s:
            reviewed_by, model, ids = params
            n = 0
            for image_id in ids:
                p = c.proposals.get((image_id, model))
                if p is not None and p["status"] == "pending":
                    p["status"] = "dismissed"
                    p["reviewed_by"] = reviewed_by
                    n += 1
            self.rowcount = n

        elif s.startswith("UPDATE dedup_sim.label_proposals SET status = 'confirmed'"):
            reviewed_by, image_id, model = params
            p = c.proposals.get((image_id, model))
            if p is None or p["status"] != "pending":
                self._rows = []
            else:
                p["status"] = "confirmed"
                p["reviewed_by"] = reviewed_by
                self._rows = [(p["label"],)]

        elif s.startswith("UPDATE dedup_sim.label_proposals SET status = 'dismissed'"):
            reviewed_by, image_id, model = params
            p = c.proposals.get((image_id, model))
            if p is None or p["status"] != "pending":
                self._rows = []
            else:
                p["status"] = "dismissed"
                p["reviewed_by"] = reviewed_by
                self._rows = [(p["label"],)]

        elif s.startswith("INSERT INTO image_training_examples"):
            # Mirrors the real ON CONFLICT (image_id) DO UPDATE SET label=...,
            # updated_at=now() — created_by is only ever set on first insert,
            # never touched by a later re-confirm.
            image_id, label, created_by = params
            existing = c.training_examples.get(image_id)
            c.training_examples[image_id] = {
                "image_id": image_id, "label": label,
                "created_by": existing["created_by"] if existing else created_by,
            }

        else:
            raise AssertionError(f"unhandled SQL in fake conn: {s}")

    def executemany(self, sql: str, params_seq: Any) -> None:
        for params in params_seq:
            self.execute(sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    class UniqueViolation(Exception):
        pass

    def __init__(self) -> None:
        self.taxonomy: dict[int, dict[str, Any]] = {}
        self.next_taxonomy_id = 0
        self.training_examples: dict[int, dict[str, Any]] = {}
        self.proposals: dict[tuple[int, str], dict[str, Any]] = {}
        self.sample: dict[int, dict[str, Any]] = {}
        self.images: set[int] = set()
        self.image_category: dict[int, str] = {}
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Txn:
        return _Txn()


@pytest.fixture(autouse=True)
def _patch_unique_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route psycopg.errors.UniqueViolation catches in dsl.py to the fake's
    own exception class, so the fake conn doesn't need a real psycopg
    connection to exercise the duplicate-label path."""
    import psycopg.errors

    monkeypatch.setattr(psycopg.errors, "UniqueViolation", _FakeConn.UniqueViolation)


@pytest.fixture()
def conn() -> _FakeConn:
    return _FakeConn()


# --- taxonomy -----------------------------------------------------------


def test_add_taxonomy_label(conn: _FakeConn) -> None:
    row = dsl.add_taxonomy_label(conn, label="interier - kuchyne")
    assert row["label"] == "interier - kuchyne"
    assert row["active"] is True


def test_add_taxonomy_label_normalizes_whitespace(conn: _FakeConn) -> None:
    row = dsl.add_taxonomy_label(conn, label="  interier   -   kuchyne  ")
    assert row["label"] == "interier - kuchyne"


def test_add_taxonomy_label_rejects_empty(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.add_taxonomy_label(conn, label="   ")


def test_add_taxonomy_label_rejects_duplicate(conn: _FakeConn) -> None:
    dsl.add_taxonomy_label(conn, label="garaz")
    with pytest.raises(ValueError, match="already exists"):
        dsl.add_taxonomy_label(conn, label="garaz")


def test_rename_taxonomy_label_cascades(conn: _FakeConn) -> None:
    row = dsl.add_taxonomy_label(conn, label="old-name")
    conn.training_examples[1] = {"image_id": 1, "label": "old-name", "created_by": "x"}
    conn.proposals[(2, "m1")] = {
        "image_id": 2, "model": "m1", "label": "old-name", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }

    renamed = dsl.rename_taxonomy_label(conn, label_id=row["id"], new_label="new-name")
    assert renamed["label"] == "new-name"
    assert conn.training_examples[1]["label"] == "new-name"
    assert conn.proposals[(2, "m1")]["label"] == "new-name"


def test_rename_taxonomy_label_unknown_id_raises(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        dsl.rename_taxonomy_label(conn, label_id=999, new_label="x")


def test_rename_taxonomy_label_collision_raises(conn: _FakeConn) -> None:
    dsl.add_taxonomy_label(conn, label="a")
    b = dsl.add_taxonomy_label(conn, label="b")
    with pytest.raises(ValueError, match="already exists"):
        dsl.rename_taxonomy_label(conn, label_id=b["id"], new_label="a")


def test_rename_taxonomy_label_noop_when_unchanged(conn: _FakeConn) -> None:
    row = dsl.add_taxonomy_label(conn, label="same")
    renamed = dsl.rename_taxonomy_label(conn, label_id=row["id"], new_label="same")
    assert renamed["label"] == "same"


def test_remove_taxonomy_label_cascades(conn: _FakeConn) -> None:
    row = dsl.add_taxonomy_label(conn, label="doomed")
    conn.training_examples[1] = {"image_id": 1, "label": "doomed", "created_by": "x"}
    conn.proposals[(2, "m1")] = {
        "image_id": 2, "model": "m1", "label": "doomed", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }

    result = dsl.remove_taxonomy_label(conn, label_id=row["id"])
    assert result["deleted_training_examples"] == 1
    assert result["deleted_proposals"] == 1
    assert row["id"] not in conn.taxonomy
    assert conn.training_examples == {}
    assert conn.proposals == {}


def test_remove_taxonomy_label_unknown_id_raises(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        dsl.remove_taxonomy_label(conn, label_id=999)


def test_taxonomy_overview_shape(conn: _FakeConn) -> None:
    a = dsl.add_taxonomy_label(conn, label="a")
    dsl.add_taxonomy_label(conn, label="b")
    conn.training_examples[1] = {"image_id": 1, "label": "a", "created_by": "x"}
    conn.proposals[(2, "m1")] = {
        "image_id": 2, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    conn.proposals[(3, "m1")] = {
        "image_id": 3, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "dismissed",
    }
    conn.sample[1] = {"image_id": 1}
    conn.sample[2] = {"image_id": 2}

    overview = dsl.taxonomy_overview(conn)
    assert overview["sample_size"] == 2
    row_a = next(r for r in overview["labels"] if r["label"] == "a")
    assert row_a["id"] == a["id"]
    assert row_a["confirmed_count"] == 1
    assert row_a["pending_count"] == 1
    assert row_a["dismissed_count"] == 1
    row_b = next(r for r in overview["labels"] if r["label"] == "b")
    assert row_b["confirmed_count"] == 0


# --- sample ---------------------------------------------------------------


def test_grow_sample_adds_new_images(conn: _FakeConn) -> None:
    conn.images = {1, 2, 3, 4, 5}
    result = dsl.grow_sample(conn, count=3)
    assert result["added"] == 3
    assert len(conn.sample) == 3


def test_grow_sample_skips_already_sampled(conn: _FakeConn) -> None:
    conn.images = {1, 2, 3}
    conn.sample[3] = {"image_id": 3}
    result = dsl.grow_sample(conn, count=5)
    assert result["added"] == 2


def test_grow_sample_rejects_zero_or_negative(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.grow_sample(conn, count=0)


def test_grow_sample_rejects_over_max(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.grow_sample(conn, count=dsl.GROW_SAMPLE_MAX + 1)


def test_grow_sample_filters_by_category(conn: _FakeConn) -> None:
    conn.images = {1, 2}
    conn.image_category = {1: "byt", 2: "dum"}
    result = dsl.grow_sample(conn, count=5, category_main="byt")
    assert result["added"] == 1
    assert 1 in conn.sample
    assert 2 not in conn.sample


def test_grow_sample_includes_images_with_no_property_yet(conn: _FakeConn) -> None:
    # A new listing whose property_maintenance attach hasn't run yet has no
    # properties row (rule #19: new rows land property_id NULL) — an
    # unfiltered grow must still pick it up (LEFT JOIN, not INNER JOIN).
    conn.images = {1}
    result = dsl.grow_sample(conn, count=5)
    assert result["added"] == 1
    assert 1 in conn.sample


# --- proposals --------------------------------------------------------------


def test_list_proposals_filters(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t2", "status": "pending",
    }
    conn.proposals[(2, "m1")] = {
        "image_id": 2, "model": "m1", "label": "b", "confidence": 0.8,
        "proposed_at": "t1", "status": "confirmed",
    }
    pending = dsl.list_proposals(conn, status="pending")
    assert len(pending) == 1
    assert pending[0]["image_id"] == 1

    by_label = dsl.list_proposals(conn, label="b")
    assert len(by_label) == 1
    assert by_label[0]["image_id"] == 2


def test_list_proposals_confirmed_includes_training_examples_without_a_proposal(
    conn: _FakeConn,
) -> None:
    # image 1 was confirmed through this page's own proposal review flow.
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t2", "status": "confirmed",
    }
    conn.training_examples[1] = {"image_id": 1, "label": "a", "created_by": "operator"}
    # image 2 predates the Labeling page — trained via /phash-audit's Train CTA,
    # never went through a proposal at all.
    conn.training_examples[2] = {"image_id": 2, "label": "b", "created_by": "operator"}
    # a pending proposal must never leak into the confirmed listing.
    conn.proposals[(3, "m1")] = {
        "image_id": 3, "model": "m1", "label": "a", "confidence": 0.5,
        "proposed_at": "t3", "status": "pending",
    }

    confirmed = dsl.list_proposals(conn, status="confirmed")
    assert {r["image_id"] for r in confirmed} == {1, 2}
    manual_row = next(r for r in confirmed if r["image_id"] == 2)
    assert manual_row["model"] == "manual"
    assert manual_row["label"] == "b"
    assert manual_row["status"] == "confirmed"
    proposal_row = next(r for r in confirmed if r["image_id"] == 1)
    assert proposal_row["model"] == "m1"


def test_list_proposals_confirmed_does_not_duplicate_a_confirmed_proposal(
    conn: _FakeConn,
) -> None:
    # image_id 1 has BOTH a confirmed proposal AND (as confirm_proposal always
    # writes) a matching image_training_examples row — must surface exactly once.
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t2", "status": "confirmed",
    }
    conn.training_examples[1] = {"image_id": 1, "label": "a", "created_by": "operator"}

    confirmed = dsl.list_proposals(conn, status="confirmed")
    assert len(confirmed) == 1
    assert confirmed[0]["model"] == "m1"


def test_list_proposals_confirmed_shows_current_label_not_stale_proposal_label(
    conn: _FakeConn,
) -> None:
    # image 1 was confirmed here with label "kuchyne", then relabeled to "koupelna"
    # via the OLDER /phash-audit Train CTA — which only ever touches
    # image_training_examples, never label_proposals (api/labeling.py's
    # set_training_example). The confirmed listing must reflect the image's
    # CURRENT label, not the now-stale text still sitting in label_proposals.
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "kuchyne", "confidence": 0.9,
        "proposed_at": "t1", "status": "confirmed",
    }
    conn.training_examples[1] = {"image_id": 1, "label": "koupelna", "created_by": "operator"}

    confirmed = dsl.list_proposals(conn, status="confirmed")
    assert len(confirmed) == 1
    assert confirmed[0]["label"] == "koupelna"
    # provenance (which model/proposal it came from) is still preserved
    assert confirmed[0]["model"] == "m1"

    # filtering by the stale proposal text must NOT find it anymore...
    assert dsl.list_proposals(conn, status="confirmed", label="kuchyne") == []
    # ...only the current label does.
    by_current_label = dsl.list_proposals(conn, status="confirmed", label="koupelna")
    assert len(by_current_label) == 1


def test_list_proposals_confirmed_respects_label_filter(conn: _FakeConn) -> None:
    conn.training_examples[1] = {"image_id": 1, "label": "a", "created_by": "operator"}
    conn.training_examples[2] = {"image_id": 2, "label": "b", "created_by": "operator"}

    confirmed = dsl.list_proposals(conn, status="confirmed", label="a")
    assert [r["image_id"] for r in confirmed] == [1]


def test_list_proposals_carries_the_current_training_label(conn: _FakeConn) -> None:
    # `trained_label` is how the page tells an already-tagged image from an
    # untouched one WITHOUT a second query — image 1 is in the training set
    # (from /clip-audit, before this proposal ever existed), image 2 isn't.
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t2", "status": "pending",
    }
    conn.proposals[(2, "m1")] = {
        "image_id": 2, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t1", "status": "pending",
    }
    conn.training_examples[1] = {"image_id": 1, "label": "koupelna", "created_by": "operator"}

    rows = {r["image_id"]: r for r in dsl.list_proposals(conn, status="pending")}
    assert rows[1]["trained_label"] == "koupelna"
    assert rows[2]["trained_label"] is None


def test_list_proposals_all_is_the_union_of_the_three_tabs(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t4", "status": "pending",
    }
    conn.proposals[(2, "m1")] = {
        "image_id": 2, "model": "m1", "label": "b", "confidence": 0.8,
        "proposed_at": "t3", "status": "confirmed",
    }
    conn.training_examples[2] = {"image_id": 2, "label": "b", "created_by": "operator"}
    conn.proposals[(3, "m1")] = {
        "image_id": 3, "model": "m1", "label": "c", "confidence": 0.7,
        "proposed_at": "t2", "status": "dismissed",
    }
    # trained elsewhere, never proposed — still part of "all".
    conn.training_examples[4] = {
        "image_id": 4, "label": "d", "created_by": "operator", "created_at": "t1",
    }

    rows = dsl.list_proposals(conn, status="all")
    assert [r["image_id"] for r in rows] == [1, 2, 3, 4]
    assert {r["image_id"]: r["status"] for r in rows} == {
        1: "pending", 2: "confirmed", 3: "dismissed", 4: "confirmed",
    }
    assert rows[3]["model"] == "manual"
    # Only the reviewed-and-kept ones count as already tagged.
    assert {r["image_id"] for r in rows if r["trained_label"] is not None} == {2, 4}


def test_list_proposals_all_shows_a_corrected_label_not_the_model_s_guess(
    conn: _FakeConn,
) -> None:
    # The operator confirmed image 1 under a corrected label; the proposal row
    # deliberately keeps the model's own prediction (that's the record of what
    # the encoder said), so the 'all' listing must read the training set.
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "kuchyne", "confidence": 0.9,
        "proposed_at": "t", "status": "confirmed",
    }
    conn.training_examples[1] = {"image_id": 1, "label": "koupelna", "created_by": "operator"}

    rows = dsl.list_proposals(conn, status="all")
    assert rows[0]["label"] == "koupelna"
    assert dsl.list_proposals(conn, status="all", label="kuchyne") == []


def test_list_proposals_all_keeps_a_dismissed_row_showing_what_was_rejected(
    conn: _FakeConn,
) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "dismissed",
    }
    rows = dsl.list_proposals(conn, status="all")
    assert rows[0]["label"] == "a"
    assert rows[0]["trained_label"] is None


def test_list_proposals_all_respects_the_label_filter(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t2", "status": "pending",
    }
    conn.proposals[(2, "m1")] = {
        "image_id": 2, "model": "m1", "label": "b", "confidence": 0.9,
        "proposed_at": "t1", "status": "pending",
    }
    assert [r["image_id"] for r in dsl.list_proposals(conn, status="all", label="b")] == [2]


def test_confirm_proposal_writes_training_example(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    result = dsl.confirm_proposal(conn, image_id=1, model="m1")
    assert result["status"] == "confirmed"
    assert result["corrected"] is False
    assert conn.proposals[(1, "m1")]["status"] == "confirmed"
    assert conn.training_examples[1]["label"] == "a"


def test_confirm_proposal_with_corrected_label(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    result = dsl.confirm_proposal(conn, image_id=1, model="m1", label="  b  ")
    # The operator's correction is what lands in the training set...
    assert conn.training_examples[1]["label"] == "b"
    assert result["label"] == "b"
    assert result["corrected"] is True
    # ...while the proposal keeps the model's own prediction, so "model said a,
    # operator said b" stays derivable without an extra column.
    assert result["proposed_label"] == "a"
    assert conn.proposals[(1, "m1")]["label"] == "a"


def test_confirm_proposal_registers_a_freehand_correction_in_the_taxonomy(
    conn: _FakeConn,
) -> None:
    # The coverage chart, the tag picker and the secondary-CLIP backfill all
    # read dedup_sim.taxonomy_labels — a correction that only reached
    # image_training_examples would be invisible to every one of them, and the
    # model could never propose that class again.
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    assert not any(t["label"] == "brand-new-tag" for t in conn.taxonomy.values())

    dsl.confirm_proposal(conn, image_id=1, model="m1", label="brand-new-tag")

    assert any(t["label"] == "brand-new-tag" for t in conn.taxonomy.values())
    assert conn.training_examples[1]["label"] == "brand-new-tag"


def test_confirm_proposal_does_not_duplicate_an_existing_taxonomy_label(
    conn: _FakeConn,
) -> None:
    dsl.add_taxonomy_label(conn, label="existing")
    before = len(conn.taxonomy)
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    dsl.confirm_proposal(conn, image_id=1, model="m1", label="existing")
    assert len(conn.taxonomy) == before


def test_confirm_proposal_without_a_correction_touches_no_taxonomy_row(
    conn: _FakeConn,
) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    dsl.confirm_proposal(conn, image_id=1, model="m1")
    assert conn.taxonomy == {}


def test_confirm_proposal_blank_label_falls_back_to_the_proposal(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    result = dsl.confirm_proposal(conn, image_id=1, model="m1", label="   ")
    assert conn.training_examples[1]["label"] == "a"
    assert result["corrected"] is False


def test_confirm_proposal_same_label_is_not_flagged_as_corrected(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    # The UI always sends the picker's value, which is seeded from the
    # proposal — an untouched Confirm must not read as a correction.
    result = dsl.confirm_proposal(conn, image_id=1, model="m1", label="a")
    assert result["corrected"] is False


def test_confirm_proposal_rejects_an_overlong_correction(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    with pytest.raises(ValueError):
        dsl.confirm_proposal(conn, image_id=1, model="m1", label="x" * 101)
    # Rejected at the boundary — nothing was written, and the proposal is
    # still pending for a retry with a valid label.
    assert conn.training_examples == {}
    assert conn.proposals[(1, "m1")]["status"] == "pending"


def test_confirm_proposal_unknown_raises(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        dsl.confirm_proposal(conn, image_id=1, model="m1")


def test_confirm_proposal_already_reviewed_raises(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "dismissed",
    }
    with pytest.raises(KeyError):
        dsl.confirm_proposal(conn, image_id=1, model="m1")
    # A dismissed proposal must never be silently resurrected into confirmed,
    # nor should it write a training example.
    assert conn.proposals[(1, "m1")]["status"] == "dismissed"
    assert 1 not in conn.training_examples


def test_dismiss_proposal_after_confirm_does_not_retract_training_example(
    conn: _FakeConn,
) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    dsl.confirm_proposal(conn, image_id=1, model="m1")
    assert conn.training_examples[1]["label"] == "a"

    # A second (stale/retried) dismiss call against the now-confirmed
    # proposal must 404, not silently flip status while leaving the
    # already-written training example behind (the two stores diverging).
    with pytest.raises(KeyError):
        dsl.dismiss_proposal(conn, image_id=1, model="m1")
    assert conn.proposals[(1, "m1")]["status"] == "confirmed"
    assert conn.training_examples[1]["label"] == "a"


def test_dismiss_proposal(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    result = dsl.dismiss_proposal(conn, image_id=1, model="m1")
    assert result["status"] == "dismissed"
    assert conn.proposals[(1, "m1")]["status"] == "dismissed"
    assert 1 not in conn.training_examples


def test_dismiss_proposal_unknown_raises(conn: _FakeConn) -> None:
    with pytest.raises(KeyError):
        dsl.dismiss_proposal(conn, image_id=1, model="m1")


def test_dismiss_proposal_already_reviewed_raises(conn: _FakeConn) -> None:
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "a", "confidence": 0.9,
        "proposed_at": "t", "status": "confirmed",
    }
    with pytest.raises(KeyError):
        dsl.dismiss_proposal(conn, image_id=1, model="m1")
    assert conn.proposals[(1, "m1")]["status"] == "confirmed"


def test_confirm_proposal_preserves_created_by_on_re_review(conn: _FakeConn) -> None:
    # Simulates confirming a second proposal for the same image (e.g. after
    # a taxonomy rename produced a fresh pending row) — real Postgres'
    # ON CONFLICT (image_id) DO UPDATE never touches created_by once set.
    conn.training_examples[1] = {"image_id": 1, "label": "old", "created_by": "operator"}
    conn.proposals[(1, "m1")] = {
        "image_id": 1, "model": "m1", "label": "new", "confidence": 0.9,
        "proposed_at": "t", "status": "pending",
    }
    dsl.confirm_proposal(conn, image_id=1, model="m1", reviewed_by="someone_else")
    assert conn.training_examples[1]["label"] == "new"
    assert conn.training_examples[1]["created_by"] == "operator"


def _add_proposal(conn: _FakeConn, image_id: int, model: str, label: str, status: str = "pending") -> None:
    conn.proposals[(image_id, model)] = {
        "image_id": image_id, "model": model, "label": label, "confidence": 0.9,
        "proposed_at": "t", "status": status,
    }


def test_bulk_confirm_proposals_writes_training_examples(conn: _FakeConn) -> None:
    _add_proposal(conn, 1, "m1", "a")
    _add_proposal(conn, 2, "m1", "b")
    result = dsl.bulk_confirm_proposals(conn, model="m1", image_ids=[1, 2])
    assert result["confirmed"] == 2
    assert conn.proposals[(1, "m1")]["status"] == "confirmed"
    assert conn.proposals[(2, "m1")]["status"] == "confirmed"
    assert conn.training_examples[1]["label"] == "a"
    assert conn.training_examples[2]["label"] == "b"


def test_bulk_confirm_proposals_skips_already_reviewed(conn: _FakeConn) -> None:
    _add_proposal(conn, 1, "m1", "a", status="dismissed")
    result = dsl.bulk_confirm_proposals(conn, model="m1", image_ids=[1])
    assert result["confirmed"] == 0
    assert 1 not in conn.training_examples


def test_bulk_confirm_proposals_rejects_empty(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.bulk_confirm_proposals(conn, model="m1", image_ids=[])


def test_bulk_confirm_proposals_rejects_over_max(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.bulk_confirm_proposals(
            conn, model="m1", image_ids=list(range(dsl.BULK_PROPOSAL_MAX + 1)),
        )


def test_bulk_dismiss_proposals(conn: _FakeConn) -> None:
    _add_proposal(conn, 1, "m1", "a")
    _add_proposal(conn, 2, "m1", "b")
    result = dsl.bulk_dismiss_proposals(conn, model="m1", image_ids=[1, 2])
    assert result["dismissed"] == 2
    assert conn.proposals[(1, "m1")]["status"] == "dismissed"
    assert conn.proposals[(2, "m1")]["status"] == "dismissed"
    assert conn.training_examples == {}


def test_bulk_dismiss_proposals_rejects_empty(conn: _FakeConn) -> None:
    with pytest.raises(ValueError):
        dsl.bulk_dismiss_proposals(conn, model="m1", image_ids=[])
