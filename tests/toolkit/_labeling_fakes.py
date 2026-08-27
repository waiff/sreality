"""Hermetic fake conn for the labeling stack — no DB (migrations 373 + 442 are
verified separately, live).

Models the four tables `toolkit/tag_annotations.py` and
`toolkit/dedup_sim_labeling.py` write (`tag_taxonomy`, `image_tag_labels`,
`dedup_sim.labeling_sample`, `dedup_sim.label_proposals`) plus the three they only
read (`images`, `image_border_cases`, and — since migration 450 — `tag_candidates`,
the per-tag review queue the tag browse and the overview now read), dispatching on
the exact SQL those modules issue. `dedup_sim.labeling_sample` stays modelled:
`grow_sample` still writes it for the secondary-CLIP proposal lane.

Shared by both test modules on purpose: `set_proposal_state` resolves a tag and
writes a tri-state cell by calling into `tag_annotations` with the SAME
connection, so the proposal tests exercise the real write path end to end rather
than a mock that could drift from it.

DELIBERATELY NOT MODELLED: migration 446's `image_tag_labels_log_event` trigger.
A fake that modelled a trigger would be a second, drifting implementation of it,
and would manufacture coverage the DB alone can give. `image_tag_label_events` is
therefore untestable here — its gate is CI's migration-replay job. Same standing
limit as CHECK / UNIQUE / FK violations, which this fake also cannot raise (see
the "adversarial review vs fake-conn" note): assert on the SQL emitted or the
shapes returned, never on the fake being permissive.
"""

from __future__ import annotations

from typing import Any

import pytest


def _tag_row(t: dict[str, Any]) -> tuple[Any, ...]:
    return (
        t["id"], t["label"], t["family"], t["active"],
        t["priority"], t["ready_for_training"], t["created_at"],
    )


def _cell_row(v: dict[str, Any]) -> tuple[Any, ...]:
    """The RETURNING / _READ_STATE_SQL column list, in order (migration 446)."""
    return (
        v["image_id"], v["tag_id"], v["state"], v["source"], v["excluded_reason"],
        v["definition_id"], v["verified_at"], v["updated_at"],
    )


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

        # --- tag_taxonomy -------------------------------------------------
        # get_or_create_tag_id's self-registering insert: 2 params, and a
        # duplicate is a silent no-op rather than a UniqueViolation.
        if s.startswith("INSERT INTO tag_taxonomy (label, created_by)"):
            label, created_by = params
            if c.tag_by_label(label) is not None:
                self._rows = []
            else:
                self._rows = [(c.insert_tag(label=label, created_by=created_by)["id"],)]

        elif s.startswith("INSERT INTO tag_taxonomy (label, family, created_by)"):
            label, family, created_by = params
            if c.tag_by_label(label) is not None:
                raise c.UniqueViolation(f"duplicate label {label!r}")
            self._rows = [_tag_row(c.insert_tag(label=label, family=family, created_by=created_by))]

        elif s.startswith("SELECT id FROM tag_taxonomy WHERE label"):
            (label,) = params
            row = c.tag_by_label(label)
            self._rows = [(row["id"],)] if row else []

        elif s.startswith("SELECT label FROM tag_taxonomy WHERE id"):
            (tag_id,) = params
            row = c.tag_taxonomy.get(tag_id)
            self._rows = [(row["label"],)] if row else []

        elif s.startswith("UPDATE tag_taxonomy SET label"):
            new_label, tag_id = params
            if any(t["label"] == new_label for tid, t in c.tag_taxonomy.items() if tid != tag_id):
                raise c.UniqueViolation(f"duplicate label {new_label!r}")
            row = c.tag_taxonomy.get(tag_id)
            if row is None:
                self._rows, self.rowcount = [], 0
            else:
                row["label"] = new_label
                self._rows, self.rowcount = [_tag_row(row)], 1

        elif s.startswith("UPDATE tag_taxonomy SET priority") or s.startswith(
            "UPDATE tag_taxonomy SET ready_for_training",
        ):
            # set_tag_flags builds its SET list dynamically (one or both
            # fields); the last param is always tag_id, the rest line up
            # with whichever "field = %s" fragments the SQL text names.
            *values, tag_id = params
            row = c.tag_taxonomy.get(tag_id)
            if row is None:
                self._rows = []
            else:
                # Scoped to the SET clause only — RETURNING always lists both
                # column names, so checking the whole string would wrongly
                # "detect" a field that wasn't actually being written.
                set_clause = s.split(" WHERE ", 1)[0]
                fields = [f for f in ("priority", "ready_for_training") if f in set_clause]
                for field, value in zip(fields, values):
                    row[field] = value
                self._rows = [_tag_row(row)]

        elif s.startswith("DELETE FROM tag_taxonomy WHERE id"):
            (tag_id,) = params
            self.rowcount = 1 if c.tag_taxonomy.pop(tag_id, None) is not None else 0

        # --- image_tag_labels ---------------------------------------------
        elif s.startswith("INSERT INTO image_tag_labels"):
            # Mirrors ON CONFLICT (image_id, tag_id) DO UPDATE SET ... —
            # created_by is set on first insert only, never rewritten by a later
            # re-decision. Named parameters since migration 446 (tag_id appears
            # twice: as a column value and inside the definition_id subquery).
            # The bulk path issues the same statement without RETURNING.
            kw = params
            image_id, tag_id = kw["image_id"], kw["tag_id"]
            existing = c.image_tag_labels.get((image_id, tag_id))
            # The human-wins rail, the fake's copy of the DO UPDATE's WHERE: a
            # machine write lands only on an untouched / machine / backfill cell.
            suppressed = existing is not None and not (
                kw["source"] != "machine"
                or existing["source"] in ("machine", "backfill_442")
            )
            if suppressed:
                self.rowcount = 0
                self._rows = []
            else:
                stamped = c.tick() if kw["verified"] else None
                row = {
                    "image_id": image_id, "tag_id": tag_id, "state": kw["state"],
                    "created_by": existing["created_by"] if existing else kw["created_by"],
                    "source": kw["source"],
                    # resolved by the INSERT's own subquery on the annotation's
                    # tag — never a parameter (see toolkit/tag_annotations.py).
                    "definition_id": c.active_definitions.get(tag_id),
                    "model": kw["model"],
                    "excluded_reason": kw["excluded_reason"],
                    # coalesce(excluded.verified_at, image_tag_labels.verified_at):
                    # a machine write can never erase a human's verification.
                    "verified_at": stamped or (existing["verified_at"] if existing else None),
                    "updated_at": c.tick(),
                }
                c.image_tag_labels[(image_id, tag_id)] = row
                self.rowcount = 1
                self._rows = [_cell_row(row)] if "RETURNING" in s else []

        elif s.startswith("SELECT image_id, tag_id, state, source, excluded_reason"):
            row = c.image_tag_labels.get((params["image_id"], params["tag_id"]))
            self._rows = [_cell_row(row)] if row else []

        elif s.startswith("DELETE FROM image_tag_labels WHERE tag_id"):
            (tag_id,) = params
            before = len(c.image_tag_labels)
            c.image_tag_labels = {
                k: v for k, v in c.image_tag_labels.items() if v["tag_id"] != tag_id
            }
            self.rowcount = before - len(c.image_tag_labels)

        elif s.startswith("DELETE FROM image_tag_labels WHERE image_id"):
            image_id, tag_id = params
            self.rowcount = 1 if c.image_tag_labels.pop((image_id, tag_id), None) else 0

        elif s.startswith("SELECT q.image_id, i.storage_path, itl.state"):
            # The migration-450 browse: arm one is this tag's candidate queue, arm
            # two every image already DECIDED for the tag but never drawn as a
            # candidate (without it the legacy positives would look deleted).
            kw = params
            tag_id = kw["tag_id"]
            queue: list[tuple[int, Any, Any, Any, Any]] = [
                (cand["image_id"], cand["drawn_at"], cand["pool_rank"],
                 cand["draw"], cand["category_main"])
                for (tid, image_id), cand in c.tag_candidates.items() if tid == tag_id
            ]
            drawn_ids = {q[0] for q in queue}
            queue += [
                (image_id, None, None, None, None)
                for (image_id, tid) in c.image_tag_labels
                if tid == tag_id and image_id not in drawn_ids
            ]
            rows = []
            for image_id, drawn_at, pool_rank, draw, category_main in queue:
                if image_id not in c.images:  # JOIN images
                    continue
                cell = c.image_tag_labels.get((image_id, tag_id))
                state = cell["state"] if cell else None
                want = kw["state"]
                if want is not None and not (
                    (want == "untouched" and state is None) or state == want
                ):
                    continue
                rows.append((
                    image_id, c.images[image_id], state,
                    cell["updated_at"] if cell else None,
                    cell["created_by"] if cell else None,
                    cell["source"] if cell else None,
                    cell["excluded_reason"] if cell else None,
                    draw, category_main, pool_rank, drawn_at,
                ))
            # ORDER BY drawn_at DESC NULLS LAST, pool_rank ASC NULLS LAST,
            # image_id DESC — stable passes, least significant key first, because
            # the directions differ per key (Python's sort keeps the order of
            # equal elements, reverse=True included).
            rows.sort(key=lambda r: r[0], reverse=True)
            rows.sort(key=lambda r: (r[9] is None, r[9] if r[9] is not None else 0))
            rows = sorted(
                (r for r in rows if r[10] is not None), key=lambda r: r[10], reverse=True,
            ) + [r for r in rows if r[10] is None]
            self._rows = [r[:10] for r in rows[: kw["limit"]]]

        elif s.startswith("SELECT count(DISTINCT image_id)::int FROM tag_candidates"):
            self._rows = [(len({image_id for _tid, image_id in c.tag_candidates}),)]

        elif s.startswith("SELECT t.id, t.label, t.family, itl.state, itl.updated_at"):
            image_id = params["image_id"]
            rows = []
            for t in c.tag_taxonomy.values():
                if not t["active"]:
                    continue
                cell = c.image_tag_labels.get((image_id, t["id"]))
                rows.append((
                    t["id"], t["label"], t["family"],
                    cell["state"] if cell else None,
                    cell["updated_at"] if cell else None,
                    cell["source"] if cell else None,
                    cell["excluded_reason"] if cell else None,
                    t["family"] or "￿",  # NULLS LAST sort key
                ))
            rows.sort(key=lambda r: (r[7], r[1]))
            self._rows = [r[:7] for r in rows]

        elif s.startswith("SELECT itl.image_id, t.id, t.label"):
            image_ids = set(params["image_ids"])
            rows = []
            for (image_id, tag_id), cell in c.image_tag_labels.items():
                if cell["state"] != "positive" or image_id not in image_ids:
                    continue
                tag = c.tag_taxonomy.get(tag_id)
                if tag is None:
                    continue
                rows.append((image_id, tag_id, tag["label"]))
            rows.sort(key=lambda r: (r[0], r[2]))
            self._rows = rows

        elif s.startswith("SELECT t.id, t.label, t.family, t.active, t.priority"):
            rows = []
            for t in sorted(c.tag_taxonomy.values(), key=lambda t: t["label"]):
                cells = [v for v in c.image_tag_labels.values() if v["tag_id"] == t["id"]]
                positive = [v for v in cells if v["state"] == "positive"]
                border = [v for v in positive if v["image_id"] in c.border_cases]

                def _pruned(v: dict[str, Any]) -> bool:
                    return v["state"] == "excluded" and v["excluded_reason"] == "pruned"

                def _ambiguous(v: dict[str, Any]) -> bool:
                    # A NULL reason counts as ambiguous, never as a third silent
                    # bucket — matching the grid's own fallback.
                    return v["state"] == "excluded" and v["excluded_reason"] != "pruned"

                real = [v for v in cells if v["source"] in ("human", "human_confirmed")]
                # Pruned exclusions sit outside BOTH the numerator and the
                # denominator; everything nobody verified (backfill_442 AND
                # machine) sits outside the denominator — the rate measures human
                # indecision.
                ambiguous_decided = sum(1 for v in real if _ambiguous(v))
                decided = sum(
                    1 for v in real
                    if v["state"] in ("positive", "negative") or _ambiguous(v)
                )
                # NULL, never 0: a tag with no decisions is unknown, not healthy.
                rate = (ambiguous_decided / decided) if decided else None
                rows.append((
                    t["id"], t["label"], t["family"], t["active"],
                    t["priority"], t["ready_for_training"], t["created_at"],
                    len(positive), len(positive) - len(border), len(border),
                    sum(1 for v in cells if v["state"] == "negative"),
                    sum(1 for v in cells if v["state"] == "excluded"),
                    sum(1 for v in cells if v["source"] in ("human", "human_confirmed")),
                    sum(1 for v in cells if v["source"] == "machine"),
                    sum(1 for v in cells if v["source"] == "backfill_442"),
                    sum(1 for v in cells if _ambiguous(v)),
                    ambiguous_decided,
                    sum(1 for v in cells if _pruned(v)),
                    decided,
                    rate,
                    bool(
                        decided >= params["min_decisions"]
                        and rate is not None and rate > params["threshold"]
                    ),
                    c.count_proposals(t["label"], "pending"),
                    c.count_proposals(t["label"], "dismissed"),
                    # The migration-450 aggregate: how big this tag's review queue
                    # is and how much of it nobody has decided yet. `open` is a
                    # JOIN onto image_tag_labels — the queue stores no state.
                    *c.candidate_counts(t["id"]),
                ))
            self._rows = rows

        # --- dedup_sim.labeling_sample ------------------------------------
        elif s.startswith("SELECT count(*) FROM dedup_sim.labeling_sample"):
            self._rows = [(len(c.sample),)]

        elif s.startswith("INSERT INTO dedup_sim.labeling_sample"):
            kw = params
            added = 0
            for image_id in sorted(c.images, reverse=True):
                if image_id in c.sample:
                    continue
                if (
                    kw["category_main"] is not None
                    and c.image_category.get(image_id) != kw["category_main"]
                ):
                    continue
                c.add_to_sample(image_id, added_by=kw["added_by"])
                added += 1
                if added >= kw["count"]:
                    break
            self.rowcount = added

        # --- dedup_sim.label_proposals ------------------------------------
        elif s.startswith("SELECT lp.image_id, lp.model, lp.label, lp.confidence"):
            kw = params
            rows = []
            for p in c.proposals.values():
                if kw["status"] not in (None, "all") and p["status"] != kw["status"]:
                    continue
                if kw["label"] is not None and p["label"] != kw["label"]:
                    continue
                if kw["original_tag"] is not None and (
                    c.original_clip_tags.get(p["image_id"]) != kw["original_tag"]
                ):
                    continue
                tag = c.tag_by_label(p["label"])
                cell = c.image_tag_labels.get((p["image_id"], tag["id"])) if tag else None
                rows.append((
                    p["image_id"], p["model"], p["label"], p["confidence"], p["proposed_at"],
                    p["status"], p.get("reviewed_at"), p.get("reviewed_by"),
                    cell["state"] if cell else None,
                    cell["excluded_reason"] if cell else None,
                ))
            rows.sort(key=lambda r: (r[4], r[0]), reverse=True)
            self._rows = rows[: kw["limit"]]

        elif s.startswith("UPDATE dedup_sim.label_proposals SET status") and "image_id = ANY" in s:
            # No status filter in the real SQL any more — re-deciding an
            # already-reviewed proposal is allowed (only ONE write path into
            # image_tag_labels here, so a repeat call can't diverge anything).
            status, reviewed_by, model, ids = params
            rows = []
            for image_id in ids:
                p = c.proposals.get((image_id, model))
                if p is not None:
                    p.update(status=status, reviewed_by=reviewed_by, reviewed_at=c.tick())
                    rows.append((image_id, p["label"]))
            self._rows = rows

        elif s.startswith("UPDATE dedup_sim.label_proposals SET status"):
            status, reviewed_by, image_id, model = params
            p = c.proposals.get((image_id, model))
            if p is None:
                self._rows = []
            else:
                p.update(status=status, reviewed_by=reviewed_by, reviewed_at=c.tick())
                self._rows = [(p["label"],)]

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
        self.tag_taxonomy: dict[int, dict[str, Any]] = {}
        self.next_tag_id = 0
        self.image_tag_labels: dict[tuple[int, int], dict[str, Any]] = {}
        # tag_definitions (migration 445), modelled only as far as the annotation
        # write path reads it: tag_id -> the id of that tag's ACTIVE definition.
        # The versioning itself is tested in tests/toolkit/test_tag_definitions.py.
        self.active_definitions: dict[int, int] = {}
        self.proposals: dict[tuple[int, str], dict[str, Any]] = {}
        self.sample: dict[int, dict[str, Any]] = {}
        # tag_candidates (migration 450), keyed (tag_id, image_id) like the PK. A
        # review queue: no state column here either, on purpose.
        self.tag_candidates: dict[tuple[int, int], dict[str, Any]] = {}
        # images: id -> storage_path. grow_sample's real SQL requires
        # storage_path IS NOT NULL, so every fixture image gets one.
        self.images: dict[int, str] = {}
        self.image_category: dict[int, str] = {}
        # image_clip_tags.fine_tag — the production tagger's own vocabulary
        # (a different, fixed vocabulary from tag_taxonomy). Simplified to
        # one fine_tag per image_id: the fake models "latest model wins",
        # and every real writer today is the single production model.
        self.original_clip_tags: dict[int, str] = {}
        # image_border_cases (migration 310) — the operator's "unclear even to a
        # human" flag. Written elsewhere (api/labeling.py); the overview only
        # reads it, to say how much of a tag's coverage is uncertain.
        self.border_cases: set[int] = set()
        self.executed: list[tuple[str, Any]] = []
        self._clock = 0

    # --- fixture helpers ---------------------------------------------------

    def tick(self) -> str:
        self._clock += 1
        return f"2026-08-26T00:00:{self._clock:02d}Z"

    def insert_tag(
        self, *, label: str, family: str | None = None, created_by: str = "operator",
    ) -> dict[str, Any]:
        self.next_tag_id += 1
        row = {
            "id": self.next_tag_id, "label": label, "family": family, "active": True,
            "priority": False, "ready_for_training": False,
            "created_at": "2026-08-26T00:00:00Z", "created_by": created_by,
        }
        self.tag_taxonomy[row["id"]] = row
        return row

    def seed_cell(
        self, image_id: int, tag_id: int, state: str, *, source: str = "human",
        created_by: str = "operator", excluded_reason: str | None = None,
        model: str | None = None, definition_id: int | None = None,
        verified_at: str | None = None,
    ) -> None:
        """Put a row into image_tag_labels WITHOUT going through the write path —
        the only way to stand up rows the toolkit can no longer produce, i.e.
        migration 442's manufactured `backfill_442` negatives."""
        self.image_tag_labels[(image_id, tag_id)] = {
            "image_id": image_id, "tag_id": tag_id, "state": state,
            "created_by": created_by, "source": source,
            "definition_id": definition_id, "model": model,
            "excluded_reason": excluded_reason, "verified_at": verified_at,
            "updated_at": self.tick(),
        }

    def set_active_definition(self, tag_id: int, definition_id: int) -> None:
        self.active_definitions[tag_id] = definition_id

    def tag_by_label(self, label: str) -> dict[str, Any] | None:
        return next((t for t in self.tag_taxonomy.values() if t["label"] == label), None)

    def add_images(self, *image_ids: int) -> None:
        for image_id in image_ids:
            self.images[image_id] = f"img/{image_id}.jpg"

    def set_original_tag(self, image_id: int, fine_tag: str) -> None:
        self.original_clip_tags[image_id] = fine_tag

    def add_to_sample(
        self, image_id: int, *, added_by: str = "operator", added_at: str | None = None,
    ) -> None:
        self.images.setdefault(image_id, f"img/{image_id}.jpg")
        self.sample[image_id] = {
            "image_id": image_id, "added_by": added_by, "added_at": added_at or self.tick(),
        }

    def add_candidate(
        self, tag_id: int, image_id: int, *, draw: str = "centroid_head",
        category_main: str = "byt", pool_rank: int = 1,
        drawn_at: str | None = None,
    ) -> None:
        """Queue one image for review on one tag — the fixture form of a drawn
        candidate. Says nothing about the image's state for that tag; a label, if
        any, lives in image_tag_labels."""
        self.images.setdefault(image_id, f"img/{image_id}.jpg")
        self.tag_candidates[(tag_id, image_id)] = {
            "tag_id": tag_id, "image_id": image_id, "draw": draw,
            "category_main": category_main, "pool_rank": pool_rank,
            "drawn_at": drawn_at or self.tick(),
        }

    def candidate_counts(self, tag_id: int) -> tuple[int, int, str | None]:
        """(candidate_count, candidate_open_count, last_drawn_at) for one tag —
        `open` = queued with no image_tag_labels row for that same tag."""
        rows = [v for (tid, _img), v in self.tag_candidates.items() if tid == tag_id]
        open_count = sum(
            1 for v in rows
            if (v["image_id"], tag_id) not in self.image_tag_labels
        )
        last = max((v["drawn_at"] for v in rows), default=None)
        return len(rows), open_count, last

    def add_proposal(
        self, image_id: int, model: str, label: str, *, status: str = "pending",
        confidence: float = 0.9, proposed_at: str = "t",
    ) -> None:
        self.proposals[(image_id, model)] = {
            "image_id": image_id, "model": model, "label": label,
            "confidence": confidence, "proposed_at": proposed_at, "status": status,
        }

    def count_proposals(self, label: str, status: str) -> int:
        return sum(
            1 for p in self.proposals.values()
            if p["label"] == label and p["status"] == status
        )

    def states_for(self, tag_id: int) -> dict[int, str]:
        return {
            image_id: v["state"]
            for (image_id, tid), v in self.image_tag_labels.items() if tid == tag_id
        }

    def sources_for(self, tag_id: int) -> dict[int, str]:
        return {
            image_id: v["source"]
            for (image_id, tid), v in self.image_tag_labels.items() if tid == tag_id
        }

    # --- psycopg surface ---------------------------------------------------

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Txn:
        return _Txn()


def patch_unique_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route psycopg.errors.UniqueViolation catches in the toolkit modules to
    the fake's own exception class, so the fake conn doesn't need a real psycopg
    connection to exercise the duplicate-label path."""
    import psycopg.errors

    monkeypatch.setattr(psycopg.errors, "UniqueViolation", _FakeConn.UniqueViolation)
