"""The model-first challenger (`autodedup/challenger/`, B-b): its three modules, its lab rungs, and
the proof that the live lane cannot import it while it is lab-only."""

from __future__ import annotations

import ast
import dataclasses
import json
import math
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autodedup import evidence, guards
from autodedup.challenger import facts as mf_facts
from autodedup.challenger.facts import READERS, Dials, stated_difference
from autodedup.challenger.score import FORMAT, _isotonic, scorer
from autodedup.challenger.union import constrained_union
from autodedup.dataset import Listing, Location
from autodedup.indistinguishable import Fact, cellar_area_conflict
from autodedup.settings import Settings

REPO = Path(__file__).resolve().parents[2]
W31 = Settings.from_json(REPO / "autodedup" / "settings" / "w31.json")
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "challenger_gbt.json"
STAMP = "2026-09-01T00:00:00+00:00"


def _listing(i: int, **kw: Any) -> Listing:
    base: dict[str, Any] = {"id": i, "block": "town1", "source": "sreality",
                            "category_type": "prodej", "category_main": "byt",
                            "first_seen_at": "2026-08-01T00:00:00+00:00",
                            "last_seen_at": "2026-08-02T00:00:00+00:00"}
    base.update(kw)
    return Listing(**base)


def _fact(*pairs: Listing, dials: Dials = Dials()) -> Any:
    return stated_difference({x.id: x for x in pairs}, W31, dials)


# --- facts ----------------------------------------------------------------------------------------

def test_a_fact_reads_both_sides_or_nothing() -> None:
    a, b = _listing(1, area_m2=50.0, floor=2, disposition="2+kk"), _listing(2)
    assert _fact(a, b)(1, 2) is None


@pytest.mark.parametrize(("left", "right", "want"), [
    ({"category_type": "prodej"}, {"category_type": "pronajem"}, "deal"),
    ({"category_main": "byt"}, {"category_main": "komercni"}, "kind"),
    ({"category_main": "dum"}, {"category_main": "komercni"}, None),
    ({"area_m2": 50.0}, {"area_m2": 55.0}, "area"),
    ({"area_m2": 50.0}, {"area_m2": 53.0}, None),
    ({"disposition": "2+kk"}, {"disposition": "3+kk"}, "disposition"),
    ({"floor": 2}, {"floor": 4}, "floor"),
    ({"floor": 2}, {"floor": 3}, None),
])
def test_the_typed_facts(left: dict[str, Any], right: dict[str, Any], want: str | None) -> None:
    a = _listing(1, source="sreality", **left)
    b = _listing(2, source="idnes", **right)
    assert _fact(a, b)(1, 2) == want


def test_co_live_on_one_portal_a_smaller_gap_is_stated() -> None:
    together = {"first_seen_at": "2026-08-01T00:00:00+00:00",
                "last_seen_at": "2026-08-05T00:00:00+00:00"}
    for key, left, right in (("area", 50.0, 51.0), ("floor", 2, 3)):
        field = "area_m2" if key == "area" else "floor"
        one = _fact(_listing(1, **{field: left}, **together), _listing(2, **{field: right}, **together))
        two = _fact(_listing(1, **{field: left}, **together),
                    _listing(2, source="idnes", **{field: right}, **together))
        assert one(1, 2) == key
        assert two(1, 2) is None


def test_price_paths_that_never_meet_while_co_live() -> None:
    together = {"first_seen_at": "2026-08-01T00:00:00+00:00",
                "last_seen_at": "2026-08-05T00:00:00+00:00"}
    a = _listing(1, price=4_000_000.0, **together)
    assert _fact(a, _listing(2, source="idnes", price=4_600_000.0, **together))(1, 2) == "price"
    assert _fact(a, _listing(2, source="idnes", price=4_100_000.0, **together))(1, 2) is None
    assert _fact(a, _listing(2, source="sreality", price=4_100_000.0, **together))(1, 2) == "price"
    sequential = _listing(2, source="idnes", price=4_600_000.0,
                          first_seen_at="2026-08-10T00:00:00+00:00",
                          last_seen_at="2026-08-12T00:00:00+00:00")
    assert _fact(a, sequential)(1, 2) is None
    per_m2 = _listing(2, source="idnes", price=80_000.0, **together)
    assert _fact(a, per_m2)(1, 2) is None


def test_the_street_limb_is_dropped_and_the_unit_is_read() -> None:
    a = _listing(1, location=Location(obec_kod=1, street_key="x|a"))
    b = _listing(2, location=Location(obec_kod=1, street_key="x|b"))
    assert _fact(a, b)(1, 2) is None
    one = Location(obec_kod=1, street_key="x|a", house_number="5")
    c = _listing(1, location=one, description="Prodej bytu, byt c. 3 ve druhem podlazi.")
    d = _listing(2, location=one, description="Prodej bytu, byt c. 7 ve druhem podlazi.")
    assert _fact(c, d)(1, 2) == "unit"
    assert mf_facts.unit_designator_conflict is guards.unit_designator_conflict


def test_the_readers_are_the_ladders_own_and_no_feature_reaches_them(monkeypatch: Any) -> None:
    seen: list[Any] = []

    def fake(a: Listing, b: Listing, feats: Any, settings: Settings, mode: str) -> list[Fact]:
        seen.append((feats, settings.d43_body_align, mode))
        return [Fact("interior", "x", "y"), Fact("floorplan", "x", "y"), Fact("street", "x", "y"),
                Fact(names.pop(), "x", "y")]

    monkeypatch.setattr(mf_facts, "distinguishing_facts", fake)
    fact = _fact(_listing(1), _listing(2))
    names = ["cellar_area"]
    assert fact(1, 2) == "cellar_area"
    assert seen[-1] == (None, False, "cluster")
    names = ["agency_code"]
    assert fact(1, 2) is None
    assert len(READERS) == 14 and not set(READERS) & {"interior", "floorplan", "street"}


def test_a_photograph_never_excuses_a_stated_fact() -> None:
    """E305r lets four tight frames excuse a cellar two bodies state differently (the w31 row keeps
    it on); the challenger reads the two adverts alone, so the stated cellar keeps them apart (K-P).
    The trial's 13799553 x 18580359: one rental, 4 vs 3.3 m2."""
    body = "Pronajem bytu 1+kk o vymere 32 m2 v Jablonci, k bytu patri sklepni koje {} m2."
    a = _listing(1, category_type="pronajem", price=22_000.0, description=body.format("4"))
    b = _listing(2, category_type="pronajem", price=22_000.0, source="idnes",
                 description=body.format("3,3"))
    frames = {"phash_tight_matches": (5.0, True)}
    assert W31.d43_cellar_area_photo_yield
    assert cellar_area_conflict(a, b, W31, None) is not None
    assert cellar_area_conflict(a, b, W31, frames) is None
    assert _fact(a, b, dials=Dials(body_align=1.0))(1, 2) == "cellar_area"


def test_a_real_reader_fires_through_distinguishing_facts() -> None:
    a = _listing(1, description="Byt 2+kk o vymere 54 m2 se nachazi v prizemi cihloveho domu.")
    b = _listing(2, description="Byt 2+kk o vymere 45 m2 se nachazi v prizemi cihloveho domu.")
    assert _fact(a, b)(1, 2) in {"printed_area", "body_align"}
    assert _fact(a, b, dials=Dials(body_align=1.0))(1, 2) == "printed_area"


def test_the_two_fixed_inputs_outside_the_dials() -> None:
    """The dial census: besides the ten Dials, the area fact reads the settings row's
    `d43_block_plot_area` (a parcel's printed plot fills its empty area) and body_align always
    heals."""
    a = _listing(1, category_main="pozemek", description="Prodej pozemku, celková plocha (m2): 312")
    b = _listing(2, category_main="pozemek", description="Prodej pozemku, celková plocha (m2): 500")
    off = dataclasses.replace(W31, d43_block_plot_area=False)
    assert W31.d43_block_plot_area and W31.d43_body_align_heal
    assert mf_facts._typed(a, b, W31, Dials()) == "area"
    assert mf_facts._typed(a, b, off, Dials()) is None
    assert mf_facts.BODY_ALIGN_HEAL is True
    assert len(dataclasses.fields(Dials)) == 10


# --- score ----------------------------------------------------------------------------------------

def _stump() -> dict[str, Any]:
    return {"format": FORMAT, "feature_order": ["f0", "f1"], "inputs": [0, 1], "fill": {"1": -1.0},
            "baseline": 0.5,
            "trees": [[[0, 1.5, True, 1, 2, 0.0, False], [0, 0.0, False, 0, 0, -1.0, True],
                       [0, 0.0, False, 0, 0, 2.0, True]],
                      [[1, 0.0, False, 1, 2, 0.0, False], [0, 0.0, False, 0, 0, -0.25, True],
                       [0, 0.0, False, 0, 0, 0.25, True]]],
            "calibration": None, "card": {}}


def test_the_tree_walk_by_hand() -> None:
    p = scorer(_stump())
    expit = lambda z: 1.0 / (1.0 + math.exp(-z))  # noqa: E731
    assert p([1.0, 1.0]) == expit(0.5 - 1.0 + 0.25)
    assert p([2.0, None]) == expit(0.5 + 2.0 - 0.25)
    assert p([None, float("nan")]) == expit(0.5 - 1.0 - 0.25)
    calibrated = _stump() | {"calibration": {"x": [0.1, 0.9], "y": [0.0, 1.0], "lo": 0.1, "hi": 0.9}}
    q = scorer(calibrated)
    low = expit(0.5 - 1.0 - 0.25)
    assert q([2.0, None]) == 1.0
    assert q([None, None]) == (1.0 - 0.0) / (0.9 - 0.1) * (low - 0.1) + 0.0
    with pytest.raises(ValueError):
        scorer({"format": "other"})


def test_a_plateau_reads_its_knot_value_exactly() -> None:
    """numpy.interp's form: two raw sums one ulp apart on an isotonic plateau read the knot value
    itself, so they tie exactly and no tie is reordered by the last bits of exp; a knot reads its
    own value."""
    plateau = _stump() | {"trees": [], "calibration": {"x": [0.0, 0.5, 1.0], "y": [0.3, 0.3, 0.9],
                                                       "lo": 0.0, "hi": 1.0}}
    p = scorer(plateau | {"baseline": -0.3})
    q = scorer(plateau | {"baseline": math.nextafter(-0.3, 0.0)})
    assert p([None, None]) == q([None, None]) == 0.3
    xs, ys = [0.1, 0.35, 0.7, 0.9], [0.0, 0.2 / 3.0, 0.61, 1.0]
    assert [_isotonic(x, xs, ys, 0.1, 0.9) for x in xs] == ys
    assert _isotonic(0.05, xs, ys, 0.1, 0.9) == 0.0 and _isotonic(0.95, xs, ys, 0.1, 0.9) == 1.0


def _vectors(n: int, width: int, seed: int) -> list[list[float | None]]:
    rng = random.Random(seed)
    return [[None if rng.random() < 0.2 else round(rng.gauss(0.0, 1.0), 6) for _ in range(width)]
            for _ in range(n)]


def test_the_committed_model_file_equals_scikit_learn_bit_for_bit() -> None:
    """CI (no scikit-learn): a model exported from a fitted HistGradientBoostingClassifier and
    isotonic map, and scikit-learn's own predictions on 1,000 vectors, both committed."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    p = scorer(fixture["model"])
    got = [p(x) for x in fixture["vectors"]]
    assert len(got) == 1000 and got == fixture["sklearn_p"]
    assert fixture["model"]["calibration"] and fixture["model"]["fill"]


def _fit_fixture(seed: int = 20260928) -> tuple[dict[str, Any], list[list[float | None]], list[float]]:
    np = pytest.importorskip("numpy")
    pytest.importorskip("sklearn")
    from autodedup.lab import mf_fit

    width = 8
    train = _vectors(3000, width, seed + 1)
    X = np.array([[np.nan if v is None else v for v in row] for row in train])
    X[:, 5] = np.where(np.isnan(X[:, 5]), np.nan, 1.0)
    X[:, 7] = np.nan
    rng = random.Random(seed + 2)
    y = np.array([int(np.nan_to_num(r[0]) + 0.5 * np.nan_to_num(r[1]) - (r[5] == 1.0)
                      + rng.gauss(0.0, 0.7) > 0) for r in X])
    w = np.array([rng.choice((0.3, 0.6, 1.0)) for _ in range(len(y))])
    fitted = mf_fit.fit_model(X, y, w, mf_fit.PARAMS | {"max_iter": 60})
    iso = mf_fit.isotonic(fitted.raw(X), y, w)
    model = mf_fit.export(fitted, [f"f{j}" for j in range(width)], iso, {"fixture": seed})
    test = _vectors(1000, width, seed)
    T = np.array([[np.nan if v is None else v for v in row] for row in test])
    T[:, 5] = np.where(np.isnan(T[:, 5]), np.nan, 1.0)
    vectors = [[None if v != v else float(v) for v in row] for row in T.tolist()]
    return model, vectors, iso.predict(fitted.raw(T)).tolist()


def test_pure_python_equals_scikit_learn_on_1000_vectors() -> None:
    """Local (training extra): fit, export, evaluate, bit for bit; calibration and a presence-only
    column in."""
    model, vectors, want = _fit_fixture()
    p = scorer(model)
    assert [p(x) for x in vectors] == want
    assert model["fill"] == {"5": 0.0} and 7 not in model["inputs"]


# --- union ----------------------------------------------------------------------------------------

def test_edges_join_in_descending_p_with_complete_link_facts() -> None:
    facts = {(1, 3): "floor"}
    groups = constrained_union([(1, 2, 0.9), (2, 3, 0.95)], lambda a, b: facts.get((a, b)),
                               {}, 0.2)
    assert groups == [(2, 3)]


def test_learned_negatives_and_must_not_links_refuse_a_union() -> None:
    edges = [(1, 2, 0.9), (2, 3, 0.95)]
    assert constrained_union(edges, lambda a, b: None, {(1, 3): 0.1}, 0.2) == [(2, 3)]
    assert constrained_union(edges, lambda a, b: None, {(1, 3): 0.3}, 0.2) == [(1, 2, 3)]
    assert constrained_union(edges, lambda a, b: None, {}, 0.2, must_not_link=[(3, 1)]) == [(2, 3)]


def test_a_must_link_crosses_any_fact_but_rule_15() -> None:
    facts = {(1, 2): "area", (1, 3): "deal", (2, 3): None}
    stated = lambda a, b: facts.get((a, b))  # noqa: E731
    assert constrained_union([], stated, {}, 0.2, must_link=[(1, 2)]) == [(1, 2)]
    assert constrained_union([], stated, {}, 0.2, must_link=[(1, 2), (2, 3)]) == [(1, 2)]


# --- the lab rungs ----------------------------------------------------------------------------

def _lab_cohort(tmp_path: Path) -> Any:
    np = pytest.importorskip("numpy")
    from autodedup.features import FEATURE_ORDER
    from autodedup.lab.cache import FIDX, Cohort

    listings = {i: _listing(i, source="sreality" if i % 2 else "idnes", area_m2=50.0)
                for i in range(1, 8)}
    listings[6].area_m2 = 80.0
    keys = [(1, 2), (2, 3), (1, 3), (4, 5), (5, 6), (6, 7)]
    n, width = len(keys), len(FEATURE_ORDER)
    V, P = np.zeros((n, width)), np.zeros((n, width), dtype=bool)
    j = FIDX["same_source"]
    V[:, j], P[:, j] = [3.0, 3.0, 0.1, 3.0, 3.0, 3.0], True
    sig = {name: np.array([""] * n, dtype=object)
           for name in ("veto", "gate", "warrant", "refusal", "corro")}
    c = Cohort("toy", {}, SimpleNamespace(listings=listings), W31, None, {}, keys,
               [frozenset()] * n, V, P, sig, {}, "toy", tmp_path)
    model = {"format": FORMAT, "feature_order": list(FEATURE_ORDER), "inputs": [j], "fill": {},
             "baseline": 0.0, "trees": [[[0, 1.0, False, 1, 2, 0.0, False],
                                         [0, 0.0, False, 0, 0, -3.0, True],
                                         [0, 0.0, False, 0, 0, 3.0, True]]],
             "calibration": None, "card": {"toy": True}}
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "town1.json").write_text(json.dumps(model))
    return c


def test_the_rungs_and_the_union_on_the_board(tmp_path: Path) -> None:
    from autodedup.lab import board

    c = _lab_cohort(tmp_path)
    config = {"model": {"const": 0.0},
              "ladder": [{"rung": "veto"},
                         {"rung": "mf_score", "model": str(tmp_path / "models" / "{block}.json"),
                          "t_merge": 0.8, "t_band": 0.2},
                         {"rung": "facts"}],
              "group": {"step": "mf_union", "t_neg": 0.2}}
    out = board.run(c, config)
    zones = [board.ZONE_NAMES[z] for z in out.decisions.zone]
    assert zones == ["merge", "merge", "reject", "merge", "veto", "veto"]
    assert out.decisions.name[4] == "area" and out.decisions.rung[4] == "fact"
    assert out.groups.clusters == {1: (1, 2), 4: (4, 5)}
    assert out.scorer["kind"] == "mf" and out.scorer["cards"] == {"town1": {"toy": True}}
    assert (tmp_path / "mf_scores").is_dir()


def test_the_rulings_bind_in_the_union_and_rule_15_still_holds(tmp_path: Path) -> None:
    from autodedup.lab import board

    c = _lab_cohort(tmp_path)
    c.ds.listings[7].category_type = "pronajem"
    labels = tmp_path / "labels"
    labels.mkdir()
    rows = [{"listing_lo": 5, "listing_hi": 6, "verdict": "same", "decided_at": STAMP},
            {"listing_lo": 6, "listing_hi": 7, "verdict": "same", "decided_at": STAMP},
            {"listing_lo": 1, "listing_hi": 2, "verdict": "different", "decided_at": STAMP}]
    (labels / "operator_labels.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    config = {"model": {"const": 0.0},
              "ladder": [{"rung": "veto"},
                         {"rung": "mf_score", "model": str(tmp_path / "models" / "{block}.json")},
                         {"rung": "facts"}],
              "group": {"step": "mf_union", "t_neg": 0.2, "rulings": str(labels)}}
    out = board.run(c, config)
    assert out.groups.clusters == {2: (2, 3), 5: (5, 6)}
    assert out.groups.stats == {"edges": 3, "must_link": 2, "must_not_link": 1,
                                "must_link_apart": 1, "must_link_apart_rule_15": 1}


def _config(tmp_path: Path, facts: dict[str, Any] | None = None,
            group: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"model": {"const": 0.0},
            "ladder": [{"rung": "veto"},
                       {"rung": "mf_score", "model": str(tmp_path / "models" / "{block}.json")},
                       {"rung": "facts", **(facts or {})}],
            "group": {"step": "mf_union", "t_neg": 0.2, **(group or {})}}


def test_a_cross_town_pair_is_scored_by_its_town_sets_model(tmp_path: Path) -> None:
    """`{block}` is the pair's town set: a pair spanning two towns needs the model sealed off both
    (`town1+town2.json`), never the lo advert's town's."""
    from autodedup.lab import board
    from autodedup.lab.challenger import town_set

    c = _lab_cohort(tmp_path)
    c.ds.listings[7].block = "town2"
    assert town_set("town2", "town1") == town_set("town1", "town2") == "town1+town2"
    with pytest.raises(FileNotFoundError, match=r"town1\+town2"):
        board.run(c, _config(tmp_path))
    model = json.loads((tmp_path / "models" / "town1.json").read_text())
    model["card"] = {"set": "town1+town2"}
    (tmp_path / "models" / "town1+town2.json").write_text(json.dumps(model))
    out = board.run(c, _config(tmp_path))
    assert set(out.scorer["files"]) == {"town1", "town1+town2"}
    assert out.scorer["cards"]["town1+town2"] == {"set": "town1+town2"}


def test_a_model_is_refused_unless_it_reads_the_cohorts_vector(tmp_path: Path) -> None:
    """A model trained with inputs the cohort does not supply is refused, never scored with them
    missing."""
    from autodedup.lab import board

    c = _lab_cohort(tmp_path)
    path = tmp_path / "models" / "town1.json"
    model = json.loads(path.read_text())
    for order in (model["feature_order"] + ["head_room_kitchen"], model["feature_order"][:-1]):
        path.write_text(json.dumps(model | {"feature_order": order}))
        with pytest.raises(ValueError, match="features are not the"):
            board.run(c, _config(tmp_path))


def test_the_fact_dials_are_set_once_for_pair_and_group_grain(tmp_path: Path) -> None:
    """The facts rung's dials are the union's too; the union refuses dials of its own."""
    from autodedup.lab import board

    c = _lab_cohort(tmp_path)
    wide = {"dials": {"area": 0.7, "colive_area": 0.7}}
    out = board.run(c, _config(tmp_path, facts=wide))
    assert [board.ZONE_NAMES[z] for z in out.decisions.zone][3:] == ["merge", "merge", "merge"]
    assert out.groups.clusters == {1: (1, 2), 4: (4, 5, 6, 7)}
    assert out.scorer["fact_dials"] == wide["dials"]
    assert board.run(c, _config(tmp_path)).groups.clusters == {1: (1, 2), 4: (4, 5)}
    with pytest.raises(ValueError, match="facts rung's dials"):
        board.run(c, _config(tmp_path, facts=wide, group=wide))


def test_the_memos_key_on_the_adapter_too(monkeypatch: Any, tmp_path: Path) -> None:
    from autodedup.lab import challenger as lab_challenger

    before = lab_challenger.challenger_code()
    edited = tmp_path / "challenger.py"
    edited.write_bytes(lab_challenger.ADAPTER.read_bytes() + b"\n# edited\n")
    monkeypatch.setattr(lab_challenger, "ADAPTER", edited)
    assert lab_challenger.challenger_code() != before


# --- mf-fit: the town seal, the map off the scored cohort, the seals, lab grounds only ---------

def _ground_file(path: Path, name: str, towns: list[str], n: int, seed: int,
                 settings: str = "s1", spans: list[str] | None = None) -> Path:
    np = pytest.importorskip("numpy")
    from autodedup.lab import ground as lab_ground

    rng = np.random.default_rng(seed)
    V = rng.normal(size=(n, 3))
    y = (V[:, 0] + rng.normal(scale=0.5, size=n) > 0).astype(np.int64)
    lo = [towns[i % len(towns)] for i in range(n)]
    hi = [towns[(i // len(towns)) % len(towns)] for i in range(n)]
    arrays = {"keys": np.array([(seed * 10_000 + 2 * i, seed * 10_000 + 2 * i + 1)
                                for i in range(n)], dtype=np.int64),
              "V": V, "P": np.ones((n, 3), dtype=bool), "y": y, "w": np.ones(n),
              "origin": np.array(["operator" if i % 2 else "w6" for i in range(n)]),
              "src": np.array(["op_explicit"] * n), "blocks": np.array(list(zip(lo, hi))),
              "towns": np.array(sorted(towns)), "spans": np.array(sorted(spans or [])),
              "feature_order": np.array(["f0", "f1", "f2"])}
    meta = {"builder": lab_ground.BUILDER, "cohort": name, "digest": lab_ground.digest(arrays),
            "settings": settings}
    np.savez(path, **arrays, meta=np.array(json.dumps(meta)))
    return path


def test_a_town_is_sealed_in_every_ground_and_the_loco_map_reads_no_scored_row(
        tmp_path: Path) -> None:
    pytest.importorskip("sklearn")
    from autodedup.lab import mf_fit

    a = mf_fit.load_ground("a", _ground_file(tmp_path / "a.npz", "a", ["town1", "town2"], 160, 1))
    b = mf_fit.load_ground("b", _ground_file(tmp_path / "b.npz", "b", ["town1", "town3"], 160, 2))
    report = mf_fit.sealed([a, b], ["a"], "all", "loco", tmp_path / "models")
    shared = report["cohorts"]["a"]["town1"]["trained_on"]
    touch_a = int(((a.blocks[:, 0] == "town1") | (a.blocks[:, 1] == "town1")).sum())
    touch_b = int(((b.blocks[:, 0] == "town1") | (b.blocks[:, 1] == "town1")).sum())
    assert touch_b > 0 and shared["excluded"] == {"a": touch_a, "b": touch_b}
    assert shared["rows"] == 320 - touch_a - touch_b
    assert report["maps"]["a"]["cohorts"] == ["b"]
    assert not report["maps"]["a"]["reads_the_scored_cohort"]
    model = json.loads((tmp_path / "models" / "all" / "a" / "town1.json").read_text())
    assert model["card"]["calibration"]["mode"] == "loco"
    assert model["card"]["grounds"] == {"a": a.meta["digest"], "b": b.meta["digest"]}
    pooled = mf_fit.sealed([a, b], ["a"], "all", "pooled", tmp_path / "pooled")
    assert pooled["maps"]["a"]["reads_the_scored_cohort"]


def test_no_labelled_row_is_scored_by_a_model_that_saw_a_label_on_either_of_its_towns(
        tmp_path: Path, monkeypatch: Any) -> None:
    """The review's cross-town leak: a pair spanning town1 and town2 was scored by town1's model,
    which trained on labels touching town2. Every raw score the maps read now comes from a model
    that saw no label on either town of the row, and every set a candidate spans (`spans`) gets its
    own file (one no label spans is checked against scikit-learn on the ground's rows)."""
    pytest.importorskip("sklearn")
    from autodedup.lab import mf_fit

    a = mf_fit.load_ground("a", _ground_file(tmp_path / "a.npz", "a", ["town1", "town2"], 160, 1,
                                             spans=["town1", "town1+town2", "town2"]))
    b = mf_fit.load_ground("b", _ground_file(tmp_path / "b.npz", "b", ["town1", "town3"], 160, 2,
                                             spans=["town1+town3", "town4+town9"]))
    towns_of = {g.V[r].tobytes(): set(g.blocks[r].tolist()) for g in (a, b) for r in range(len(g.y))}
    fit, raw, check = mf_fit.fit_model, mf_fit.Fitted.raw, mf_fit.equivalence
    scored: list[int] = []
    checking: list[bool] = []

    def spy_fit(X: Any, y: Any, w: Any, params: Any = mf_fit.PARAMS) -> Any:
        fitted = fit(X, y, w, params)
        fitted.saw = set().union(*(towns_of[row.tobytes()] for row in X))
        return fitted

    def spy_raw(self: Any, X: Any) -> Any:
        if not checking:
            for row in X:
                assert not towns_of[row.tobytes()] & self.saw
            scored.append(len(X))
        return raw(self, X)

    def spy_check(*args: Any) -> float:
        checking.append(True)
        try:
            return check(*args)
        finally:
            checking.pop()

    monkeypatch.setattr(mf_fit, "fit_model", spy_fit)
    monkeypatch.setattr(mf_fit.Fitted, "raw", spy_raw)
    monkeypatch.setattr(mf_fit, "equivalence", spy_check)
    report = mf_fit.sealed([a, b], ["a", "b"], "all", "loco", tmp_path / "models")
    assert sum(scored) == 320
    assert sorted(report["cohorts"]["a"]) == ["town1", "town1+town2", "town2"]
    assert sorted(report["cohorts"]["b"]) == ["town1", "town1+town3", "town3", "town4+town9"]
    cross = report["cohorts"]["a"]["town1+town2"]
    assert cross["trained_on"]["excluded"] == {"a": 160, "b": int((b.blocks == "town1").any(1).sum())}
    assert cross["equivalence_rows"] == int((a.sets == "town1+town2").sum()) > 0
    card = json.loads((tmp_path / "models" / "all" / "a" / "town1+town2.json").read_text())["card"]
    assert card["town_set"] == "town1+town2" and "town1 or town2" in card["sealed"]
    assert card["equivalence"]["rows_of"] == "town1+town2"
    empty = json.loads((tmp_path / "models" / "all" / "b" / "town4+town9.json").read_text())["card"]
    assert empty["equivalence"]["rows"] == 160 and empty["equivalence"]["max_abs_dp"] <= 1e-9


def test_mf_fit_trains_only_on_lab_grounds_off_no_sealed_block(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    from autodedup import evidence
    from autodedup.lab import mf_fit

    path = _ground_file(tmp_path / "g.npz", "g", ["town586021", "town2"], 40, 3)
    g = mf_fit.load_ground("g", path)
    with pytest.raises(mf_fit.NotALabGround):
        mf_fit.load_ground("other", path)
    data = dict(np.load(path))
    data["y"] = 1 - data["y"]
    np.savez(tmp_path / "edited.npz", **data)
    with pytest.raises(mf_fit.NotALabGround):
        mf_fit.load_ground("g", tmp_path / "edited.npz")
    data = dict(np.load(path))
    del data["spans"]
    np.savez(tmp_path / "old.npz", **data)
    with pytest.raises(mf_fit.NotALabGround, match="town sets"):
        mf_fit.load_ground("g", tmp_path / "old.npz")
    del data["meta"]
    np.savez(tmp_path / "bare.npz", **data)
    with pytest.raises(mf_fit.NotALabGround):
        mf_fit.load_ground("g", tmp_path / "bare.npz")
    seal = tmp_path / "preregistration_cohort99.json"
    seal.write_text(json.dumps({"blocks": {"spelling": "town:586021"}}))
    with pytest.raises(evidence.SealedExport, match="town:586021"):
        mf_fit.check_seals([g], {"seals": [str(seal)]})
    mf_fit.check_seals([g], {"seals": [str(seal)], "cohorts": {"g": {"freeze": "addendum"}}})
    other = mf_fit.load_ground("h", _ground_file(tmp_path / "h.npz", "h", ["town3"], 40, 4, "s2"))
    with pytest.raises(ValueError, match="settings rows"):
        mf_fit.check_seals([g, other], {})


# --- lab-only ---------------------------------------------------------------------------------

def _closure(roots: tuple[str, ...]) -> set[str]:
    """Every repo module (and third-party top-level name) the roots import, transitively."""
    seen: set[str] = set()
    third: set[str] = set()
    todo = [REPO / root for root in roots]
    while todo:
        path = todo.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                found = evidence._module_file(name)
                if found is not None:
                    todo.append(found)
                elif not name.startswith(("autodedup", "toolkit")):
                    third.add(name.split(".")[0])
    return {str(p.relative_to(REPO)) for p in seen} | {f"third:{t}" for t in third}


def test_the_live_lane_imports_nothing_from_the_challenger() -> None:
    lane = _closure(("autodedup/incremental_lane.py", "autodedup/reconcile.py",
                     "autodedup/harness.py", "autodedup/evidence.py", "api/main.py",
                     "scraper/realtime_worker.py"))
    assert {"autodedup/incremental.py", "api/main.py", "scraper/realtime_worker.py"} <= lane
    assert not any(m.startswith(("autodedup/challenger/", "autodedup/lab/")) for m in lane)
    assert not any(m.startswith("autodedup/challenger/") for m in evidence.engine_modules())


def test_the_challenger_is_stdlib_and_engine_only() -> None:
    mine = _closure(("autodedup/challenger/facts.py", "autodedup/challenger/score.py",
                     "autodedup/challenger/union.py"))
    assert not {"third:numpy", "third:sklearn", "third:scipy"} & mine
    assert not any(m.startswith("autodedup/lab/") for m in mine)


if __name__ == "__main__":
    # Rewrites the committed fixture (needs the training extra): python3 -m tests.autodedup.test_challenger
    fitted_model, fixture_vectors, fixture_p = _fit_fixture()
    FIXTURE.write_text(json.dumps({"about": "tests/autodedup/test_challenger.py: a HistGradientBoosting"
                                            "Classifier + isotonic map exported by autodedup.lab.mf_fit, "
                                            "and scikit-learn's own p on 1,000 vectors",
                                   "model": fitted_model, "vectors": fixture_vectors,
                                   "sklearn_p": fixture_p}, separators=(",", ":")), encoding="utf-8")
