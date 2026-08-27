"""Tests for toolkit/tag_definitions.py — the versioned tag-definition store
(migration 445).

Hermetic: a self-contained fake conn modelled on tests/toolkit/_labeling_fakes.py
(dispatch on the exact SQL the module issues). It is deliberately NOT that shared
module — nothing here overlaps the tables it models, and it is imported by two
other passing suites.

A fake conn cannot enforce CHECK / UNIQUE / FK, so no test here "passes" on the
fake being permissive: every assertion is on the SQL the module emits, the params
it binds, or the shape it returns. The one place the fake does model a constraint
(the partial unique index behind "exactly one active version") is named in the
test that relies on it, and the real guarantee is the index in migration 445 plus
the CI PREPARE gate.
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit import tag_definitions as td


def _unwrap(value: Any) -> Any:
    """psycopg wraps jsonb params in Jsonb(...); the fake stores the payload."""
    return getattr(value, "obj", value)


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
        if s.startswith("SELECT 1 FROM tag_taxonomy WHERE id"):
            self._rows = [(1,)] if params["tag_id"] in c.tag_taxonomy else []

        elif s.startswith("SELECT id FROM tag_taxonomy WHERE id = ANY"):
            self._rows = [(i,) for i in params["ids"] if i in c.tag_taxonomy]

        elif s.startswith("SELECT id, label FROM tag_taxonomy WHERE id = ANY"):
            rows = [
                (i, c.tag_taxonomy[i]) for i in params["ids"] if i in c.tag_taxonomy
            ]
            self._rows = sorted(rows, key=lambda r: r[1])

        # --- tag_definitions writes ---------------------------------------
        elif s.startswith("SELECT version FROM tag_definitions"):
            self._rows = [
                (d["version"],) for d in c.definitions
                if d["tag_id"] == params["tag_id"] and d["status"] == "active"
            ]

        elif s.startswith("UPDATE tag_definitions SET status = 'superseded'"):
            n = 0
            for d in c.definitions:
                if (
                    d["tag_id"] == params["tag_id"]
                    and d["status"] == "active"
                    and d["version"] == params["base_version"]
                ):
                    d["status"] = "superseded"
                    n += 1
            self.rowcount = n

        elif s.startswith("INSERT INTO tag_definitions"):
            if c.fail_next_insert_with_unique_violation:
                c.fail_next_insert_with_unique_violation = False
                raise c.UniqueViolation("tag_definitions_one_active_idx")
            tag_id = params["tag_id"]
            mine = [d for d in c.definitions if d["tag_id"] == tag_id]
            # Mirrors tag_definitions_one_active_idx. Named here because one test
            # depends on it; the real guarantee is the index, not this fake.
            if any(d["status"] == "active" for d in mine):
                raise c.UniqueViolation("tag_definitions_one_active_idx")
            c.next_definition_id += 1
            row = {
                "id": c.next_definition_id, "tag_id": tag_id,
                "version": max((d["version"] for d in mine), default=0) + 1,
                "means": params["means"],
                "counts": _unwrap(params["counts"]),
                "does_not_count": _unwrap(params["does_not_count"]),
                "confusable_with": _unwrap(params["confusable_with"]),
                "leave_out_when": params["leave_out_when"],
                "example_image_ids": list(params["example_image_ids"]),
                "status": "active", "created_at": c.tick(),
                "created_by": params["created_by"],
            }
            c.definitions.append(row)
            self._rows = [c.definition_row(row)]

        # --- tag_definitions reads ----------------------------------------
        elif s.startswith("SELECT d.id, d.tag_id, d.version, d.means"):
            tag_id = params["tag_id"]
            if tag_id not in c.tag_taxonomy:
                self._rows = []
            else:
                row = next(
                    (
                        d for d in c.definitions
                        if d["tag_id"] == tag_id and d["status"] == "active"
                    ),
                    None,
                )
                self._rows = [c.definition_row(row) if row else (None,) * 12]

        elif s.startswith("SELECT d.id, d.version, d.status, d.means"):
            tag_id = params["tag_id"]
            if tag_id not in c.tag_taxonomy:
                self._rows = []
            else:
                mine = sorted(
                    (d for d in c.definitions if d["tag_id"] == tag_id),
                    key=lambda d: d["version"], reverse=True,
                )
                rows = [
                    (d["id"], d["version"], d["status"], d["means"],
                     d["created_at"], d["created_by"])
                    for d in mine
                ]
                self._rows = rows[: params["limit"]] or [(None,) * 6]

        elif s.startswith("SELECT id, tag_id, version, means, counts"):
            row = next(
                (
                    d for d in c.definitions
                    if d["tag_id"] == params["tag_id"] and d["version"] == params["version"]
                ),
                None,
            )
            self._rows = [c.definition_row(row)] if row else []

        elif s.startswith("SELECT tag_id, id, version, means, created_at"):
            self._rows = [
                (d["tag_id"], d["id"], d["version"], d["means"], d["created_at"])
                for d in sorted(
                    (d for d in c.definitions if d["status"] == "active"),
                    key=lambda d: d["tag_id"],
                )
            ]

        # --- images / embeddings ------------------------------------------
        elif s.startswith("SELECT itl.image_id, i.storage_path, i.sreality_url"):
            rows = [
                (image_id, f"img/{image_id}.jpg", f"https://cdn/{image_id}.jpg", updated_at)
                for (image_id, tag_id, updated_at) in c.positives
                if tag_id == params["tag_id"]
            ]
            rows.sort(key=lambda r: (r[3], r[0]), reverse=True)
            self._rows = rows[: params["limit"]]

        elif s.startswith("WITH centroids AS"):
            self._rows = list(c.neighbour_rows)

        else:
            raise AssertionError(f"unhandled SQL in fake conn: {s}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Txn:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Txn":
        self._conn.executed.append(("BEGIN", None))
        return self

    def __exit__(self, exc_type: Any, *rest: Any) -> None:
        self._conn.executed.append(("ROLLBACK" if exc_type else "COMMIT", None))
        return None


class _FakeConn:
    class UniqueViolation(Exception):
        pass

    def __init__(self) -> None:
        self.tag_taxonomy: dict[int, str] = {}
        self.definitions: list[dict[str, Any]] = []
        self.next_definition_id = 0
        # (image_id, tag_id, updated_at) triples standing in for image_tag_labels
        # rows whose state is 'positive', already JOINed to images.
        self.positives: list[tuple[int, int, str]] = []
        self.neighbour_rows: list[tuple[Any, ...]] = []
        self.fail_next_insert_with_unique_violation = False
        self.executed: list[tuple[str, Any]] = []
        self._clock = 0

    # --- fixture helpers ---------------------------------------------------

    def tick(self) -> str:
        self._clock += 1
        return f"2026-08-27T00:00:{self._clock:02d}Z"

    def add_tags(self, **labels: str) -> None:
        """add_tags(t1='interier - kuchyne') -> tag_taxonomy[1] = that label."""
        for key, label in labels.items():
            self.tag_taxonomy[int(key.lstrip("t"))] = label

    def add_positive(self, image_id: int, tag_id: int) -> None:
        self.positives.append((image_id, tag_id, self.tick()))

    def definition_row(self, d: dict[str, Any]) -> tuple[Any, ...]:
        return (
            d["id"], d["tag_id"], d["version"], d["means"], d["counts"],
            d["does_not_count"], d["confusable_with"], d["leave_out_when"],
            d["example_image_ids"], d["status"], d["created_at"], d["created_by"],
        )

    def sql_log(self) -> list[str]:
        return [s for s, _ in self.executed]

    def active_versions(self, tag_id: int) -> list[int]:
        return [
            d["version"] for d in self.definitions
            if d["tag_id"] == tag_id and d["status"] == "active"
        ]

    # --- psycopg surface ---------------------------------------------------

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Txn:
        return _Txn(self)


@pytest.fixture()
def conn() -> _FakeConn:
    c = _FakeConn()
    c.add_tags(t1="interier - kuchyne", t2="interier - obyvaci pokoj", t3="exterier - fasada")
    return c


@pytest.fixture(autouse=True)
def _unique_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the module's psycopg.errors.UniqueViolation catch to the fake's own
    exception class, so the concurrent-save path is exercisable without a real
    psycopg connection."""
    import psycopg.errors

    monkeypatch.setattr(psycopg.errors, "UniqueViolation", _FakeConn.UniqueViolation)


# --- save: the supersede-never-overwrite contract ---------------------------


def test_first_save_lands_version_1_as_the_active_row(conn):
    out = td.save_definition(conn, tag_id=1, means="A kitchen inside a flat.")
    assert (out["version"], out["status"]) == (1, "active")
    assert out["tag_id"] == 1
    assert conn.active_versions(1) == [1]


def test_second_save_lands_version_2_and_supersedes_version_1(conn):
    td.save_definition(conn, tag_id=1, means="First take.")
    out = td.save_definition(conn, tag_id=1, means="Second take.", base_version=1)
    assert (out["version"], out["status"]) == (2, "active")
    by_version = {d["version"]: d["status"] for d in conn.definitions}
    assert by_version == {1: "superseded", 2: "active"}


def test_five_saves_leave_exactly_one_active_version(conn):
    for i in range(5):
        td.save_definition(conn, tag_id=1, means=f"take {i}", base_version=i or None)
    assert conn.active_versions(1) == [5]
    assert len(conn.definitions) == 5


def test_supersede_and_insert_are_issued_inside_one_transaction(conn):
    td.save_definition(conn, tag_id=1, means="A kitchen.")
    conn.executed.clear()
    td.save_definition(conn, tag_id=1, means="A kitchen, take two.", base_version=1)
    log = conn.sql_log()
    begin = log.index("BEGIN")
    supersede = next(i for i, s in enumerate(log) if s.startswith("UPDATE tag_definitions"))
    insert = next(i for i, s in enumerate(log) if s.startswith("INSERT INTO tag_definitions"))
    commit = log.index("COMMIT")
    assert begin < supersede < insert < commit


def test_insert_derives_the_version_in_sql_not_in_python(conn):
    # `coalesce(max(version), 0) + 1` computed by the database is what keeps two
    # racing saves from both claiming the same version number.
    td.save_definition(conn, tag_id=1, means="A kitchen.")
    insert = next(s for s in conn.sql_log() if s.startswith("INSERT INTO tag_definitions"))
    assert "coalesce(max(version), 0) + 1" in insert
    assert "'active'" in insert


def test_a_unique_violation_from_the_insert_surfaces_as_a_valueerror(conn):
    # The OVERLAPPING-transaction race: the loser's read predates the winner's
    # insert, so its own insert trips tag_definitions_one_active_idx.
    conn.fail_next_insert_with_unique_violation = True
    with pytest.raises(ValueError, match="another tab"):
        td.save_definition(conn, tag_id=1, means="A kitchen.")
    assert conn.definitions == []


# --- save: the stale-tab race the unique index CANNOT catch ------------------


def test_a_save_written_against_a_superseded_version_is_refused(conn):
    # Two tabs minutes apart are not overlapping transactions, so no unique
    # violation happens: without the base_version predicate this save would
    # supersede v2 and land its own v1-era text as v3.
    td.save_definition(conn, tag_id=1, means="First take.")
    td.save_definition(conn, tag_id=1, means="Second take.", base_version=1)

    with pytest.raises(ValueError, match="another tab"):
        td.save_definition(conn, tag_id=1, means="Stale tab's text.", base_version=1)

    assert conn.active_versions(1) == [2]
    assert [d["means"] for d in conn.definitions] == ["First take.", "Second take."]


def test_a_stale_save_supersedes_nothing_and_inserts_nothing(conn):
    td.save_definition(conn, tag_id=1, means="First take.")
    td.save_definition(conn, tag_id=1, means="Second take.", base_version=1)
    conn.executed.clear()

    with pytest.raises(ValueError, match="another tab"):
        td.save_definition(conn, tag_id=1, means="Stale.", base_version=1)

    log = conn.sql_log()
    assert not any(s.startswith("INSERT INTO tag_definitions") for s in log)
    assert log[-1] == "ROLLBACK"


def test_the_supersede_names_the_version_it_expects_to_retire(conn):
    td.save_definition(conn, tag_id=1, means="First take.")
    td.save_definition(conn, tag_id=1, means="Second take.", base_version=1)
    sql, params = next(
        (s, p) for s, p in conn.executed if s.startswith("UPDATE tag_definitions")
    )
    assert "AND version = %(base_version)s" in sql
    assert params["base_version"] == 1


def test_a_first_save_against_a_tag_that_gained_a_definition_meanwhile_is_refused(conn):
    # base_version=None asserts "this tag had no definition when I loaded it".
    td.save_definition(conn, tag_id=1, means="Someone else got there first.")
    with pytest.raises(ValueError, match="another tab"):
        td.save_definition(conn, tag_id=1, means="My blank-form text.")
    assert [d["means"] for d in conn.definitions] == ["Someone else got there first."]


def test_save_returns_the_document_with_its_tag_references_resolved(conn):
    out = td.save_definition(
        conn, tag_id=1, means="A kitchen.",
        confusable_with=[{"tag_id": 2, "tell": "no worktop = living room"}],
        does_not_count=[{"case": "kitchenette in a studio", "goes_to_tag_id": 3}],
    )
    assert out["referenced_tags"] == [
        {"tag_id": 3, "label": "exterier - fasada"},
        {"tag_id": 2, "label": "interier - obyvaci pokoj"},
    ]


# --- save: what is actually bound into the INSERT ---------------------------


def _insert_params(conn: _FakeConn) -> dict[str, Any]:
    return next(
        p for s, p in conn.executed if s.startswith("INSERT INTO tag_definitions")
    )


def test_save_binds_tag_references_as_ids_never_as_label_text(conn):
    td.save_definition(
        conn, tag_id=1, means="A kitchen.",
        confusable_with=[{"tag_id": 2, "tell": "no worktop"}],
        does_not_count=[{"case": "a kitchenette", "goes_to_tag_id": 3}],
    )
    params = _insert_params(conn)
    assert _unwrap(params["confusable_with"]) == [{"tag_id": 2, "tell": "no worktop"}]
    assert _unwrap(params["does_not_count"]) == [
        {"case": "a kitchenette", "goes_to_tag_id": 3},
    ]
    blob = repr(params)
    assert "interier - obyvaci pokoj" not in blob and "exterier - fasada" not in blob


def test_save_defaults_the_three_jsonb_documents_to_empty_arrays(conn):
    td.save_definition(conn, tag_id=1, means="A kitchen.")
    params = _insert_params(conn)
    assert _unwrap(params["counts"]) == []
    assert _unwrap(params["does_not_count"]) == []
    assert _unwrap(params["confusable_with"]) == []
    assert params["example_image_ids"] == []


def test_save_omits_goes_to_tag_id_as_null_when_the_case_belongs_nowhere(conn):
    td.save_definition(
        conn, tag_id=1, means="A kitchen.",
        does_not_count=[{"case": "a blurry photo of nothing"}],
    )
    assert _unwrap(_insert_params(conn)["does_not_count"]) == [
        {"case": "a blurry photo of nothing", "goes_to_tag_id": None},
    ]


def test_save_collapses_whitespace_and_dedupes_counts_preserving_order(conn):
    td.save_definition(
        conn, tag_id=1, means="  A   kitchen. ",
        counts=["  a  galley kitchen ", "an open kitchen", "a galley kitchen"],
    )
    params = _insert_params(conn)
    assert params["means"] == "A kitchen."
    assert _unwrap(params["counts"]) == ["a galley kitchen", "an open kitchen"]


def test_save_dedupes_confusable_with_by_tag_id_first_occurrence_wins(conn):
    td.save_definition(
        conn, tag_id=1, means="A kitchen.",
        confusable_with=[
            {"tag_id": 2, "tell": "first"}, {"tag_id": 2, "tell": "second"},
        ],
    )
    assert _unwrap(_insert_params(conn)["confusable_with"]) == [
        {"tag_id": 2, "tell": "first"},
    ]


def test_save_stores_a_blank_leave_out_when_as_null(conn):
    td.save_definition(conn, tag_id=1, means="A kitchen.", leave_out_when="   ")
    assert _insert_params(conn)["leave_out_when"] is None


def test_save_dedupes_example_image_ids_without_checking_they_exist(conn):
    # No FK is possible on a bigint[]; a deleted image is skipped at render time.
    td.save_definition(conn, tag_id=1, means="A kitchen.", example_image_ids=[7, 7, 9])
    assert _insert_params(conn)["example_image_ids"] == [7, 9]


# --- save: rejections -------------------------------------------------------


def test_save_on_an_unknown_tag_raises_keyerror_and_writes_nothing(conn):
    with pytest.raises(KeyError):
        td.save_definition(conn, tag_id=999, means="A kitchen.")
    assert not any(s.startswith("INSERT INTO tag_definitions") for s in conn.sql_log())


def test_save_referencing_a_nonexistent_tag_raises_valueerror(conn):
    with pytest.raises(ValueError, match="unknown tag_id"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.",
            confusable_with=[{"tag_id": 404, "tell": "nope"}],
        )
    assert not any(s.startswith("INSERT INTO tag_definitions") for s in conn.sql_log())


def test_save_with_a_nonexistent_goes_to_tag_id_raises_valueerror(conn):
    with pytest.raises(ValueError, match="unknown tag_id"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.",
            does_not_count=[{"case": "a hallway", "goes_to_tag_id": 404}],
        )


def test_save_rejects_a_tag_confusable_with_itself(conn):
    with pytest.raises(ValueError, match="confusable with itself"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.",
            confusable_with=[{"tag_id": 1, "tell": "same tag"}],
        )


def test_save_rejects_an_unknown_field_in_a_does_not_count_entry(conn):
    with pytest.raises(ValueError, match="unknown does_not_count field 'goes_to'"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.",
            does_not_count=[{"case": "a hallway", "goes_to": 2}],
        )


def test_save_rejects_an_unknown_field_in_a_confusable_with_entry(conn):
    with pytest.raises(ValueError, match="unknown confusable_with field 'label'"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.",
            confusable_with=[{"tag_id": 2, "tell": "x", "label": "kuchyne"}],
        )


def test_save_rejects_a_confusable_with_entry_missing_its_tag_id(conn):
    with pytest.raises(ValueError, match="tag_id is required"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.", confusable_with=[{"tell": "x"}],
        )


def test_save_rejects_a_non_integer_goes_to_tag_id(conn):
    with pytest.raises(ValueError, match="integer or null"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.",
            does_not_count=[{"case": "a hallway", "goes_to_tag_id": "kuchyne"}],
        )


@pytest.mark.parametrize("means", ["", "   ", None])
def test_save_rejects_a_blank_means(conn, means):
    with pytest.raises(ValueError, match="means must not be empty"):
        td.save_definition(conn, tag_id=1, means=means)


def test_save_rejects_an_over_long_means(conn):
    with pytest.raises(ValueError, match=f"at most {td.MEANS_MAX_CHARS} characters"):
        td.save_definition(conn, tag_id=1, means="x" * (td.MEANS_MAX_CHARS + 1))


def test_save_rejects_too_many_counts_entries(conn):
    with pytest.raises(ValueError, match=f"at most {td.COUNTS_MAX}"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.",
            counts=[f"case {i}" for i in range(td.COUNTS_MAX + 1)],
        )


def test_save_rejects_too_many_does_not_count_entries(conn):
    with pytest.raises(ValueError, match=f"at most {td.DOES_NOT_COUNT_MAX}"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.",
            does_not_count=[{"case": f"case {i}"} for i in range(td.DOES_NOT_COUNT_MAX + 1)],
        )


def test_save_rejects_too_many_example_images(conn):
    with pytest.raises(ValueError, match=f"at most {td.EXAMPLE_IMAGES_MAX}"):
        td.save_definition(
            conn, tag_id=1, means="A kitchen.",
            example_image_ids=list(range(td.EXAMPLE_IMAGES_MAX + 1)),
        )


def test_save_rejects_a_does_not_count_entry_that_is_not_an_object(conn):
    with pytest.raises(ValueError, match="must be an object"):
        td.save_definition(conn, tag_id=1, means="A kitchen.", does_not_count=["a hallway"])


# --- reads ------------------------------------------------------------------


def test_get_active_definition_returns_none_for_a_tag_with_no_definition(conn):
    assert td.get_active_definition(conn, tag_id=1) is None


def test_get_active_definition_raises_keyerror_for_an_unknown_tag(conn):
    with pytest.raises(KeyError):
        td.get_active_definition(conn, tag_id=999)


def test_get_active_definition_returns_the_newest_version_only(conn):
    td.save_definition(conn, tag_id=1, means="First take.")
    td.save_definition(conn, tag_id=1, means="Second take.", base_version=1)
    out = td.get_active_definition(conn, tag_id=1)
    assert (out["version"], out["means"]) == (2, "Second take.")


def test_referenced_tags_omits_an_id_whose_tag_no_longer_exists(conn):
    td.save_definition(
        conn, tag_id=1, means="A kitchen.",
        confusable_with=[{"tag_id": 2, "tell": "no worktop"}],
    )
    del conn.tag_taxonomy[2]  # the tag is renamed away / deleted later
    out = td.get_active_definition(conn, tag_id=1)
    assert out["confusable_with"] == [{"tag_id": 2, "tell": "no worktop"}]
    assert out["referenced_tags"] == []


def test_list_definition_versions_is_newest_first(conn):
    for i in range(3):
        td.save_definition(conn, tag_id=1, means=f"take {i}", base_version=i or None)
    versions = td.list_definition_versions(conn, tag_id=1)
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert [v["status"] for v in versions] == ["active", "superseded", "superseded"]
    assert "counts" not in versions[0]  # metadata only, no document body


def test_list_definition_versions_returns_empty_for_a_tag_with_no_versions(conn):
    assert td.list_definition_versions(conn, tag_id=1) == []


def test_list_definition_versions_raises_keyerror_for_an_unknown_tag(conn):
    with pytest.raises(KeyError):
        td.list_definition_versions(conn, tag_id=999)


def test_list_definition_versions_clamps_its_limit_to_the_cap(conn):
    td.save_definition(conn, tag_id=1, means="A kitchen.")
    td.list_definition_versions(conn, tag_id=1, limit=10_000)
    params = next(p for s, p in conn.executed if s.startswith("SELECT d.id, d.version"))
    assert params["limit"] == td.VERSION_LIST_MAX


def test_get_definition_version_returns_the_immutable_old_document(conn):
    td.save_definition(conn, tag_id=1, means="First take.", counts=["a galley kitchen"])
    td.save_definition(conn, tag_id=1, means="Second take.", base_version=1)
    old = td.get_definition_version(conn, tag_id=1, version=1)
    assert (old["means"], old["status"]) == ("First take.", "superseded")
    assert old["counts"] == ["a galley kitchen"]


def test_get_definition_version_raises_keyerror_for_a_missing_version(conn):
    td.save_definition(conn, tag_id=1, means="A kitchen.")
    with pytest.raises(KeyError):
        td.get_definition_version(conn, tag_id=1, version=7)


def test_list_definition_status_lists_only_the_active_row_per_tag(conn):
    td.save_definition(conn, tag_id=1, means="First take.")
    td.save_definition(conn, tag_id=1, means="Second take.", base_version=1)
    td.save_definition(conn, tag_id=2, means="A living room.")
    rows = td.list_definition_status(conn)
    assert [(r["tag_id"], r["version"]) for r in rows] == [(1, 2), (2, 1)]
    # No label/family/priority: tag_annotations.tag_overview is their one source.
    assert set(rows[0]) == {"tag_id", "definition_id", "version", "means", "created_at"}


def test_a_tag_with_no_definition_is_simply_absent_from_the_status_list(conn):
    td.save_definition(conn, tag_id=1, means="A kitchen.")
    assert [r["tag_id"] for r in td.list_definition_status(conn)] == [1]


# --- what the tag actually contains -----------------------------------------


def test_list_positive_images_reads_image_tag_labels_not_the_dedup_sim_sample(conn):
    conn.add_positive(11, 1)
    conn.add_positive(12, 1)
    conn.add_positive(13, 2)
    rows = td.list_positive_images(conn, tag_id=1)
    assert [r["image_id"] for r in rows] == [12, 11]  # newest first
    sql = next(s for s in conn.sql_log() if s.startswith("SELECT itl.image_id"))
    assert "dedup_sim" not in sql
    assert "FROM image_tag_labels itl JOIN images i" in sql


def test_list_positive_images_sorts_on_a_total_order(conn):
    # A bare timestamp sort reshuffles ties under the operator between refetches.
    sql = td._POSITIVE_IMAGES_SQL
    assert "ORDER BY itl.updated_at DESC, itl.image_id DESC" in " ".join(sql.split())


@pytest.mark.parametrize(
    ("asked", "bound"), [(10_000, td.POSITIVE_IMAGE_LIST_MAX), (0, 1), (25, 25)],
)
def test_list_positive_images_clamps_its_limit(conn, asked, bound):
    td.list_positive_images(conn, tag_id=1, limit=asked)
    params = next(p for s, p in conn.executed if s.startswith("SELECT itl.image_id"))
    assert params["limit"] == bound


# --- overlap evidence -------------------------------------------------------


def test_nearest_tags_returns_empty_when_the_subject_has_no_centroid(conn):
    # The CROSS JOIN over an empty `subject` CTE yields no rows — a tag under the
    # min_positives floor degrades to [], it never raises.
    conn.neighbour_rows = []
    assert td.nearest_tags(conn, tag_id=1) == []


def test_nearest_tags_maps_rows_to_a_named_cosine_distance(conn):
    conn.neighbour_rows = [(2, "interier - obyvaci pokoj", None, 31, 0.0412)]
    assert td.nearest_tags(conn, tag_id=1) == [
        {
            "tag_id": 2, "label": "interier - obyvaci pokoj", "family": None,
            "embedded_positive_count": 31, "cosine_distance": 0.0412,
        },
    ]


def test_nearest_tags_defaults_the_model_to_the_checkpoint_in_the_taxonomy_file(conn):
    from scraper.clip_tagger import load_taxonomy

    td.nearest_tags(conn, tag_id=1)
    params = next(p for s, p in conn.executed if s.startswith("WITH centroids AS"))
    assert params["model"] == load_taxonomy()["model"]


def test_nearest_tags_lets_an_explicit_model_override_the_default(conn):
    td.nearest_tags(conn, tag_id=1, model="some/other-checkpoint")
    params = next(p for s, p in conn.executed if s.startswith("WITH centroids AS"))
    assert params["model"] == "some/other-checkpoint"


@pytest.mark.parametrize(
    ("asked", "bound"), [(10_000, td.NEIGHBOUR_LIMIT_MAX), (0, 1), (5, 5)],
)
def test_nearest_tags_clamps_its_limit(conn, asked, bound):
    td.nearest_tags(conn, tag_id=1, limit=asked)
    params = next(p for s, p in conn.executed if s.startswith("WITH centroids AS"))
    assert params["limit"] == bound


def test_nearest_tags_binds_the_min_positives_floor(conn):
    td.nearest_tags(conn, tag_id=1)
    params = next(p for s, p in conn.executed if s.startswith("WITH centroids AS"))
    assert params["min_positives"] == td.MIN_POSITIVES_FOR_CENTROID == 5


def test_nearest_tags_orders_by_distance_with_a_tag_id_tiebreaker(conn):
    # Equal distances would otherwise reshuffle the list between refetches, and
    # `<=>` is a DISTANCE (0 = identical), so ascending is nearest-first.
    sql = " ".join(td._NEAREST_TAGS_SQL.split())
    assert "ORDER BY cosine_distance, c.tag_id" in sql
    assert "(c.centroid <=> s.centroid) AS cosine_distance" in sql
