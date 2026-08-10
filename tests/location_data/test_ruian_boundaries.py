"""Boundary pack: layer mapping, the 8-vs-14 assertion, and the three-geometry contract."""

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


def test_default_levels_are_all_real_layers():
    known = {layer.level for layer in rb.LAYERS}
    assert set(rb.DEFAULT_LAYERS) <= known
    # ZSJ (138 MB) is off by default; it is loadable via --levels.
    assert "zsj" not in rb.DEFAULT_LAYERS
    assert "zsj" in known


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


def test_loading_a_boundary_marks_the_unit_as_having_a_polygon():
    assert "UPDATE ruian_admin_units SET has_polygon = true" in rb._MARK_HAS_POLYGON


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
    def __init__(self, result=None, rows=()):
        self.result = result
        self.rows = list(rows)
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


def test_load_feature_writes_the_three_geometries_and_the_has_polygon_flag():
    conn = _Conn(7)  # unit_id_for -> 7
    layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    loaded, upgraded = rb.load_feature(
        conn, _feature("obec", 554782), layer, 3, with_pip=True,
    )
    assert (loaded, upgraded) == (True, True)
    statements = [sql for sql, _ in conn.executed]
    assert any(s.startswith("DELETE FROM ruian_admin_unit_geometries") for s in statements)
    assert sum("INSERT INTO ruian_admin_unit_geometries" in s for s in statements) == 3
    assert any("has_polygon = true" in s for s in statements)


def test_a_missing_pip_check_or_index_is_an_explicit_failure_not_a_pass():
    """A NULL probe means the constraint is ABSENT — loading on would silently degrade
    the containment authority, which is the whole reason the probe exists."""
    with pytest.raises(rb.BoundarySchemaError):
        rb.check_pip_supported(_Conn(None))
    with pytest.raises(rb.BoundarySchemaError):
        rb.check_pip_supported(_Conn("CHECK (purpose = ANY (ARRAY['authoritative','render'])"))


def test_check_pip_supported_passes_on_migration_381s_shape():
    class _Probes(_Conn):
        def __init__(self):
            super().__init__()
            self.answers = [
                "CHECK ((purpose = ANY (ARRAY['authoritative'::text, 'pip'::text, "
                "'render'::text])))",
                "CREATE UNIQUE INDEX ruian_aug_unique_nonpip ON public."
                "ruian_admin_unit_geometries USING btree (unit_id, registry_version_id, "
                "purpose) WHERE (purpose <> 'pip'::text)",
            ]

        def cursor(self):
            self.result = self.answers.pop(0) if self.answers else None
            return _Cur(self)

    rb.check_pip_supported(_Probes())


def test_upgrading_names_triggers_a_gazetteer_rebuild():
    """The pack is the ONLY name source for kraj/okres/ORP/POU/KÚ/ZSJ and the gazetteer
    skips placeholder-named units, so those levels are unsearchable until it is rebuilt."""
    source = inspect.getsource(rb.run)
    assert "name_index.rebuild" in source
    assert 'counts["names"]' in source


def test_a_degenerate_feature_is_counted_not_fatal(monkeypatch):
    layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    monkeypatch.setattr(rb, "LAYERS", (layer,))
    monkeypatch.setattr(rb, "read_layer", lambda d, l: ([_feature("obec", 1)], [999]))
    monkeypatch.setattr(rb, "load_feature", lambda *a, **k: (True, True))
    recorded: list[dict] = []
    monkeypatch.setattr(rb.loader_db, "record_discrepancy",
                        lambda conn, v, **kw: recorded.append(kw))
    counts, _ = rb.load_layers(_Conn(), Path("/nonexistent"), levels=("obec",), version_id=1,
                               with_pip=True)
    assert counts == {"loaded": 1, "skipped_no_unit": 0, "degenerate": 1, "failed": 0,
                      "names": 1, "resumed": 0}
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
        return True, False

    monkeypatch.setattr(rb, "load_feature", _load)
    recorded: list[dict] = []
    monkeypatch.setattr(rb.loader_db, "record_discrepancy",
                        lambda conn, v, **kw: recorded.append(kw))
    counts, _ = rb.load_layers(_Conn(), Path("/nonexistent"), levels=("obec",), version_id=1,
                               with_pip=True)
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
        return True, False

    monkeypatch.setattr(rb, "load_feature", _load)
    counts, live = rb.load_layers(
        dead, Path("/nonexistent"), levels=("obec",), version_id=1, with_pip=True,
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
        return True, False

    monkeypatch.setattr(rb, "load_feature", _load)
    recorded: list[tuple[object, dict]] = []
    monkeypatch.setattr(rb.loader_db, "record_discrepancy",
                        lambda conn, v, **kw: recorded.append((conn, kw)))
    counts, live = rb.load_layers(
        dead, Path("/nonexistent"), levels=("obec",), version_id=1, with_pip=True,
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
        _Conn(), Path("/nonexistent"), levels=("obec",), version_id=1, with_pip=True,
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
        rb.load_layers(_Conn(), Path("/nonexistent"), levels=("obec",), version_id=1,
                       with_pip=True,
                       reconnector=rb.Reconnector(
                           lambda: (opened.append(1), _Conn())[1], limit=3))
    assert len(opened) == 3
    assert "reconnects exhausted" in str(exc.value)


def test_all_three_purposes_and_the_name_upgrade_commit_in_one_transaction():
    """The resume fast-path's premise: an `authoritative` row for a registry version
    proves the unit's render + pip rows, its name upgrade and its has_polygon flag
    committed too, so a unit found there can be skipped WHOLE."""
    conn = _Conn(7)  # unit_id_for -> 7
    layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    rb.load_feature(conn, _feature("obec", 554782), layer, 3, with_pip=True)
    statements = conn.statements()
    begin, commit = statements.index("BEGIN"), statements.index("COMMIT")
    inside = statements[begin + 1:commit]
    assert sum("INSERT INTO ruian_admin_unit_geometries" in s for s in inside) == 3
    assert any(s.startswith("UPDATE ruian_admin_units u SET name") for s in inside)
    assert any("has_polygon = true" in s for s in inside)
    assert any(s.startswith("DELETE FROM ruian_admin_unit_geometries") for s in inside)
    assert statements.count("BEGIN") == 1  # ONE transaction, so it is all-or-nothing


def test_units_already_loaded_for_this_version_are_skipped_on_resume(monkeypatch):
    _obec_layer(monkeypatch, [_feature("obec", 1), _feature("obec", 2)])
    conn = _Conn(rows=[(1,)])  # obec 1 already has its authoritative row
    loaded: list[int] = []

    def _load(c, feature, *a, **k):
        loaded.append(feature.code)
        return True, True

    monkeypatch.setattr(rb, "load_feature", _load)
    counts, _ = rb.load_layers(conn, Path("/nonexistent"), levels=("obec",), version_id=1,
                               with_pip=True)
    assert loaded == [2]
    assert (counts["resumed"], counts["loaded"]) == (1, 1)
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


def test_no_resume_reloads_everything(monkeypatch):
    _obec_layer(monkeypatch, [_feature("obec", 1), _feature("obec", 2)])
    conn = _Conn(rows=[(1,)])
    loaded: list[int] = []
    monkeypatch.setattr(rb, "load_feature",
                        lambda c, f, *a, **k: (loaded.append(f.code), (True, True))[1])
    counts, _ = rb.load_layers(conn, Path("/nonexistent"), levels=("obec",), version_id=1,
                               with_pip=True, resume=False)
    assert loaded == [1, 2] and counts["resumed"] == 0
    assert not [s for s in conn.statements() if "EXISTS ( SELECT 1 FROM" in s]


def test_a_resumed_run_still_rebuilds_the_gazetteer():
    """A skipped unit was name-upgraded by the pass that died — which by definition never
    reached the rebuild, so `names == 0` on the resume must not mean `no rebuild`."""
    source = inspect.getsource(rb.run)
    assert 'if (counts["names"] or counts["resumed"]) and not skip_gazetteer' in source


def test_the_live_connection_is_the_one_that_gets_closed():
    """`load_layers` may hand back a different connection than it was given; closing the
    dead original would leak the fresh session-mode backend it replaced."""
    source = inspect.getsource(rb.run)
    assert "counts, conn = load_layers(" in source
    assert "with loader_db.open_loader_connection() as conn" not in source
    assert "finally:\n        conn.close()" in source


def test_pick_field_is_case_insensitive_and_ordered():
    assert rb._pick_field(["kod", "nazev"], rb._CODE_FIELDS) == "kod"
    assert rb._pick_field(["KOD_KU_", "NAZ_KU"], rb._NAME_FIELDS) == "NAZ_KU"
    assert rb._pick_field(["other"], rb._CODE_FIELDS) is None
