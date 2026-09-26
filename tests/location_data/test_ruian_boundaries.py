"""Boundary pack: layer mapping, the 8-vs-14 assertion, the three-geometry contract,
carry-forward, and the completeness read that gates publish."""

from __future__ import annotations

import inspect
from pathlib import Path

import psycopg
import pytest

from location_data import ruian_boundaries as rb


def _feature(level: str, code: int) -> rb.BoundaryFeature:
    return rb.BoundaryFeature(level=level, code=code, name=f"unit {code}", wkb=b"")


def test_region_and_vusc_are_different_layers_and_both_are_asserted():
    region = next(x for x in rb.LAYERS if x.token == "REGION_P")
    vusc = next(x for x in rb.LAYERS if x.token == "VUSC_P")
    assert (region.level, region.expected_features) == ("region_soudrznosti", 8)
    assert (vusc.level, vusc.expected_features) == ("kraj", 14)


def test_feature_count_assertion_catches_the_region_vusc_mixup():
    vusc = next(x for x in rb.LAYERS if x.token == "VUSC_P")
    rb.assert_feature_counts(vusc, [_feature("kraj", i) for i in range(14)])
    with pytest.raises(rb.BoundarySchemaError):
        rb.assert_feature_counts(vusc, [_feature("kraj", i) for i in range(8)])


def test_layers_without_an_expected_count_are_not_asserted():
    obce = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    assert obce.expected_features is None
    rb.assert_feature_counts(obce, [])


def test_every_layer_is_a_distinct_mirror_level():
    """One layer per level, so a done-set prefetched per level can never contain codes a
    sibling layer wrote in the same run."""
    levels = [layer.level for layer in rb.LAYERS]
    assert len(levels) == len(set(levels))
    assert set(rb.COMPLETE_LEVELS) <= set(levels)


def test_the_office_layers_are_not_spravni_obvod():
    """STU_P (stavební úřady, keyed by an office ID) and PRARES_P (katastrální pracoviště)
    are not správní obvody. Mapped to that level they wrote an office polygon onto every
    Praha správní obvod sharing its number — 51, 60, 78 and 205, the last renamed
    "Kutná Hora" — and 731 `skipped_no_unit`. The pack has no správní obvod layer."""
    tokens = {layer.token for layer in rb.LAYERS}
    assert not tokens & {"STU_P", "PRARES_P", "VO_P", "ZSJ_P"}
    assert "spravni_obvod" not in {layer.level for layer in rb.LAYERS}
    assert "spravni_obvod" not in rb.COMPLETE_LEVELS


def test_render_tolerances_are_finer_for_smaller_units():
    by_level = {layer.level: layer.render_tolerance_m for layer in rb.LAYERS}
    assert by_level["kraj"] > by_level["okres"] > by_level["obec"]
    assert by_level["katastralni_uzemi"] < by_level["obec"]


def test_the_recorded_tolerance_is_the_metric_one_actually_applied():
    """A degree tolerance is ~111.3 km/deg N-S but only ~71.7 km/deg E-W at Czech
    latitudes, so no single degrees->metres factor is honest. Simplify in EPSG:5514 and
    the recorded `generalization_tolerance_m` IS the tolerance applied."""
    obec = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    assert 50 <= obec.render_tolerance_m <= 60  # the ~55 m the production loader uses
    assert not hasattr(obec, "render_tolerance_deg")
    assert "ST_Transform(a.geom, 5514)" in rb._INSERT_RENDER
    assert "%(tolerance_deg)s" not in rb._INSERT_RENDER
    assert rb._INSERT_RENDER.count("%(tolerance_m)s") == 2  # recorded == applied


def test_authoritative_geometry_is_never_simplified():
    assert "'authoritative', 0, 'none'" in rb._INSERT_AUTHORITATIVE
    assert "Simplify" not in rb._INSERT_AUTHORITATIVE


def test_pip_geometry_is_subdivided_from_the_authoritative_row_only():
    assert "ST_Subdivide" in rb._INSERT_PIP
    assert "purpose = 'authoritative'" in rb._INSERT_PIP
    assert rb.SUBDIVIDE_MAX_VERTICES == 256


def test_render_geometry_records_its_tolerance_and_algorithm():
    assert "ST_SimplifyPreserveTopology" in rb._INSERT_RENDER
    assert "%(tolerance_m)s" in rb._INSERT_RENDER


def test_representative_point_is_paired_with_a_containment_radius():
    """An inscribed-circle CENTRE with the max centre-to-boundary distance — never the
    inscribed radius, which understates uncertainty on elongated units."""
    sql = rb._INSERT_AUTHORITATIVE
    assert "ST_MaximumInscribedCircle" in sql
    assert "ST_MaxDistance(c.center, ST_Boundary(c.geom5514))" in sql


def test_pip_pieces_carry_piece_local_diagnostics_only():
    """A subdivided piece must never advertise the WHOLE unit's representative point,
    area or radii: a consumer reading a pip row would get a point outside its own piece
    and an uncertainty radius describing the entire obec (01 §3.3.1)."""
    sql = rb._INSERT_PIP
    for unit_wide in ("a.area_m2", "a.representative_point", "a.inscribed_radius_m",
                      "a.centroid_point", "a.containment_radius_m", "a.max_radius_m"):
        assert unit_wide not in sql
    assert "ST_Area(piece::geography)" in sql
    assert "ST_Centroid(piece)" in sql
    assert "ST_MaximumInscribedCircle(m.piece5514)" in sql
    assert "ST_MaxDistance(c.center, ST_Boundary(m.piece5514))" in sql


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        if "ST_OrderingEquals" in self.conn.executed[-1][0]:
            return None if self.conn.carry_from is None else (self.conn.carry_from,)
        return (self.conn.result,)

    def fetchall(self):
        return list(self.conn.rows)


class _Tx:
    """Records the transaction boundary so a test can prove what commits together."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.executed.append(("BEGIN", None))
        return self

    def __exit__(self, *exc):
        self.conn.executed.append(("COMMIT", None))
        return False


class _Conn:
    def __init__(self, result=None, rows=(), carry_from=None):
        self.result = result
        self.rows = list(rows)
        self.carry_from = carry_from
        self.executed: list[tuple[str, object]] = []
        self.closed = False

    def cursor(self):
        return _Cur(self)

    def transaction(self):
        return _Tx(self)

    def close(self):
        self.closed = True

    def statements(self) -> list[str]:
        return [sql for sql, _ in self.executed]


def test_a_changed_unit_is_derived_three_geometries_and_nothing_else():
    """No carry source (a new unit, or a boundary that moved): authoritative, render and
    pip are derived. No DELETE — a unit only gets here with nothing committed for this
    version — and no `has_polygon` write (the column goes; nothing read it)."""
    conn = _Conn(7)  # unit_id_for -> 7; the carry probe finds nothing equal
    layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    how, upgraded = rb.load_feature(conn, _feature("obec", 554782), layer, 3)
    assert (how, upgraded) == ("derived", True)
    statements = conn.statements()
    assert sum("INSERT INTO ruian_admin_unit_geometries" in s for s in statements) == 3
    assert not any(s.startswith("DELETE") for s in statements)
    assert not any("has_polygon" in s for s in statements)


def test_an_unchanged_unit_is_carried_in_one_insert_select():
    """The pack's geometry equals the unit's newest earlier authoritative row: that
    version's rows are copied — one statement, no ST_MaximumInscribedCircle, no
    ST_Subdivide — and restamped to the version being loaded."""
    conn = _Conn(7, carry_from=2)
    layer = next(x for x in rb.LAYERS if x.token == "KATUZE_P")
    how, _ = rb.load_feature(conn, _feature("katastralni_uzemi", 659673), layer, 3)
    assert how == "carried"
    inserts = [(sql, params) for sql, params in conn.executed
               if "INSERT INTO ruian_admin_unit_geometries" in sql]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert params == {"unit_id": 7, "version_id": 3, "from_version_id": 2}
    assert "ST_MaximumInscribedCircle" not in sql and "ST_Subdivide" not in sql


def test_the_carry_compares_the_exact_expression_the_authoritative_row_was_stored_with():
    """Vertex-exact against the stored row, so an unchanged boundary compares equal and a
    PROJ/GEOS change that moves one coordinate is recomputed rather than carried stale."""
    probe = " ".join(rb._CARRY_SOURCE_SQL.split())
    assert rb._SOURCE_GEOM in rb._CARRY_SOURCE_SQL
    assert rb._SOURCE_GEOM in rb._INSERT_AUTHORITATIVE
    assert "ST_OrderingEquals(prev.geom," in probe
    # the newest EARLIER version holding this unit, never the version being loaded
    assert "a.registry_version_id < %(version_id)s" in probe
    assert "ORDER BY a.registry_version_id DESC LIMIT 1" in probe


def test_the_carry_copies_every_purpose_of_the_source_version_by_index():
    """All three purposes, every pip piece, row for row — located through the two indexes
    that cover them (`pip` rows sit outside both btrees, hence the bbox join) and never by
    a per-unit scan of the whole table."""
    sql = " ".join(rb._CARRY_FORWARD_SQL.split())
    assert "SELECT g.unit_id, %(version_id)s, g.purpose," in sql
    assert "n.purpose <> 'pip'" in sql
    assert "p.purpose = 'pip' AND p.geom && a.geom" in sql
    for column in ("generalization_tolerance_m", "simplify_algorithm", "area_m2",
                   "representative_point", "inscribed_radius_m", "centroid_point",
                   "containment_radius_m", "max_radius_m"):
        assert f"g.{column}" in sql


def test_carried_rows_equal_the_previous_versions_rows():
    """What the INSERT…SELECT writes, applied to rows: every row of the source version,
    restamped, every other column verbatim; nothing from other units or versions."""
    rows = [
        {"id": 1, "unit_id": 7, "registry_version_id": 2, "purpose": "authoritative", "g": "A"},
        {"id": 2, "unit_id": 7, "registry_version_id": 2, "purpose": "render", "g": "R"},
        {"id": 3, "unit_id": 7, "registry_version_id": 2, "purpose": "pip", "g": "P1"},
        {"id": 4, "unit_id": 7, "registry_version_id": 2, "purpose": "pip", "g": "P2"},
        {"id": 5, "unit_id": 7, "registry_version_id": 1, "purpose": "pip", "g": "OLD"},
        {"id": 6, "unit_id": 8, "registry_version_id": 2, "purpose": "pip", "g": "NEIGHBOUR"},
    ]

    def carry(unit_id: int, version_id: int, from_version_id: int) -> list[dict]:
        source = [r for r in rows
                  if r["unit_id"] == unit_id and r["registry_version_id"] == from_version_id]
        return [{**r, "registry_version_id": version_id} for r in source]

    carried = carry(7, 3, 2)
    strip = lambda r: {k: v for k, v in r.items() if k not in ("id", "registry_version_id")}
    assert [strip(r) for r in carried] == [strip(r) for r in rows[:4]]
    assert {r["registry_version_id"] for r in carried} == {3}


def test_a_degenerate_feature_is_counted_not_fatal(monkeypatch):
    layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    monkeypatch.setattr(rb, "LAYERS", (layer,))
    monkeypatch.setattr(rb, "read_layer", lambda d, l: ([_feature("obec", 1)], [999]))
    monkeypatch.setattr(rb, "load_feature", lambda *a, **k: ("derived", True))
    recorded: list[dict] = []
    monkeypatch.setattr(rb.loader_db, "record_discrepancy",
                        lambda conn, v, **kw: recorded.append(kw))
    counts, _ = rb.load_layers(_Conn(), Path("/nonexistent"), version_id=1)
    assert counts == {"loaded": 1, "carried": 0, "skipped_no_unit": 0, "degenerate": 1,
                      "failed": 0, "names": 1, "resumed": 0}
    assert recorded[0]["discrepancy"] == "degenerate_boundary_geometry"
    assert recorded[0]["entity_code"] == 999


def test_a_failing_feature_is_a_discrepancy_row_not_the_end_of_the_pack(monkeypatch):
    layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    monkeypatch.setattr(rb, "LAYERS", (layer,))
    monkeypatch.setattr(
        rb, "read_layer", lambda d, l: ([_feature("obec", 1), _feature("obec", 2)], []))

    def _load(conn, feature, *a, **k):
        if feature.code == 1:
            raise psycopg.errors.InternalError_("GEOSException")
        return "derived", False

    monkeypatch.setattr(rb, "load_feature", _load)
    recorded: list[dict] = []
    monkeypatch.setattr(rb.loader_db, "record_discrepancy",
                        lambda conn, v, **kw: recorded.append(kw))
    counts, _ = rb.load_layers(_Conn(), Path("/nonexistent"), version_id=1)
    assert (counts["loaded"], counts["failed"]) == (1, 1)
    assert recorded[0]["discrepancy"] == "boundary_load_failed"


# --- session death mid-pack: reconnect once, resume, never mask the cause ----------
#
# The 2026-08 boundary run died 45 minutes into OBCE_P (obec 576069) when the single
# long-lived session-pooler connection was dropped ("SSL connection has been closed
# unexpectedly"), and then reported "the connection is closed" from the discrepancy INSERT
# it tried to write on that same dead handle.

def _obec_layer(monkeypatch, features, degenerate=()):
    layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    monkeypatch.setattr(rb, "LAYERS", (layer,))
    monkeypatch.setattr(rb, "read_layer", lambda d, l: (list(features), list(degenerate)))
    return layer


def _dropped() -> psycopg.OperationalError:
    return psycopg.OperationalError("consuming input failed: SSL connection has been "
                                    "closed unexpectedly")


def test_a_dropped_session_is_reconnected_and_the_unit_retried_once(monkeypatch):
    _obec_layer(monkeypatch, [_feature("obec", 1), _feature("obec", 2)])
    dead, fresh = _Conn(), _Conn()
    opened: list[object] = []
    seen: list[tuple[object, int]] = []

    def _load(conn, feature, *a, **k):
        seen.append((conn, feature.code))
        if conn is dead:
            raise _dropped()
        return "derived", False

    monkeypatch.setattr(rb, "load_feature", _load)
    counts, live = rb.load_layers(
        dead, Path("/nonexistent"), version_id=1,
        reconnector=rb.Reconnector(lambda: (opened.append(fresh), fresh)[1]),
    )
    assert len(opened) == 1                       # ONE reconnect, not one per unit
    assert dead.closed and live is fresh          # the dead handle is closed, not leaked
    assert [code for _, code in seen] == [1, 1, 2]  # unit 1 retried, then unit 2 carried on
    assert (counts["loaded"], counts["failed"]) == (2, 0)


def test_a_unit_that_fails_its_retry_is_a_discrepancy_and_the_run_goes_on(monkeypatch):
    _obec_layer(monkeypatch, [_feature("obec", 1), _feature("obec", 2)])
    dead, fresh = _Conn(), _Conn()

    def _load(conn, feature, *a, **k):
        if feature.code == 1:
            raise _dropped()
        return "derived", False

    monkeypatch.setattr(rb, "load_feature", _load)
    recorded: list[tuple[object, dict]] = []
    monkeypatch.setattr(rb.loader_db, "record_discrepancy",
                        lambda conn, v, **kw: recorded.append((conn, kw)))
    counts, live = rb.load_layers(
        dead, Path("/nonexistent"), version_id=1,
        reconnector=rb.Reconnector(lambda: fresh),
    )
    assert (counts["loaded"], counts["failed"]) == (1, 1)
    assert live is fresh
    conn_arg, kwargs = recorded[0]
    # The bookkeeping row never rides the handle we just failed on, and it carries the
    # ORIGINAL error rather than a masking "the connection is closed".
    assert conn_arg is None and kwargs["own_connection"] is True
    assert kwargs["discrepancy"] == "boundary_load_failed"
    assert "SSL connection has been closed" in kwargs["detail"]["error"]


def test_a_non_transient_error_is_not_retried(monkeypatch):
    """A GEOSException is one broken geometry, not a dead session — reconnecting would
    burn the budget on a unit that will fail identically on any connection."""
    _obec_layer(monkeypatch, [_feature("obec", 1)])
    attempts: list[int] = []

    def _load(conn, feature, *a, **k):
        attempts.append(feature.code)
        raise psycopg.errors.InternalError_("GEOSException")

    monkeypatch.setattr(rb, "load_feature", _load)
    monkeypatch.setattr(rb.loader_db, "record_discrepancy", lambda *a, **k: None)
    reconnects: list[int] = []
    counts, _ = rb.load_layers(
        _Conn(), Path("/nonexistent"), version_id=1,
        reconnector=rb.Reconnector(lambda: reconnects.append(1) or _Conn()),
    )
    assert attempts == [1] and reconnects == []
    assert counts["failed"] == 1


def test_the_reconnect_budget_is_bounded_and_aborts_loudly(monkeypatch):
    """Past the budget the environment is broken, not the pack: stop instead of grinding
    through 6,258 obce one reconnect at a time and calling the result a load."""
    _obec_layer(monkeypatch, [_feature("obec", i) for i in range(1, 40)])
    monkeypatch.setattr(rb, "load_feature",
                        lambda *a, **k: (_ for _ in ()).throw(_dropped()))
    monkeypatch.setattr(rb.loader_db, "record_discrepancy", lambda *a, **k: None)
    opened: list[object] = []
    with pytest.raises(rb.loader_db.LoadAborted) as exc:
        rb.load_layers(_Conn(), Path("/nonexistent"), version_id=1,
                       reconnector=rb.Reconnector(
                           lambda: (opened.append(1), _Conn())[1], limit=3))
    assert len(opened) == 3
    assert "reconnects exhausted" in str(exc.value)


def test_all_three_purposes_and_the_name_upgrade_commit_in_one_transaction():
    """The resume fast-path's premise: an `authoritative` row for a registry version
    proves the unit's render + pip rows and its name upgrade committed too, so a unit found
    there can be skipped WHOLE — for a derived unit and a carried one alike."""
    for carry_from, inserts in ((None, 3), (2, 1)):
        conn = _Conn(7, carry_from=carry_from)  # unit_id_for -> 7
        layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
        rb.load_feature(conn, _feature("obec", 554782), layer, 3)
        statements = conn.statements()
        begin, commit = statements.index("BEGIN"), statements.index("COMMIT")
        inside = statements[begin + 1:commit]
        assert sum("INSERT INTO ruian_admin_unit_geometries" in s for s in inside) == inserts
        assert any(s.startswith("UPDATE ruian_admin_units u SET name") for s in inside)
        assert statements.count("BEGIN") == 1  # ONE transaction, so it is all-or-nothing


def test_units_already_loaded_for_this_version_are_skipped_on_resume(monkeypatch):
    _obec_layer(monkeypatch, [_feature("obec", 1), _feature("obec", 2)])
    conn = _Conn(rows=[(1,)])  # obec 1 already has its authoritative row
    loaded: list[int] = []

    def _load(c, feature, *a, **k):
        loaded.append(feature.code)
        return "carried", True

    monkeypatch.setattr(rb, "load_feature", _load)
    counts, _ = rb.load_layers(conn, Path("/nonexistent"), version_id=1)
    assert loaded == [2]
    assert (counts["resumed"], counts["loaded"], counts["carried"]) == (1, 1, 1)
    # ONE done-set query for the layer, not one probe per unit.
    probes = [s for s in conn.statements() if "EXISTS ( SELECT 1 FROM" in s]
    assert len(probes) == 1


def test_the_done_set_is_scoped_to_the_registry_version_purpose_and_level():
    sql = " ".join(rb._DONE_CODES.split())
    assert "g.registry_version_id = %s" in sql
    assert "g.purpose = 'authoritative'" in sql
    assert "u.level::text = %s" in sql
    assert "u.valid_to IS NULL" in sql  # matches unit_id_for's own liveness filter
    # Params in the order `done_codes` binds them: level first, then the version.
    assert sql.index("u.level::text = %s") < sql.index("g.registry_version_id = %s")


def test_a_unit_with_no_mirror_row_is_counted_not_loaded(monkeypatch):
    _obec_layer(monkeypatch, [_feature("obec", 1)])
    monkeypatch.setattr(rb, "load_feature", lambda *a, **k: (None, False))
    counts, _ = rb.load_layers(_Conn(), Path("/nonexistent"), version_id=1)
    assert (counts["loaded"], counts["skipped_no_unit"]) == (0, 1)


def test_completeness_reads_the_versions_membership_and_excuses_only_degenerate_units():
    """Membership is the version's own span, a unit closed BY the version is out, and only
    a `degenerate_boundary_geometry` row excuses a unit — a `boundary_load_failed` one is
    retried by the next run, never waved through."""
    sql = " ".join(rb._MISSING_GEOMETRY_SQL.split())
    assert "u.first_version_id <= %(version_id)s AND u.last_version_id >= %(version_id)s" in sql
    assert "(u.valid_to IS NULL OR u.last_version_id > %(version_id)s)" in sql
    assert "HAVING count(DISTINCT g.purpose) = 2" in sql
    assert "g.purpose IN ('pip', 'authoritative')" in sql
    assert "d.discrepancy = 'degenerate_boundary_geometry'" in sql
    assert "boundary_load_failed" not in sql


def test_missing_geometry_binds_the_complete_levels_and_shapes_its_answer():
    conn = _Conn(rows=[("katastralni_uzemi", 2, [600016, 600024])])
    assert rb.missing_geometry(conn, 3) == [("katastralni_uzemi", 2, [600016, 600024])]
    _, params = conn.executed[-1]
    assert params == {"levels": ["obec", "katastralni_uzemi"], "version_id": 3}


def test_pick_field_is_case_insensitive_and_ordered():
    assert rb._pick_field(["kod", "nazev"], rb._CODE_FIELDS) == "kod"
    assert rb._pick_field(["KOD_KU_", "NAZ_KU"], rb._NAME_FIELDS) == "NAZ_KU"
    assert rb._pick_field(["other"], rb._CODE_FIELDS) is None
