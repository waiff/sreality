"""W18 — a bound street decides the point, and an unbound one is not published.

The operator's ruling (2026-09-16, bazos ad 223293822, listing 18667956): the street an ad
names in its text must reach the store, and "if the text has a street that does not align
with the pin, the pin should be updated based on the street".

Two rules carry it, and both are stated once in code:

  1. `fill.fill` publishes a street ONLY when BIND matched one in the register. There is no
     preserve-if-null from the claim text any more — one rule for all nine portals.
  2. `bind.place` places a row on the street's own point (the centroid of its valid address
     points) whenever the portal pin cannot be trusted over it: no pin, a DECLARED blurred
     pin, or a pin farther away than the street is long.

The mirror's Jiráskova is shaped like the live one: centroid POINT(14.91365 50.42247) over
points spanning ~1.7 km, so its extent is ~872 m against the 863 m measured on the register.
That number is the whole reason the threshold is `max(REGISTRY_PIN_CONFLICT_M, extent)` — at
300 m flat, every second pin on a long street would read as a disagreement.
"""

from __future__ import annotations

from location_data.resolver import bind as step_bind
from location_data.resolver import core
from location_data.resolver import grade as step_grade
from location_data.resolver import normalize as step_normalize
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

# The live values this fixture is modelled on (`ruian_address_points`, read 2026-09-16).
STREET_LAT, STREET_LON = 50.42247, 14.91365
BLURRED_PIN = (50.416394, 14.916)     # the ad's own "Přibližná lokalita" marker
NEAR_PIN = (50.42000, 14.91400)       # 276 m from the centroid — on the street
FAR_PIN = (50.44000, 14.91365)        # 1,949 m — cannot be on it


def _extent() -> float:
    """Jiráskova's own extent, off the mirror rather than typed twice."""
    mirror = mm.default_mirror()
    street = next(s for s in mirror.streets if s.code == 105)
    return mirror.street_point(street).extent_m


def _resolve(claims, *, mirror=None):
    return core.resolve(
        claims, mm.context(mirror), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
    )


def _bazos(*extra, street: str | None = "ul. Jiráskova", pin=None, label=None):
    """The operator's listing as bazos@7 states it: town and PSČ off the href, the street
    out of the title, and the portal's own declaration that its pin is approximate."""
    claims = [
        mm.claim(1, "obec_name", source="bazos", value_text="Mladá Boleslav"),
        mm.claim(2, "psc", source="bazos", value_text="29301"),
    ]
    if street is not None:
        claims.append(mm.claim(3, "street_name", source="bazos", value_text=street))
    if pin is not None:
        claims.append(mm.claim(
            4, "coordinate", source="bazos", lat=pin[0], lon=pin[1],
            declared_precision_label=label))
    if label in step_bind.BLURRED_DECLARED_LABELS:
        # bazos states its blur in a SEPARATE claim (the maps anchor's title), which is what
        # `precision_cap.blurred_labels` is calibrated against. A precise label rides on the
        # coordinate itself and carries no blur evidence — passing `declared` beside a `gps`
        # label would blur the very pin the label certifies.
        claims.append(mm.claim(
            5, "precision_declaration", source="bazos", value_text=label,
            declared_precision_label=label, blur_evidence="declared"))
    return [*claims, *extra]


# ------------------------------------------------------------------ the street's own point

def test_a_street_the_register_holds_has_a_point_and_an_extent():
    """Derived, never stored: `ruian_streets` carries no geometry, so where a street IS is
    the centroid of its own address points and how far it reaches is half their bounding
    diagonal."""
    mirror = mm.default_mirror()
    street = next(s for s in mirror.streets if s.code == 105)
    point = mirror.street_point(street)
    assert (round(point.lat, 5), round(point.lon, 5)) == (STREET_LAT, STREET_LON)
    assert 800.0 < point.extent_m < 950.0
    assert point.point_count == 3


def test_a_register_street_with_no_address_points_has_no_point():
    """`Bernáčkova` is a real register row with nothing hung off it. A street without points
    keeps the pre-W18 answer — a name, and the town's position."""
    mirror = mm.statutory_city_mirror()
    street = next(s for s in mirror.streets if s.code == 200)
    assert mirror.street_point(street) is None


# --------------------------------------------------------------- the precedence, in order

def test_a_blurred_pin_loses_to_the_street_the_ad_names():
    """THE RULING. bazos stamps every pin "Přibližná lokalita", so a street the ad states
    outright is better evidence than a coordinate the portal itself calls approximate. The
    row moves 696 m, onto the street it names — and this is NOT a dispute: the portal and
    the register never contradicted each other, one is simply coarser."""
    resolution = _resolve(_bazos(pin=BLURRED_PIN, label="approximate_location"))
    assert (round(resolution.lat, 5), round(resolution.lon, 5)) == (STREET_LAT, STREET_LON)
    assert resolution.street_name == "Jiráskova"
    assert resolution.ulice_kod == 105
    assert resolution.obec_name == "Mladá Boleslav"
    assert resolution.disputed is None
    # THE GRAIN FOLLOWS THE POSITION. bazos declares its pin approximate and its contract caps
    # that pin at `obec` — but the pin is not where this row stands: the REGISTER is. So the
    # row grades at the bind's own level, carries the street's own radius, and the
    # declaration is left to do the only thing it still honestly can, which is cap the
    # confidence. Publishing `street_name` + `ulice_kod`, sitting on the street's centroid and
    # grading `obec` at 1 km was three fields of one row disagreeing with each other.
    assert resolution.granularity == "street"
    assert resolution.uncertainty_radius_m == max(step_grade.RADIUS_M["street"], _extent())
    assert resolution.match_confidence == "medium"

    # The same row through the OTHER surface — the capped headline rather than the parser's
    # own value — is the same answer: the line binder reaches the same register row.
    from_title = _resolve(_bazos(
        street="Prodej bytu 3+1 s lodžií, 86 m2, ul. Jiráskova, Mladá Bolesl",
        pin=BLURRED_PIN, label="approximate_location"))
    assert (from_title.ulice_kod, from_title.granularity) == (105, "street")
    assert (round(from_title.lat, 5), round(from_title.lon, 5)) == (STREET_LAT, STREET_LON)


def test_the_same_row_without_a_street_is_unchanged_by_w18():
    """The control, and the other half of the rule: with nothing bound below the town the
    pin IS the position, so bazos' declared cap applies exactly as it always has — obec, one
    kilometre, the ad's own approximate pin."""
    resolution = _resolve(_bazos(street=None, pin=BLURRED_PIN, label="approximate_location"))
    assert resolution.granularity == "obec"
    assert resolution.uncertainty_radius_m == step_grade.RADIUS_M["obec"]
    assert (resolution.lat, resolution.lon) == BLURRED_PIN
    assert resolution.street_name is None and resolution.ulice_kod is None


def test_a_street_with_no_pin_at_all_is_placed_on_the_street():
    resolution = _resolve(_bazos())
    assert (round(resolution.lat, 5), round(resolution.lon, 5)) == (STREET_LAT, STREET_LON)
    assert resolution.granularity == "street"
    # The radius reaches the far end of the street, not the level's 300 m constant.
    assert resolution.uncertainty_radius_m > step_grade.RADIUS_M["street"]
    assert 800.0 < resolution.uncertainty_radius_m < 950.0


def test_an_exact_pin_that_agrees_with_the_street_keeps_the_position():
    """276 m along a street that runs 1.7 km is ON it. The pin is the finer of two true
    answers, so it stays — and nothing is disputed."""
    resolution = _resolve(_bazos(pin=NEAR_PIN, label="gps"))
    assert (resolution.lat, resolution.lon) == NEAR_PIN
    assert resolution.ulice_kod == 105
    assert resolution.disputed is None
    assert resolution.granularity == "street"
    assert resolution.uncertainty_radius_m == step_grade.RADIUS_M["street"]


def test_an_exact_pin_farther_than_the_street_is_long_loses_and_is_called_out():
    """1,949 m from the centroid of an 872 m street: the ad's two statements about where it
    is do not fit. The street wins because it BOUND to the register, and the row says which
    half lost."""
    resolution = _resolve(_bazos(pin=FAR_PIN, label="gps"))
    assert (round(resolution.lat, 5), round(resolution.lon, 5)) == (STREET_LAT, STREET_LON)
    assert resolution.disputed == "pin_off_street"
    assert resolution.match_confidence == "medium"
    assert resolution.granularity == "street"


def test_the_threshold_is_the_streets_extent_not_a_flat_radius():
    """The same 500 m pin is ON a 1.7 km street and OFF a two-point one. A flat
    `REGISTRY_PIN_CONFLICT_M` would dispute the first, which is the false positive the
    extent exists to prevent."""
    kept = _resolve(_bazos(pin=(50.42690, 14.91365), label="gps"))
    assert kept.disputed is None and (kept.lat, kept.lon) == (50.42690, 14.91365)

    # `Nad Bořislavkou` is two adjacent points — an extent of metres, so the threshold
    # falls back to the 300 m floor and the same displacement is a disagreement.
    tight = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou"),
        mm.claim(3, "coordinate", lat=50.10600, lon=14.34800, declared_precision_label="gps"),
    ])
    assert tight.disputed == "pin_off_street"


def test_an_address_point_still_outranks_everything():
    """The precedence is a ladder, and W18 inserts a rung UNDER the top one. A čp that joins
    to an address point places the row on the building, not on the street's centroid."""
    resolution = _resolve(_bazos(street="ul. Jiráskova 40", pin=BLURRED_PIN,
                                 label="approximate_location"))
    assert resolution.ruian_adm_kod == 55000002
    assert (resolution.lat, resolution.lon) == (50.42247, 14.91365)
    assert resolution.house_number_cp == "40"


# ------------------------------------------------------- what the register refuses to bind

def test_a_numeric_leading_street_binds():
    """220059906's regression, end to end: `28. října` is a real street and the resolver
    carries it whole — the leading ordinal is part of the name, never a house number."""
    resolution = _resolve([
        mm.claim(1, "obec_name", source="bazos", value_text="Praha"),
        mm.claim(2, "street_name", source="bazos", value_text="ul. 28. října"),
    ])
    assert resolution.street_name == "28. října"
    assert resolution.ulice_kod == 102


def test_a_hallucinated_street_binds_to_nothing_and_is_not_published():
    """220870847's regression, and the reason no morphology gate is needed: there is no
    street called `Nový` in Praha, so the register refuses it. The row keeps its town."""
    resolution = _resolve([
        mm.claim(1, "obec_name", source="bazos", value_text="Praha"),
        mm.claim(2, "street_name", source="bazos", value_text="Nový"),
    ])
    assert resolution.street_name is None
    assert resolution.ulice_kod is None
    assert resolution.obec_name == "Praha"


def test_an_unbound_street_never_moves_the_pin_either():
    """The two rules are one rule: a street that is not published cannot place a row. The
    blurred pin stays the position — there is nothing better to put there."""
    resolution = _resolve(_bazos(street="ul. Neexistující", pin=BLURRED_PIN,
                                 label="approximate_location"))
    assert resolution.street_name is None
    assert (resolution.lat, resolution.lon) == BLURRED_PIN


# ------------------------------------------------------------------ the cost of the rung

def test_the_street_point_is_asked_for_the_winner_and_for_nothing_else():
    """One round trip per listing that BINDS a street, and none at all for a listing that
    does not — the question is asked after the ranking, not per candidate. Praha alone holds
    thousands of streets, and asking each one where it is would be the whole budget."""
    mirror = mm.default_mirror()
    asked: list[int] = []
    inner = mirror.street_point

    def counting(street):
        asked.append(street.code)
        return inner(street)

    mirror.street_point = counting  # type: ignore[method-assign]

    _resolve(_bazos(), mirror=mirror)
    assert asked == [105]

    asked.clear()
    _resolve(_bazos(street=None, pin=BLURRED_PIN, label="approximate_location"),
             mirror=mirror)
    assert asked == []


def test_place_reads_the_binding_and_never_the_registry():
    """The purity rail, stated where it is easiest to break: `place()` compares a pin with a
    point BIND already carries. A registry read here would put a round trip inside the pure
    core, which `test_resolver_purity` forbids by AST scan."""
    claims = [mm.claim(1, "obec_name", value_text="Mladá Boleslav"),
              mm.claim(2, "street_name", value_text="Jiráskova")]
    binding, _ = step_bind.bind(
        claims, step_normalize.normalize_all(claims), mm.context(),
    )
    assert binding.target_kind == "street"
    assert (round(binding.lat, 5), round(binding.lon, 5)) == (STREET_LAT, STREET_LON)
    assert binding.street_extent_m is not None
