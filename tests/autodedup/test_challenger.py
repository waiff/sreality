"""The model-first challenger (`autodedup/challenger/`, B-b): its three modules, its lab rungs, and
the proof that the live lane cannot import it while it is lab-only."""

from __future__ import annotations

import ast
import json
import math
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autodedup import evidence
from autodedup.challenger import facts as mf_facts
from autodedup.challenger.facts import READERS, Dials, stated_difference
from autodedup.challenger.score import FORMAT, scorer
from autodedup.challenger.union import constrained_union
from autodedup.dataset import Listing, Location
from autodedup.features import TAG_FEATURE_NAMES
from autodedup.indistinguishable import Fact
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


def test_the_readers_are_the_ladders_own_and_tags_are_never_facts(monkeypatch: Any) -> None:
    seen: list[Any] = []

    def fake(a: Listing, b: Listing, feats: Any, settings: Settings, mode: str) -> list[Fact]:
        seen.append((feats, settings.d43_body_align, mode))
        return [Fact("interior", "x", "y"), Fact("floorplan", "x", "y"), Fact("street", "x", "y"),
                Fact(names.pop(), "x", "y")]

    monkeypatch.setattr(mf_facts, "distinguishing_facts", fake)
    feats = {name: (0.0, True) for name in TAG_FEATURE_NAMES} | {"phash_tight_matches": (5.0, True)}
    fact = _fact(_listing(1), _listing(2))
    names = ["cellar_area"]
    assert fact(1, 2, feats) == "cellar_area"
    assert seen[-1][0] == {"phash_tight_matches": (5.0, True)}
    assert seen[-1][1:] == (False, "cluster")
    names = ["agency_code"]
    assert fact(1, 2, feats) is None
    assert len(READERS) == 14 and not set(READERS) & {"interior", "floorplan", "street"}


def test_a_real_reader_fires_through_distinguishing_facts() -> None:
    a = _listing(1, description="Byt 2+kk o vymere 54 m2 se nachazi v prizemi cihloveho domu.")
    b = _listing(2, description="Byt 2+kk o vymere 45 m2 se nachazi v prizemi cihloveho domu.")
    assert _fact(a, b)(1, 2) in {"printed_area", "body_align"}
    assert _fact(a, b, dials=Dials(body_align=1.0))(1, 2) == "printed_area"


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
    assert p([1.0, 1.0]) == round(expit(0.5 - 1.0 + 0.25), 12)
    assert p([2.0, None]) == round(expit(0.5 + 2.0 - 0.25), 12)
    assert p([None, float("nan")]) == round(expit(0.5 - 1.0 - 0.25), 12)
    calibrated = _stump() | {"calibration": {"x": [0.1, 0.9], "y": [0.0, 1.0], "lo": 0.1, "hi": 0.9}}
    q = scorer(calibrated)
    low = expit(0.5 - 1.0 - 0.25)
    assert q([2.0, None]) == 1.0
    assert q([None, None]) == round((low - 0.1) / 0.8 * 1.0 + (0.9 - low) / 0.8 * 0.0, 12)
    with pytest.raises(ValueError):
        scorer({"format": "other"})


def test_the_last_bits_of_exp_never_reach_p() -> None:
    """Two raw sums one ulp apart on an isotonic plateau read one p, so no tie is reordered."""
    plateau = _stump() | {"trees": [], "calibration": {"x": [0.0, 0.5, 1.0], "y": [0.3, 0.3, 0.9],
                                                       "lo": 0.0, "hi": 1.0}}
    p = scorer(plateau | {"baseline": -0.3})
    q = scorer(plateau | {"baseline": math.nextafter(-0.3, 0.0)})
    assert p([None, None]) == q([None, None]) == 0.3


def _vectors(n: int, width: int, seed: int) -> list[list[float | None]]:
    rng = random.Random(seed)
    return [[None if rng.random() < 0.2 else round(rng.gauss(0.0, 1.0), 6) for _ in range(width)]
            for _ in range(n)]


def test_the_committed_model_file_equals_scikit_learn_within_1e_9() -> None:
    """CI (no scikit-learn): a model exported from a fitted HistGradientBoostingClassifier and
    isotonic map, and scikit-learn's own predictions on 1,000 vectors, both committed."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    p = scorer(fixture["model"])
    gaps = [abs(p(x) - want) for x, want in zip(fixture["vectors"], fixture["sklearn_p"])]
    assert len(gaps) == 1000 and max(gaps) <= 1e-9
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
    """Local (training extra): fit, export, evaluate; calibration and a presence-only column in."""
    model, vectors, want = _fit_fixture()
    p = scorer(model)
    gaps = [abs(p(x) - w) for x, w in zip(vectors, want)]
    assert len(gaps) == 1000 and max(gaps) <= 1e-9
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
                     "autodedup/harness.py", "autodedup/evidence.py"))
    assert "autodedup/incremental.py" in lane
    assert not any(m.startswith("autodedup/challenger/") for m in lane)
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
