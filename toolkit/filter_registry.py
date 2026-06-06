"""Canonical registry of every listing filter the app exposes.

One entry per filter. Each entry carries the metadata that every
consumer needs:

- the SQL column it maps to (or `None` for synthetic filters),
- the Python type and default,
- a single-paragraph description (agents read this verbatim through the
  tool JSON schemas),
- the agendas where it applies (Browse, Watchdog, Comparables, …),
- a UI control hint (so the React `<FilterForm>` can render the right
  widget without per-filter case statements),
- optional constraints (min / max / step / enum / list length),
- optional unit (`m`, `%`, `days`, `m²`, `CZK`),
- optional enum value list with Czech + English labels,
- legacy aliases so older field names stay readable.

Adding a new filter is a single PR that touches this file and (if it
needs a DB column) a migration. Every downstream surface — Pydantic
schemas, agent tool JSON, Watchdog matcher, React FilterForm, browse
URL serialiser — is either generated from the registry or asserted
against it in tests, so the registry is genuinely the source of truth.

The `filter_visibility` table (migration 059) lets the operator turn
individual (agenda, filter) pairs off from Settings. Use
`effective_for(agenda, conn)` to get the visible subset for a given
surface; `effective_for(agenda)` without a connection returns every
filter declared for that agenda (the default-on superset).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg


# --- enums ----------------------------------------------------------------


class Agenda(StrEnum):
    """Where a filter can apply.

    Multiple consumers per surface is fine; the registry is the union.
    """
    BROWSE = "browse"             # frontend Browse sidebar
    WATCHDOG = "watchdog"         # WatchdogFilterSpec + matcher
    COMPARABLES = "comparables"   # toolkit.find_comparables_* + agent tools
    ESTIMATION = "estimation"     # CreateEstimationIn / EstimateYieldIn
    VELOCITY = "velocity"         # compute_market_velocity
    NEIGHBORHOOD = "neighborhood" # describe_neighborhood
    DEFAULTS = "defaults"         # Settings → app_settings tunables


class UiControl(StrEnum):
    """The widget shape the registry-driven React FilterForm renders."""
    RANGE_SLIDER = "range_slider"      # dual-thumb slider over a bounded axis
    RANGE_INPUTS = "range_inputs"      # paired number inputs (open ends)
    PILL_GROUP = "pill_group"          # exclusive select rendered as buttons
    MULTISELECT = "multiselect"        # multi-value chip selector
    TRISTATE = "tristate"              # any / yes / no
    SINGLE_SELECT = "single_select"    # dropdown
    NUMBER_INPUT = "number_input"      # single number
    CSV_INPUT = "csv_input"            # comma-separated string list
    BOOLEAN = "boolean"                # plain checkbox
    LOCATION = "location"              # composite: districts + map + dot/radius
    CITY_INDEX_RULES = "city_index_rules"  # repeatable {index, op, threshold} rows
    NEAR_CITY_RULE = "near_city_rule"  # city_index_rules + radius_km + population


class FilterType(StrEnum):
    """Python type token. Used for codegen + JSON Schema rendering."""
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "string"
    STRING_LIST = "string_list"
    INT_LIST = "int_list"
    LOCATION = "location"            # composite — only used with UiControl.LOCATION
    DISTRICT_CHIP_LIST = "district_chip_list"  # list of {name, context|null} — Browse/Watchdog districts
    CITY_INDEX_RULE_LIST = "city_index_rule_list"  # list of {index_name, op, value} — Browse/Watchdog city quality
    NEAR_CITY_PROXIMITY = "near_city_proximity"   # composite {index_rules, population_min, radius_km}


# --- value containers -----------------------------------------------------


@dataclass(frozen=True)
class EnumOption:
    """One choice in an enum-valued filter.

    `value` is the wire value (what gets sent to the SQL clause /
    Pydantic schema). `label_cs` is what the operator sees in the UI;
    `label_en` is for tooling that needs English (agent prompts can
    pick either, but the SQL data is Czech without diacritics).
    """
    value: str | int
    label_cs: str
    label_en: str


@dataclass(frozen=True)
class FilterDef:
    """One filter, fully described.

    `id` is the canonical snake_case key. Every other code surface
    references the filter by this id. Aliases let legacy names
    (e.g. `min_price_czk` vs `price_min`) keep working at the
    serialiser boundary without polluting the canonical model.

    `pg_column` is the underlying listings column for column-backed
    filters; `None` for synthetic ones (population, status, max_age_days
    which is a derived predicate).
    """
    id: str
    type: FilterType
    pg_column: str | None
    default: Any
    description: str
    category: str
    ui_control: UiControl
    agendas: frozenset[Agenda]
    constraints: dict[str, Any] = field(default_factory=dict)
    unit: str | None = None
    enum_values: tuple[EnumOption, ...] | None = None
    aliases: tuple[str, ...] = ()


# --- enum value tables ----------------------------------------------------
# Single source for label_cs / label_en pairs across the registry.
# The actual sreality enum codes mirror scraper/parser.py.


CATEGORY_MAIN_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("byt", "Byty", "Apartments"),
    EnumOption("dum", "Domy", "Houses"),
    EnumOption("komercni", "Komerční", "Commercial"),
    EnumOption("pozemek", "Pozemky", "Land"),
    EnumOption("ostatni", "Ostatní", "Other"),
)

CATEGORY_TYPE_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("pronajem", "Pronájem", "For rent"),
    EnumOption("prodej", "Prodej", "For sale"),
    EnumOption("drazba", "Dražba", "Auction"),
    EnumOption("podil", "Podíl", "Fractional"),
)

# Sentinel multiselect value meaning "no value, or a value outside the
# canonical option set" (e.g. a NULL furnished, or a rogue portal label the
# parser didn't normalise). The WHERE builders translate it to
# `col IS NULL OR col <> ALL(canonical)`. The canonical tuples below are the
# single source of truth for "what counts as a known value".
UNKNOWN_FILTER_VALUE = "__unknown__"
_UNKNOWN_OPTION = EnumOption(UNKNOWN_FILTER_VALUE, "Neuvedeno", "Unknown / not specified")

FURNISHED_CANONICAL: tuple[str, ...] = ("ano", "ne", "castecne")
OWNERSHIP_CANONICAL: tuple[str, ...] = ("osobni", "druzstevni", "statni")

FURNISHED_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("ano", "Vybaveno", "Furnished"),
    EnumOption("ne", "Nevybaveno", "Unfurnished"),
    EnumOption("castecne", "Částečně", "Partially furnished"),
    _UNKNOWN_OPTION,
)

OWNERSHIP_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("osobni", "Osobní", "Personal"),
    EnumOption("druzstevni", "Družstevní", "Cooperative"),
    EnumOption("statni", "Státní/obecní", "State/Municipal"),
    _UNKNOWN_OPTION,
)

BUILDING_MATERIAL_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("cihla", "Cihla", "Brick"),
    EnumOption("panel", "Panel", "Panel"),
    EnumOption("smisena", "Smíšená", "Mixed"),
    EnumOption("ostatni", "Ostatní", "Other"),
)

BUILDING_TYPE_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("cihla", "Cihla", "Brick"),
    EnumOption("panel", "Panel", "Panel"),
    EnumOption("smisena", "Smíšená", "Mixed"),
    EnumOption("skelet", "Skelet", "Skeleton"),
    EnumOption("drevo", "Dřevěná", "Wood"),
    EnumOption("kamen", "Kamenná", "Stone"),
    EnumOption("montovana", "Montovaná", "Prefab"),
    EnumOption("nizkoenergeticka", "Nízkoenergetická", "Low-energy"),
)

CONDITION_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("novostavba", "Novostavba", "New build"),
    EnumOption("po_rekonstrukci", "Po rekonstrukci", "Recently renovated"),
    EnumOption("velmi_dobry", "Velmi dobrý", "Very good"),
    EnumOption("dobry", "Dobrý", "Good"),
    EnumOption("pred_rekonstrukci", "Před rekonstrukcí", "Needs renovation"),
    EnumOption("k_demolici", "K demolici", "For demolition"),
)

ENERGY_RATING_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("A", "A", "A"),
    EnumOption("B", "B", "B"),
    EnumOption("C", "C", "C"),
    EnumOption("D", "D", "D"),
    EnumOption("E", "E", "E"),
    EnumOption("F", "F", "F"),
    EnumOption("G", "G", "G"),
)

DISPOSITION_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("1+kk", "1+kk", "1+kk"),
    EnumOption("1+1", "1+1", "1+1"),
    EnumOption("2+kk", "2+kk", "2+kk"),
    EnumOption("2+1", "2+1", "2+1"),
    EnumOption("3+kk", "3+kk", "3+kk"),
    EnumOption("3+1", "3+1", "3+1"),
    EnumOption("4+kk", "4+kk", "4+kk"),
    EnumOption("4+1", "4+1", "4+1"),
    EnumOption("5+kk", "5+kk", "5+kk"),
    EnumOption("5+1", "5+1", "5+1"),
)

# Portal-agnostic property sub-type, grouped by category_main. House and
# commercial slugs are disjoint; the frontend renders the group matching the
# selected category_main. Mirrors scraper.parser.SUBTYPE / data backfill.
SUBTYPE_OPTIONS: tuple[EnumOption, ...] = (
    # dum (houses)
    EnumOption("rodinny_dum", "Rodinný dům", "Detached house"),
    EnumOption("vila", "Vila", "Villa"),
    EnumOption("chata", "Chata", "Cabin"),
    EnumOption("chalupa", "Chalupa", "Cottage"),
    EnumOption("vicegeneracni_dum", "Vícegenerační dům", "Multi-generational house"),
    EnumOption("zemedelska_usedlost", "Zemědělská usedlost", "Farmstead"),
    EnumOption("na_klic", "Na klíč", "Turnkey"),
    EnumOption("pamatka_jine", "Památka/jiné", "Heritage/other"),
    # komercni (commercial)
    EnumOption("kancelar", "Kancelář", "Office"),
    EnumOption("sklad", "Sklad", "Warehouse"),
    EnumOption("obchodni_prostor", "Obchodní prostor", "Retail space"),
    EnumOption("vyroba", "Výroba", "Manufacturing"),
    EnumOption("ubytovani", "Ubytování", "Accommodation"),
    EnumOption("restaurace", "Restaurace", "Restaurant"),
    EnumOption("cinzovni_dum", "Činžovní dům", "Tenement house"),
    EnumOption("apartmany", "Apartmány", "Apartments"),
    EnumOption("ordinace", "Ordinace", "Medical office"),
    EnumOption("zemedelsky", "Zemědělský objekt", "Agricultural"),
    EnumOption("virtualni_kancelar", "Virtuální kancelář", "Virtual office"),
    EnumOption("ostatni", "Ostatní", "Other"),
)

POPULATION_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("active", "Aktivní", "Active"),
    EnumOption("delisted", "Stažené", "Delisted"),
    EnumOption("all", "Vše", "All"),
)

DISPOSITION_MATCH_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("exact", "Přesně", "Exact"),
    EnumOption("loose", "Volně", "Loose (kk ↔ 1)"),
    EnumOption("any", "Libovolně", "Any"),
)

STATUS_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("any", "Vše", "Any"),
    EnumOption("active", "Aktivní", "Active"),
    EnumOption("inactive", "Neaktivní", "Inactive"),
)

# Preset "last N days" buckets shared by the `recently_added_days` /
# `recently_changed_days` Browse filters. Values are day counts; the empty
# (unset) selection means "any time". Kept as a shared table so the two
# filters can never drift in their option list.
RECENCY_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption(1, "Dnes", "Today"),
    EnumOption(3, "Poslední 3 dny", "Last 3 days"),
    EnumOption(7, "Poslední týden", "Last 7 days"),
    EnumOption(14, "Posledních 14 dní", "Last 14 days"),
    EnumOption(30, "Poslední měsíc", "Last month"),
)

# Source portals — mirrors the scraper-kind rows of the `portals` registry
# table (migration 100 onward). Values match `listings.source`; labels match
# the portal display labels. Extend this when a new ingesting portal lands
# (and regenerate the frontend registry). The on-demand URL-parser source
# `idnes_reality` never produces `listings` rows, so it is not offered as a
# filter option. (`remax` IS offered: as of migration 135 it is a scraper that
# ingests `listings` rows, distinct from its same-named on-demand URL parser.)
PORTAL_OPTIONS: tuple[EnumOption, ...] = (
    EnumOption("sreality", "Sreality", "Sreality"),
    EnumOption("bazos", "Bazoš", "Bazoš"),
    EnumOption("idnes", "iDNES Reality", "iDNES Reality"),
    EnumOption("maxima", "Maxima Reality", "Maxima Reality"),
    EnumOption("ceskereality", "Českéreality", "Českéreality"),
    EnumOption("bezrealitky", "Bezrealitky", "Bezrealitky"),
    EnumOption("mmreality", "M&M Reality", "M&M Reality"),
    EnumOption("remax", "RE/MAX", "RE/MAX"),
)


# --- category constants ---------------------------------------------------
# Stable category names used in the registry. Settings UI groups
# filters by these.


CATEGORY_SPATIAL = "Spatial"
CATEGORY_PROPERTY = "Property"
CATEGORY_AMENITY = "Amenity"
CATEGORY_VELOCITY = "Velocity"
CATEGORY_STATUS = "Status"
CATEGORY_CURATION = "Curation"
CATEGORY_COHORT = "Cohort tuning"
CATEGORY_CITY_QUALITY = "City quality"


_ALL_AGENDAS = frozenset(Agenda)
_BACKEND_AGENDAS = frozenset({
    Agenda.COMPARABLES, Agenda.ESTIMATION,
    Agenda.VELOCITY, Agenda.NEIGHBORHOOD,
})
_UI_AGENDAS = frozenset({Agenda.BROWSE, Agenda.WATCHDOG})


# --- the registry ---------------------------------------------------------
# Order matters only for human readability; consumers iterate by id.


def _build_registry() -> dict[str, FilterDef]:
    """Construct the registry. Lifted into a function so tests can call
    it for parity checks without import-time side effects."""

    entries: list[FilterDef] = [
        # --- location (composite, BROWSE / WATCHDOG) ---------------------
        FilterDef(
            id="location",
            type=FilterType.LOCATION,
            pg_column=None,
            default=None,
            description=(
                "Where the listing must be. Composite filter with three "
                "complementary sub-fields: a district name list "
                "(matched against l.district), a map bounding box "
                "(west/south/east/north on l.geom), and a center+radius "
                "pair (ST_DWithin around (lat,lng) within radius_m). "
                "Districts is an independent AND-clause; the map vs "
                "center+radius pair are mutually exclusive — when both "
                "are set, center+radius wins. Leave everything null for "
                "no spatial restriction."
            ),
            category=CATEGORY_SPATIAL,
            ui_control=UiControl.LOCATION,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
        ),

        FilterDef(
            id="districts",
            type=FilterType.DISTRICT_CHIP_LIST,
            pg_column="district",
            default=None,
            description=(
                "Match listings whose `district` (human-readable text) "
                "is in the list. Each chip is an object "
                "`{name: str, context: str | null}`: `name` is the "
                "primary phrase to match (ILIKE substring across "
                "`district` and `locality`), `context` is the parent "
                "municipality from Mapy.cz's `regionalStructure` that "
                "narrows the match when set (so picking the Plzeň "
                "entry for 'Edvarda Beneše' doesn't drag in the "
                "Olomouc + Hradec Králové streets of the same name). "
                "Multi-value AND-of-OR: a listing matches if ANY chip "
                "matches, and districts is AND'd with the other "
                "filters. Use `locality_district_id` for "
                "renames-stable matching."
            ),
            category=CATEGORY_SPATIAL,
            ui_control=UiControl.MULTISELECT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
        ),

        # --- cohort tuning (COMPARABLES / agent-only knobs) --------------
        FilterDef(
            id="radius_m",
            type=FilterType.INT,
            pg_column=None,  # target-relative; applied via ST_DWithin around target
            default=1000,
            description=(
                "Spatial radius around the target listing, in metres. "
                "Applied as ST_DWithin(l.geom, target_point, radius_m). "
                "Defaults to 1000 m — a tight 10-minute walk. Widen "
                "for sparse rural cohorts; tighten for dense urban "
                "blocks where a few hundred metres changes the price "
                "level materially."
            ),
            category=CATEGORY_COHORT,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({
                Agenda.COMPARABLES, Agenda.ESTIMATION,
                Agenda.VELOCITY, Agenda.NEIGHBORHOOD,
                Agenda.DEFAULTS,
            }),
            constraints={"min": 100, "max": 10000},
            unit="m",
        ),
        FilterDef(
            id="area_band_pct",
            type=FilterType.FLOAT,
            pg_column=None,
            default=0.20,
            description=(
                "Half-width of the area band around the target's "
                "area_m2, expressed as a fraction. 0.20 means "
                "±20% — a 60 m² target accepts comparables in "
                "48 m² – 72 m². Widen on small cohorts; tighten "
                "when the cohort is large enough to demand a closer "
                "match."
            ),
            category=CATEGORY_COHORT,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({
                Agenda.COMPARABLES, Agenda.ESTIMATION,
                Agenda.VELOCITY, Agenda.DEFAULTS,
            }),
            constraints={"min": 0.05, "max": 0.6, "step": 0.05},
            unit="%",
        ),
        FilterDef(
            id="disposition_match",
            type=FilterType.STRING,
            pg_column=None,
            default="exact",
            description=(
                "How strictly the cohort's disposition must match the "
                "target's. `exact` requires the same string (e.g. "
                "3+kk = 3+kk). `loose` collapses kk-vs-1 pairs "
                "(3+kk ↔ 3+1). `any` drops the constraint."
            ),
            category=CATEGORY_COHORT,
            ui_control=UiControl.SINGLE_SELECT,
            agendas=frozenset({
                Agenda.COMPARABLES, Agenda.ESTIMATION,
                Agenda.VELOCITY, Agenda.DEFAULTS,
            }),
            constraints={"enum": ["exact", "loose", "any"]},
            enum_values=DISPOSITION_MATCH_OPTIONS,
        ),
        FilterDef(
            id="floor_band",
            type=FilterType.INT,
            pg_column=None,
            default=None,
            description=(
                "If set, restrict the cohort to listings whose `floor` "
                "is within ±N of the target's floor. Omit (the default) "
                "to ignore floor entirely. Useful in apartment buildings "
                "where ground-floor and top-floor prices diverge from "
                "the middle floors."
            ),
            category=CATEGORY_COHORT,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.COMPARABLES, Agenda.ESTIMATION, Agenda.VELOCITY}),
            constraints={"min": 0, "max": 20},
        ),
        FilterDef(
            id="max_age_days",
            type=FilterType.INT,
            pg_column=None,
            default=None,
            description=(
                "Drop listings whose `last_seen_at` is older than N "
                "days. Applied only when `population` resolves to "
                "`active` (or `active_only=true`). Set explicitly when "
                "you want a freshness gate — there's no implicit "
                "default."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({
                Agenda.COMPARABLES, Agenda.ESTIMATION,
                Agenda.VELOCITY, Agenda.NEIGHBORHOOD,
                Agenda.DEFAULTS,
            }),
            constraints={"min": 1, "max": 365},
            unit="days",
        ),
        FilterDef(
            id="active_only",
            type=FilterType.BOOL,
            pg_column=None,
            default=False,
            description=(
                "When true, restricts the cohort to `l.is_active = true`. "
                "Legacy boolean retained for backwards compatibility; "
                "`population='active'` is the modern equivalent and "
                "carries the same effect plus an optional freshness "
                "gate via `max_age_days`."
            ),
            category=CATEGORY_STATUS,
            ui_control=UiControl.BOOLEAN,
            agendas=frozenset({Agenda.COMPARABLES, Agenda.ESTIMATION, Agenda.DEFAULTS}),
            aliases=("activeOnly",),
        ),
        FilterDef(
            id="population",
            type=FilterType.STRING,
            pg_column=None,
            default=None,
            description=(
                "Coarse cohort population selector. `active` = "
                "is_active=true (plus max_age_days if set); "
                "`delisted` = is_active=false (closed deals only — "
                "rough proxy for transacted listings); `all` = both. "
                "Mutually exclusive with `active_only`; if both are set, "
                "population wins."
            ),
            category=CATEGORY_STATUS,
            ui_control=UiControl.SINGLE_SELECT,
            agendas=frozenset({
                Agenda.COMPARABLES, Agenda.ESTIMATION, Agenda.VELOCITY,
            }),
            constraints={"enum": ["active", "delisted", "all"]},
            enum_values=POPULATION_OPTIONS,
        ),

        # --- listing status (BROWSE friendlier alt) ----------------------
        FilterDef(
            id="status",
            type=FilterType.STRING,
            pg_column=None,  # synthetic: drives is_active filter at the UI layer
            default="any",
            description=(
                "Listing status filter for Browse. `any` shows both "
                "live and delisted; `active` only is_active=true; "
                "`inactive` only is_active=false. The Watchdog matcher "
                "ignores this (it fires on new listings only) — use "
                "`population` for the analytical surfaces."
            ),
            category=CATEGORY_STATUS,
            ui_control=UiControl.PILL_GROUP,
            agendas=frozenset({Agenda.BROWSE}),
            constraints={"enum": ["any", "active", "inactive"]},
            enum_values=STATUS_OPTIONS,
        ),

        # --- recency presets (BROWSE Status section) ---------------------
        # Friendly "last N days" pickers. `recently_added_days` is the
        # preset twin of `first_seen_max_days` (first_seen_at >= now() - N);
        # `recently_changed_days` filters on properties.last_change_at
        # (migration 158 — newest content snapshot per property). BROWSE-only,
        # matching the rest of the first/last-seen date filters: the watchdog
        # matcher fires on new/changed listings already, and the estimation
        # agent keeps its own deterministic freshness knobs (first_seen_max_days
        # / max_age_days), so these never reach those agendas. Synthetic
        # (pg_column=None): hand-coded to a days-ago timestamp predicate in
        # the frontend + browse_stats RPC.
        FilterDef(
            id="recently_added_days",
            type=FilterType.INT,
            pg_column=None,
            default=None,
            description=(
                "Restrict to properties first seen in the last N days "
                "(`first_seen_at >= now() - N days`). Preset buckets: "
                "1 (today), 3, 7, 14, 30. The friendly 'what's new' filter."
            ),
            category=CATEGORY_STATUS,
            ui_control=UiControl.SINGLE_SELECT,
            agendas=frozenset({Agenda.BROWSE}),
            constraints={"enum": [1, 3, 7, 14, 30]},
            unit="days",
            enum_values=RECENCY_OPTIONS,
        ),
        FilterDef(
            id="recently_changed_days",
            type=FilterType.INT,
            pg_column=None,
            default=None,
            description=(
                "Restrict to properties whose content changed in the last N "
                "days (`last_change_at >= now() - N days`, where last_change_at "
                "is the newest content snapshot across the property's children "
                "— a price / area / description / attribute change, not a mere "
                "re-sighting). Preset buckets: 1 (today), 3, 7, 14, 30."
            ),
            category=CATEGORY_STATUS,
            ui_control=UiControl.SINGLE_SELECT,
            agendas=frozenset({Agenda.BROWSE}),
            constraints={"enum": [1, 3, 7, 14, 30]},
            unit="days",
            enum_values=RECENCY_OPTIONS,
        ),

        # --- velocity bands ----------------------------------------------
        FilterDef(
            id="tom_days_min",
            type=FilterType.INT,
            pg_column=None,  # computed: see listings_public.tom_days (migration 054)
            default=None,
            description=(
                "Lower bound on time-on-market in days. TOM = "
                "(now - first_seen_at) for active rows, "
                "(last_seen_at - first_seen_at) for delisted rows. "
                "Inclusive."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.COMPARABLES, Agenda.ESTIMATION}),
            constraints={"min": 0},
            unit="days",
        ),
        FilterDef(
            id="tom_days_max",
            type=FilterType.INT,
            pg_column=None,
            default=None,
            description=(
                "Upper bound on time-on-market in days. See "
                "`tom_days_min` for the TOM definition. Inclusive."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.COMPARABLES, Agenda.ESTIMATION}),
            constraints={"min": 0},
            unit="days",
        ),
        FilterDef(
            id="last_seen_min_days",
            type=FilterType.INT,
            pg_column=None,  # predicate on last_seen_at
            default=None,
            description=(
                "`last_seen_at <= now() - N days`. Days-ago floor — "
                "set N=3 to exclude listings seen in the last 2 days. "
                "Pair with `last_seen_max_days` for a window."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.COMPARABLES, Agenda.ESTIMATION}),
            constraints={"min": 0},
            unit="days",
        ),
        FilterDef(
            id="last_seen_max_days",
            type=FilterType.INT,
            pg_column=None,
            default=None,
            description=(
                "`last_seen_at >= now() - N days`. Days-ago ceiling — "
                "set N=30 to exclude listings not seen in the last 30 "
                "days. Pair with `last_seen_min_days` for a window."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.COMPARABLES, Agenda.ESTIMATION}),
            constraints={"min": 0},
            unit="days",
        ),
        FilterDef(
            id="first_seen_min_days",
            type=FilterType.INT,
            pg_column=None,
            default=None,
            description=(
                "`first_seen_at <= now() - N days`. Excludes listings "
                "that first appeared in the last N days. Useful for "
                "filtering out brand-new postings when looking at "
                "established cohorts."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.COMPARABLES, Agenda.ESTIMATION}),
            constraints={"min": 0},
            unit="days",
        ),
        FilterDef(
            id="first_seen_max_days",
            type=FilterType.INT,
            pg_column=None,
            default=None,
            description=(
                "`first_seen_at >= now() - N days`. Restricts to listings "
                "first seen in the last N days. The classic 'show me "
                "new listings' filter."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.COMPARABLES, Agenda.ESTIMATION}),
            constraints={"min": 0},
            unit="days",
        ),

        # --- category + disposition --------------------------------------
        FilterDef(
            id="category_main",
            type=FilterType.STRING,
            pg_column="category_main",
            default="byt",
            description=(
                "Top-level sreality category. `byt` = apartments, "
                "`dum` = houses, `komercni` = commercial. "
                "`pozemek` (land) and `ostatni` (other) exist in the "
                "data but the app's downstream surfaces target the "
                "first three."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.PILL_GROUP,
            agendas=_ALL_AGENDAS,
            constraints={"enum": [o.value for o in CATEGORY_MAIN_OPTIONS]},
            enum_values=CATEGORY_MAIN_OPTIONS,
        ),
        FilterDef(
            id="category_type",
            type=FilterType.STRING,
            pg_column="category_type",
            default="pronajem",
            description=(
                "Deal type. `pronajem` = for rent, `prodej` = for sale, "
                "`drazba` = auction, `podil` = fractional ownership. "
                "Default depends on context: estimation flows use the "
                "estimate_kind to pick (rent → pronajem, sale → prodej)."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.PILL_GROUP,
            agendas=_ALL_AGENDAS,
            constraints={"enum": [o.value for o in CATEGORY_TYPE_OPTIONS]},
            enum_values=CATEGORY_TYPE_OPTIONS,
        ),
        FilterDef(
            id="category_sub_cb",
            type=FilterType.INT,
            pg_column="category_sub_cb",
            default=None,
            description=(
                "Numeric sreality sub-category code (e.g. 6 = 3+kk, "
                "37 = rodinný dům). Narrows the cohort aggressively — "
                "use sparingly. The frontend renders this as a "
                "dropdown keyed by category_main."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.SINGLE_SELECT,
            agendas=_ALL_AGENDAS,
        ),
        FilterDef(
            id="dispositions",
            type=FilterType.STRING_LIST,
            pg_column="disposition",
            default=None,
            description=(
                "Multi-select disposition list (1+kk, 1+1, 2+kk, …). "
                "A listing matches if its disposition is in the list. "
                "Empty list / null = no constraint. Used by Browse + "
                "Watchdog UI; the analytical surfaces use "
                "`disposition_match` together with the target's "
                "disposition instead."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.MULTISELECT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            enum_values=DISPOSITION_OPTIONS,
        ),
        FilterDef(
            id="subtype",
            type=FilterType.STRING_LIST,
            pg_column="subtype",
            default=None,
            description=(
                "Portal-agnostic property sub-type (multi-select). Only "
                "meaningful for category_main in (dum, komercni): houses "
                "(rodinny_dum, vila, chata, chalupa, vicegeneracni_dum, "
                "zemedelska_usedlost, na_klic, pamatka_jine) and commercial "
                "(kancelar, sklad, obchodni_prostor, vyroba, ubytovani, "
                "restaurace, cinzovni_dum, apartmany, ordinace, zemedelsky, "
                "virtualni_kancelar, ostatni). A listing matches if its "
                "subtype is in the list. Normalized across portals — distinct "
                "from the sreality-only numeric category_sub_cb. The Browse "
                "sidebar renders the group matching the selected category_main "
                "(dum / komercni) and hides it otherwise."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.MULTISELECT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            enum_values=SUBTYPE_OPTIONS,
        ),
        FilterDef(
            id="portals",
            type=FilterType.STRING_LIST,
            pg_column="source",
            default=None,
            description=(
                "Restrict the cohort to listings from one or more source "
                "portals (`listings.source`): sreality, bazos, idnes, "
                "maxima, ceskereality, bezrealitky, mmreality, remax. A "
                "listing matches if its source is in the list. Empty list / "
                "null = all portals."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.MULTISELECT,
            agendas=_ALL_AGENDAS,
            enum_values=PORTAL_OPTIONS,
        ),
        FilterDef(
            id="condition_match",
            type=FilterType.STRING_LIST,
            pg_column="condition",
            default=None,
            description=(
                "Restrict cohort to listings whose `condition` is in "
                "this list. Czech values without diacritics: "
                "novostavba, po_rekonstrukci, velmi_dobry, dobry, "
                "pred_rekonstrukci, k_demolici."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.MULTISELECT,
            agendas=_ALL_AGENDAS,
            enum_values=CONDITION_OPTIONS,
        ),
        FilterDef(
            id="building_type_match",
            type=FilterType.STRING_LIST,
            pg_column="building_type",
            default=None,
            description=(
                "Restrict cohort to listings whose `building_type` is "
                "in this list. Czech values: cihla, panel, smisena, "
                "skelet, drevo, kamen, montovana, nizkoenergeticka."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.MULTISELECT,
            agendas=frozenset({Agenda.COMPARABLES, Agenda.ESTIMATION, Agenda.VELOCITY}),
            enum_values=BUILDING_TYPE_OPTIONS,
        ),
        FilterDef(
            id="building_material",
            type=FilterType.STRING_LIST,
            pg_column="building_type",  # mapped via Browse's 4-bucket grouping
            default=None,
            description=(
                "Operator-friendly building material buckets (multi-select). "
                "The four values (cihla / panel / smisena / ostatni) map onto "
                "the granular building_type column; a listing matches if its "
                "building_type is in the union of the selected buckets. "
                "`ostatni` expands to skelet / drevo / kamen / montovana / "
                "nizkoenergeticka under the hood. Empty list / null = no "
                "constraint."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.MULTISELECT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            enum_values=BUILDING_MATERIAL_OPTIONS,
        ),
        FilterDef(
            id="energy_rating_match",
            type=FilterType.STRING_LIST,
            pg_column="energy_rating",
            default=None,
            description=(
                "Restrict to energy ratings in this list (single "
                "capital letters A through G). New constructions "
                "trend A/B; pre-renovation panel buildings often G."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.MULTISELECT,
            agendas=frozenset({Agenda.COMPARABLES, Agenda.ESTIMATION, Agenda.VELOCITY}),
            enum_values=ENERGY_RATING_OPTIONS,
        ),
        FilterDef(
            id="building_condition_level_min",
            type=FilterType.INT,
            pg_column="building_condition_level",
            default=None,
            description=(
                "Minimum building condition score (`building_condition_level "
                ">= N`, 1..5). Set by toolkit.condition_scoring.score_listing_condition "
                "off the curated Czech marker dictionary + 5-level rubric. "
                "NULL rows (not yet scored) are excluded from the result. "
                "5 = výborný stav (excellent), 4 = velmi dobrý stav, "
                "3 = průměrný / udržovaný, 2 = vyžaduje rekonstrukci, "
                "1 = kritický stav."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=_ALL_AGENDAS,
            constraints={"min": 1, "max": 5},
            aliases=("buildingConditionLevelMin",),
        ),
        FilterDef(
            id="apartment_condition_level_min",
            type=FilterType.INT,
            pg_column="apartment_condition_level",
            default=None,
            description=(
                "Minimum apartment condition score (`apartment_condition_level "
                ">= N`, 1..5). Same scorer as building_condition_level_min "
                "but scoped to the unit itself rather than the building shell. "
                "NULL rows (not yet scored) are excluded from the result. "
                "5 = výborný stav (novostavba / po kompletní rekonstrukci), "
                "4 = velmi dobrý stav (po rekonstrukci, new jádro / koupelna), "
                "3 = průměrný, 2 = v původním stavu / před rekonstrukcí, "
                "1 = umakartové jádro."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=_ALL_AGENDAS,
            constraints={"min": 1, "max": 5},
            aliases=("apartmentConditionLevelMin",),
        ),
        FilterDef(
            id="furnished",
            type=FilterType.STRING_LIST,
            pg_column="furnished",
            default=None,
            description=(
                "Multi-select furnishing status. `ano` = furnished, `ne` = "
                "unfurnished, `castecne` = partially furnished. A listing "
                "matches if its value is in the list. The special "
                "`__unknown__` value matches listings whose furnishing is "
                "not specified (NULL) or stored under a non-canonical label. "
                "Empty list / null = no constraint."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.MULTISELECT,
            agendas=_ALL_AGENDAS,
            enum_values=FURNISHED_OPTIONS,
        ),
        FilterDef(
            id="ownership",
            type=FilterType.STRING_LIST,
            pg_column="ownership",
            default=None,
            description=(
                "Multi-select ownership type. `osobni` = personal (full "
                "title), `druzstevni` = cooperative (member share), `statni` "
                "= state/municipal. A listing matches if its value is in the "
                "list. The special `__unknown__` value matches listings whose "
                "ownership is not specified (NULL) or stored under a "
                "non-canonical label. Materially affects sale prices "
                "(druzstevni typically 10–20% cheaper than osobni). Empty "
                "list / null = no constraint."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.MULTISELECT,
            agendas=_ALL_AGENDAS,
            enum_values=OWNERSHIP_OPTIONS,
        ),

        # --- amenities (tri-state booleans) -------------------------------
        FilterDef(
            id="has_balcony",
            type=FilterType.BOOL,
            pg_column="has_balcony",
            default=None,
            description=(
                "Legacy combined flag: balcony OR terrace OR loggia. "
                "Kept for backwards compatibility; prefer the granular "
                "`terrace` filter when only a terrace will do."
            ),
            category=CATEGORY_AMENITY,
            ui_control=UiControl.TRISTATE,
            agendas=_ALL_AGENDAS,
            aliases=("balcony",),
        ),
        FilterDef(
            id="has_lift",
            type=FilterType.BOOL,
            pg_column="has_lift",
            default=None,
            description="Elevator. True / false / null.",
            category=CATEGORY_AMENITY,
            ui_control=UiControl.TRISTATE,
            agendas=_ALL_AGENDAS,
            aliases=("lift",),
        ),
        FilterDef(
            id="has_parking",
            type=FilterType.BOOL,
            pg_column="has_parking",
            default=None,
            description=(
                "Legacy combined flag: any parking (street, lot, or "
                "garage). Prefer the granular `garage` and "
                "`parking_lots_min` filters for new analytical work."
            ),
            category=CATEGORY_AMENITY,
            ui_control=UiControl.TRISTATE,
            agendas=_ALL_AGENDAS,
            aliases=("parking",),
        ),
        FilterDef(
            id="terrace",
            type=FilterType.BOOL,
            pg_column="terrace",
            default=None,
            description="Dedicated terrace (not just a balcony).",
            category=CATEGORY_AMENITY,
            ui_control=UiControl.TRISTATE,
            agendas=_ALL_AGENDAS,
        ),
        FilterDef(
            id="cellar",
            type=FilterType.BOOL,
            pg_column="cellar",
            default=None,
            description="Cellar / basement storage.",
            category=CATEGORY_AMENITY,
            ui_control=UiControl.TRISTATE,
            agendas=_ALL_AGENDAS,
        ),
        FilterDef(
            id="garage",
            type=FilterType.BOOL,
            pg_column="garage",
            default=None,
            description="Enclosed garage (distinct from open parking lot).",
            category=CATEGORY_AMENITY,
            ui_control=UiControl.TRISTATE,
            agendas=_ALL_AGENDAS,
        ),
        FilterDef(
            id="min_parking_lots",
            type=FilterType.INT,
            pg_column="parking_lots",
            default=None,
            description=(
                "Minimum number of parking spaces (`parking_lots >= N`). "
                "Includes open lots, covered, and garages; for an "
                "enclosed-garage-only constraint use `garage=true`."
            ),
            category=CATEGORY_AMENITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=_ALL_AGENDAS,
            constraints={"min": 0},
            aliases=("parking_lots_min", "parkingLotsMin"),
        ),

        # --- price / area ranges ------------------------------------------
        FilterDef(
            id="min_price_czk",
            type=FilterType.INT,
            pg_column="price_czk",
            default=None,
            description=(
                "Lower bound on listing price in CZK. For rentals this "
                "is the monthly rent; for sales the total asking price. "
                "Inclusive."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_INPUTS,
            agendas=_ALL_AGENDAS,
            constraints={"min": 0, "max": 100_000, "step": 500},
            unit="CZK",
            aliases=("price_min", "priceMin"),
        ),
        FilterDef(
            id="max_price_czk",
            type=FilterType.INT,
            pg_column="price_czk",
            default=None,
            description=(
                "Upper bound on listing price in CZK. See "
                "`min_price_czk` for the rental-vs-sale semantics. "
                "Inclusive."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_INPUTS,
            agendas=_ALL_AGENDAS,
            constraints={"min": 0, "max": 100_000, "step": 500},
            unit="CZK",
            aliases=("price_max", "priceMax"),
        ),
        FilterDef(
            id="min_price_per_m2",
            type=FilterType.FLOAT,
            pg_column="price_per_m2",
            default=None,
            description=(
                "Lower bound on price per square metre (`price_czk / "
                "area_m2`). Inclusive. Listings with NULL area_m2 fall "
                "out when this bound is set. Useful for sale filtering "
                "where absolute price varies wildly with size but "
                "unit price is the comparable metric."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_INPUTS,
            agendas=_ALL_AGENDAS,
            constraints={"min": 0, "max": 500_000, "step": 1_000},
            unit="CZK/m²",
            aliases=("price_per_m2_min", "pricePerM2Min"),
        ),
        FilterDef(
            id="max_price_per_m2",
            type=FilterType.FLOAT,
            pg_column="price_per_m2",
            default=None,
            description=(
                "Upper bound on price per square metre. See "
                "`min_price_per_m2`. Inclusive."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_INPUTS,
            agendas=_ALL_AGENDAS,
            constraints={"min": 0, "max": 500_000, "step": 1_000},
            unit="CZK/m²",
            aliases=("price_per_m2_max", "pricePerM2Max"),
        ),
        FilterDef(
            id="min_mf_gross_yield_pct",
            type=FilterType.FLOAT,
            pg_column="mf_gross_yield_pct",
            default=None,
            description=(
                "Lower bound on MF gross rental yield % (`mf_gross_yield_pct"
                " >= N`). Inclusive. Yield = MF Cenová mapa reference monthly"
                " rent × 12 / asking price × 100; sale apartments only, so"
                " non-apartment / rental / unpriced listings fall out when"
                " set. Browse + Watchdog only."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_SLIDER,
            agendas=_UI_AGENDAS,
            constraints={"min": 0, "max": 12, "step": 0.1},
            unit="%",
            aliases=("mf_gross_yield_pct_min", "mfGrossYieldPctMin"),
        ),
        FilterDef(
            id="max_mf_gross_yield_pct",
            type=FilterType.FLOAT,
            pg_column="mf_gross_yield_pct",
            default=None,
            description=(
                "Upper bound on MF gross rental yield %. See "
                "`min_mf_gross_yield_pct`. Inclusive."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_SLIDER,
            agendas=_UI_AGENDAS,
            constraints={"min": 0, "max": 12, "step": 0.1},
            unit="%",
            aliases=("mf_gross_yield_pct_max", "mfGrossYieldPctMax"),
        ),
        FilterDef(
            id="min_area_m2",
            type=FilterType.FLOAT,
            pg_column="area_m2",
            default=None,
            description=(
                "Absolute floor on `area_m2` (square metres) — the "
                "basis-aware interior dwelling area (usable, else floor "
                "or total; see `area_basis`), NULL for land. Use "
                "`min_estate_area` for plot size. Distinct from the "
                "target-relative `area_band_pct` used by the analytical "
                "surfaces; this is for Browse / Watchdog."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_INPUTS,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 300, "step": 5},
            unit="m²",
            aliases=("area_min", "areaMin"),
        ),
        FilterDef(
            id="max_area_m2",
            type=FilterType.FLOAT,
            pg_column="area_m2",
            default=None,
            description=(
                "Absolute ceiling on `area_m2` (square metres). See "
                "`min_area_m2`."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_INPUTS,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 300, "step": 5},
            unit="m²",
            aliases=("area_max", "areaMax"),
        ),
        FilterDef(
            id="min_estate_area",
            type=FilterType.FLOAT,
            pg_column="estate_area",
            default=None,
            description=(
                "Lower bound on plot area in m². Mostly relevant for "
                "`category_main='dum'` (houses) and `pozemek` (land) — "
                "apartments usually have null estate_area."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_INPUTS,
            agendas=_ALL_AGENDAS,
            constraints={"min": 0, "max": 5000, "step": 50},
            unit="m²",
            aliases=("estate_min",),
        ),
        FilterDef(
            id="max_estate_area",
            type=FilterType.FLOAT,
            pg_column="estate_area",
            default=None,
            description="Upper bound on plot area in m². See `min_estate_area`.",
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_INPUTS,
            agendas=_ALL_AGENDAS,
            constraints={"min": 0, "max": 5000, "step": 50},
            unit="m²",
            aliases=("estate_max",),
        ),
        FilterDef(
            id="min_garden_area",
            type=FilterType.FLOAT,
            pg_column="garden_area",
            default=None,
            description=(
                "Lower bound on garden_area in m². Populated by the "
                "scraper for listings with a dedicated garden plot — "
                "usually houses and ground-floor apartments."
            ),
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_SLIDER,
            agendas=_ALL_AGENDAS,
            constraints={"min": 0, "max": 5000, "step": 50},
            unit="m²",
        ),
        FilterDef(
            id="max_garden_area",
            type=FilterType.FLOAT,
            pg_column="garden_area",
            default=None,
            description="Upper bound on garden_area in m². See `min_garden_area`.",
            category=CATEGORY_PROPERTY,
            ui_control=UiControl.RANGE_SLIDER,
            agendas=_ALL_AGENDAS,
            constraints={"min": 0, "max": 5000, "step": 50},
            unit="m²",
        ),

        # --- locality ids (server-side, agent-friendly) ------------------
        FilterDef(
            id="locality_district_id",
            type=FilterType.INT,
            pg_column="locality_district_id",
            default=None,
            description=(
                "Sreality district id. Stable across district renames "
                "(unlike the human-readable `district` text). Useful "
                "for constraining a cohort to one municipality "
                "without geocoding."
            ),
            category=CATEGORY_SPATIAL,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({
                Agenda.COMPARABLES, Agenda.ESTIMATION,
                Agenda.VELOCITY, Agenda.WATCHDOG,
            }),
        ),
        FilterDef(
            id="locality_region_id",
            type=FilterType.INT,
            pg_column="locality_region_id",
            default=None,
            description="Sreality region id. Broader than district.",
            category=CATEGORY_SPATIAL,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({
                Agenda.COMPARABLES, Agenda.ESTIMATION,
                Agenda.VELOCITY, Agenda.WATCHDOG,
            }),
        ),

        # --- curation -----------------------------------------------------
        FilterDef(
            id="tags",
            type=FilterType.INT_LIST,
            pg_column=None,  # joined via listing_tags
            default=None,
            description=(
                "Operator-curated tag ids. AND-semantics — a listing "
                "matches only if it carries every tag in the list. "
                "Tag ids are stable across renames."
            ),
            category=CATEGORY_CURATION,
            ui_control=UiControl.MULTISELECT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
        ),

        # --- city quality (BROWSE / WATCHDOG only — by design, the
        # --- estimation agent / comparables tool do not see these) -------
        FilterDef(
            id="city_index_rules",
            type=FilterType.CITY_INDEX_RULE_LIST,
            pg_column=None,
            default=None,
            description=(
                "Filter listings to those located in a curated city whose "
                "qualitative indexes meet every rule in the list. Each "
                "rule is `{index_name: str, op: '>='|'<=', value: float}`. "
                "Rules are AND'd. `index_name` is a slug from "
                "`city_index_definitions_public` (e.g. `bezpecnost`, "
                "`prakticti_lekari`). Listings outside the curated city "
                "set (matched via ST_DWithin to the nearest curated "
                "city's centroid using its `default_radius_m`) are "
                "excluded when this filter is active."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.CITY_INDEX_RULES,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
        ),
        FilterDef(
            id="min_city_population",
            type=FilterType.INT,
            pg_column="home_obec_pop",
            default=None,
            description=(
                "Lower bound on the population of the listing's OWN "
                "municipality (`home_obec_pop >= N`, ČSÚ population of the "
                "obec whose polygon contains the listing). Precomputed by "
                "recompute_city_proximity (migration 142) for every listing "
                "country-wide — not just the 206 curated cities."
            ),
            category=CATEGORY_CITY_QUALITY,
            # Plain paired number inputs, not a 0–1.5M slider: the range is
            # too wide for a slider to be usable, the operator types exact
            # populations. RANGE_INPUTS keeps the min/max pair but drops the
            # dual-thumb track (see FilterForm's slider-vs-inputs branch).
            ui_control=UiControl.RANGE_INPUTS,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 1500000, "step": 1000},
            aliases=("minCityPopulation",),
        ),
        FilterDef(
            id="max_city_population",
            type=FilterType.INT,
            pg_column="home_obec_pop",
            default=None,
            description="Upper bound on the listing's own-municipality population (`home_obec_pop <= N`). See `min_city_population`.",
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.RANGE_INPUTS,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 1500000, "step": 1000},
            aliases=("maxCityPopulation",),
        ),
        FilterDef(
            id="near_city_proximity",
            type=FilterType.NEAR_CITY_PROXIMITY,
            pg_column=None,
            default=None,
            description=(
                "Restrict listings to those within `radius_km` km of "
                "any curated city matching the inner index rules and "
                "the optional population minimum. Composite shape: "
                "`{index_rules: [{index_name, op, value}, ...], "
                "population_min: int|null, radius_km: int}`. Index "
                "rules are AND'd. Implementation uses "
                "`ST_DWithin(listing.geom, ST_Union(matching_cities."
                "centroid), radius_km*1000)`."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.NEAR_CITY_RULE,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
        ),

        # --- fast polygon-edge proximity (migration 142) -----------------
        # Precomputed columns on `properties`: each stores the MAX metric
        # found within a FIXED radius (5 / 15 km) of the listing, measured to
        # the municipality POLYGON EDGE (0 m when inside). The threshold stays
        # dynamic — the filter is `column >= value`. Population proximity
        # considers obce with pop >= 10000; index proximity the 206 curated
        # cities. Unlike near_city_proximity (centroid + per-request RPC),
        # these are plain indexed-column predicates: no anon timeout.
        FilterDef(
            id="near_pop_5km_min",
            type=FilterType.INT,
            pg_column="near_pop_5km",
            default=None,
            description=(
                "Within 5 km of a municipality whose population is at least N "
                "(`near_pop_5km >= N`; polygon-edge distance). E.g. 50000 for "
                "'within 5 km of a town of 50k+'."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 10000, "max": 1500000, "step": 10000},
            aliases=("nearPop5kmMin",),
        ),
        FilterDef(
            id="near_pop_15km_min",
            type=FilterType.INT,
            pg_column="near_pop_15km",
            default=None,
            description=(
                "Within 15 km of a municipality whose population is at least N "
                "(`near_pop_15km >= N`; polygon-edge distance)."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 10000, "max": 1500000, "step": 10000},
            aliases=("nearPop15kmMin",),
        ),
        FilterDef(
            id="near_jobs_5km_min",
            type=FilterType.FLOAT,
            pg_column="near_jobs_5km",
            default=None,
            description=(
                "Within 5 km of a curated city whose job-offer index "
                "(`pracovni_mista`, 0–10) is at least T (`near_jobs_5km >= T`)."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 10, "step": 0.5},
            aliases=("nearJobs5kmMin",),
        ),
        FilterDef(
            id="near_jobs_15km_min",
            type=FilterType.FLOAT,
            pg_column="near_jobs_15km",
            default=None,
            description=(
                "Within 15 km of a curated city whose job-offer index "
                "(`pracovni_mista`, 0–10) is at least T (`near_jobs_15km >= T`)."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 10, "step": 0.5},
            aliases=("nearJobs15kmMin",),
        ),
        FilterDef(
            id="near_youth_5km_min",
            type=FilterType.FLOAT,
            pg_column="near_youth_5km",
            default=None,
            description=(
                "Within 5 km of a curated city whose young-migration index "
                "(`stehovani_mladych`, 0–10) is at least T (`near_youth_5km >= T`)."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 10, "step": 0.5},
            aliases=("nearYouth5kmMin",),
        ),
        FilterDef(
            id="near_youth_15km_min",
            type=FilterType.FLOAT,
            pg_column="near_youth_15km",
            default=None,
            description=(
                "Within 15 km of a curated city whose young-migration index "
                "(`stehovani_mladych`, 0–10) is at least T (`near_youth_15km >= T`)."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 10, "step": 0.5},
            aliases=("nearYouth15kmMin",),
        ),
        FilterDef(
            id="near_overall_5km_min",
            type=FilterType.FLOAT,
            pg_column="near_overall_5km",
            default=None,
            description=(
                "Within 5 km of a curated city whose overall quality index "
                "(`celkove_hodnoceni`, 0–10) is at least T (`near_overall_5km >= T`)."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 10, "step": 0.5},
            aliases=("nearOverall5kmMin",),
        ),
        FilterDef(
            id="near_overall_15km_min",
            type=FilterType.FLOAT,
            pg_column="near_overall_15km",
            default=None,
            description=(
                "Within 15 km of a curated city whose overall quality index "
                "(`celkove_hodnoceni`, 0–10) is at least T (`near_overall_15km >= T`)."
            ),
            category=CATEGORY_CITY_QUALITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0, "max": 10, "step": 0.5},
            aliases=("nearOverall15kmMin",),
        ),

        # --- multi-portal / price-history signals (Slice 2a/2b) ---------
        # Derived columns on `properties`, maintained by the recompute job
        # (scripts/recompute_property_stats.py). BROWSE (2a) + WATCHDOG (2b):
        # the property-grain matcher reads them off properties_public.
        FilterDef(
            id="distinct_site_count_min",
            type=FilterType.INT,
            pg_column="distinct_site_count",
            default=None,
            description=(
                "Minimum number of distinct source sites a property is "
                "listed on (`distinct_site_count >= N`). 1 today for every "
                "property (sreality-only); lights up once multi-portal "
                "ingestion lands. Use 2+ for 'listed on multiple sites'."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 1},
            aliases=("distinctSiteCountMin",),
        ),
        FilterDef(
            id="price_drop_count_min",
            type=FilterType.INT,
            pg_column="price_drop_count",
            default=None,
            description=(
                "Minimum number of price decreases across the property's "
                "combined snapshot history (`price_drop_count >= N`). One "
                "count per consecutive snapshot pair where the asking price "
                "fell. Use 2+ for repeatedly-cut listings."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 1},
            aliases=("priceDropCountMin",),
        ),
        FilterDef(
            id="price_rise_count_min",
            type=FilterType.INT,
            pg_column="price_rise_count",
            default=None,
            description=(
                "Minimum number of price increases across the property's "
                "combined snapshot history (`price_rise_count >= N`)."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 1},
            aliases=("priceRiseCountMin",),
        ),
        FilterDef(
            id="max_price_drop_pct_min",
            type=FilterType.FLOAT,
            pg_column="max_price_drop_pct",
            default=None,
            description=(
                "Minimum largest single-step price drop, as a percent "
                "(`max_price_drop_pct >= X`). The biggest one-change cut in "
                "the property's combined snapshot history. Use 10 for "
                "'price dropped 10%+ at some point'."
            ),
            category=CATEGORY_VELOCITY,
            ui_control=UiControl.NUMBER_INPUT,
            agendas=frozenset({Agenda.BROWSE, Agenda.WATCHDOG}),
            constraints={"min": 0},
            unit="%",
            aliases=("maxPriceDropPctMin",),
        ),

        # --- reliability flag --------------------------------------------
        FilterDef(
            id="include_unreliable",
            type=FilterType.BOOL,
            pg_column=None,
            default=False,
            description=(
                "When false (default), excludes listings whose detail "
                "fetches have been given up "
                "(`listing_fetch_failures.given_up = true`). Set true "
                "only for forensic queries where you want every row "
                "regardless of fetch state."
            ),
            category=CATEGORY_STATUS,
            ui_control=UiControl.BOOLEAN,
            agendas=frozenset({Agenda.COMPARABLES, Agenda.ESTIMATION, Agenda.VELOCITY}),
        ),
    ]

    return {e.id: e for e in entries}


REGISTRY: dict[str, FilterDef] = _build_registry()


# --- public helpers -------------------------------------------------------


def all_filters() -> list[FilterDef]:
    """Every filter in declaration order."""
    return list(REGISTRY.values())


def by_id(filter_id: str) -> FilterDef:
    """Lookup by canonical id. Raises KeyError for unknown ids."""
    return REGISTRY[filter_id]


def description(filter_id: str) -> str:
    """Shortcut for `REGISTRY[id].description` with a clear KeyError."""
    return REGISTRY[filter_id].description


def filters_for_agenda(agenda: Agenda) -> list[FilterDef]:
    """Every filter declared for the agenda, ignoring visibility overrides."""
    return [f for f in REGISTRY.values() if agenda in f.agendas]


def visibility_map(
    conn: "psycopg.Connection | None" = None,
) -> dict[tuple[str, str], bool]:
    """Read the `filter_visibility` table.

    Returns `{(agenda, filter_id): enabled}`. When `conn` is None or
    the table is missing (e.g. running pre-migration), returns an
    empty dict — `effective_for` then treats every (agenda, filter)
    pair as enabled (the all-on default).
    """
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agenda, filter_id, enabled FROM filter_visibility"
            )
            rows = cur.fetchall()
    except Exception:
        return {}
    return {(r[0], r[1]): bool(r[2]) for r in rows}


def effective_for(
    agenda: Agenda,
    conn: "psycopg.Connection | None" = None,
) -> list[FilterDef]:
    """Filters available for `agenda` AFTER the visibility matrix.

    Missing rows in `filter_visibility` are treated as enabled, so a
    fresh deploy with the seed migration but no operator edits behaves
    exactly like a deploy without the table.
    """
    visibility = visibility_map(conn)
    out: list[FilterDef] = []
    for f in REGISTRY.values():
        if agenda not in f.agendas:
            continue
        if visibility.get((str(agenda), f.id), True):
            out.append(f)
    return out


# --- JSON dump (used by /admin/filter-schema + scripts/generate_…) -------


def _enum_to_json(e: EnumOption) -> dict[str, Any]:
    return {"value": e.value, "label_cs": e.label_cs, "label_en": e.label_en}


def _filter_to_json(f: FilterDef) -> dict[str, Any]:
    return {
        "id": f.id,
        "type": str(f.type),
        "pg_column": f.pg_column,
        "default": f.default,
        "description": f.description,
        "category": f.category,
        "ui_control": str(f.ui_control),
        "agendas": sorted(str(a) for a in f.agendas),
        "constraints": dict(f.constraints) if f.constraints else None,
        "unit": f.unit,
        "enum_values": (
            [_enum_to_json(e) for e in f.enum_values]
            if f.enum_values else None
        ),
        "aliases": list(f.aliases) if f.aliases else [],
    }


def registry_to_json(
    visibility: dict[tuple[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Serialise the registry to a JSON-friendly dict.

    When `visibility` is provided, each filter entry gets an extra
    `visibility` map of `{agenda: enabled}` for the agendas it
    declares — convenient for the Settings UI which renders the
    agenda × filter matrix.
    """
    vis = visibility or {}
    filters_payload: list[dict[str, Any]] = []
    for f in REGISTRY.values():
        entry = _filter_to_json(f)
        entry["visibility"] = {
            str(a): vis.get((str(a), f.id), True)
            for a in sorted(f.agendas)
        }
        filters_payload.append(entry)
    return {
        "agendas": [str(a) for a in Agenda],
        "categories": [
            CATEGORY_SPATIAL, CATEGORY_PROPERTY, CATEGORY_AMENITY,
            CATEGORY_VELOCITY, CATEGORY_STATUS, CATEGORY_CURATION,
            CATEGORY_COHORT,
        ],
        "ui_controls": [str(u) for u in UiControl],
        "filters": filters_payload,
    }


# --- JSON Schema rendering for agent tool input_schemas -------------------
# `to_jsonschema_property` turns one FilterDef into the {type, description,
# minimum, maximum, enum, items, …} block that lives under each tool's
# `properties` dict in the agent's input_schema. `to_jsonschema_properties`
# does the same for every filter declared for a given agenda, which is
# usually what an agent tool wants. The agent's hand-written `_build_tool_
# registry` previously hard-coded these descriptions per tool; centralising
# them here means a description tweak in `filter_registry.py` flows to every
# agent and every operator-facing surface in a single PR.

_JSON_TYPE_MAP: dict[FilterType, str] = {
    FilterType.INT: "integer",
    FilterType.FLOAT: "number",
    FilterType.BOOL: "boolean",
    FilterType.STRING: "string",
    FilterType.STRING_LIST: "array",
    FilterType.INT_LIST: "array",
    FilterType.LOCATION: "object",
    FilterType.DISTRICT_CHIP_LIST: "array",
}


def to_jsonschema_property(f: FilterDef) -> dict[str, Any]:
    """Render one FilterDef as a JSON Schema property."""
    prop: dict[str, Any] = {
        "type": _JSON_TYPE_MAP[f.type],
        "description": f.description,
    }
    constraints = f.constraints or {}
    if "min" in constraints:
        prop["minimum"] = constraints["min"]
    if "max" in constraints:
        prop["maximum"] = constraints["max"]
    if "enum" in constraints:
        prop["enum"] = list(constraints["enum"])
    if f.type == FilterType.STRING_LIST:
        items: dict[str, Any] = {"type": "string"}
        if f.enum_values:
            items["enum"] = [o.value for o in f.enum_values]
        prop["items"] = items
    elif f.type == FilterType.INT_LIST:
        prop["items"] = {"type": "integer"}
    elif f.type == FilterType.DISTRICT_CHIP_LIST:
        prop["items"] = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "context": {"type": ["string", "null"]},
            },
            "required": ["name"],
            "additionalProperties": False,
        }
    return prop


def to_jsonschema_properties(
    agenda: Agenda,
    *,
    conn: "psycopg.Connection | None" = None,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """Render every (visible) filter for `agenda` as a `properties` dict.

    `exclude` skips named filters — useful for composite filters
    (LOCATION) and host-specific knobs that the tool resolves
    server-side (e.g. `target` lat/lng).
    """
    out: dict[str, dict[str, Any]] = {}
    for f in effective_for(agenda, conn):
        if f.id in exclude:
            continue
        if f.type == FilterType.LOCATION:
            # Composite — agents don't pass it directly.
            continue
        out[f.id] = to_jsonschema_property(f)
    return out


__all__ = [
    "Agenda",
    "UiControl",
    "FilterType",
    "EnumOption",
    "FilterDef",
    "REGISTRY",
    "all_filters",
    "by_id",
    "description",
    "filters_for_agenda",
    "effective_for",
    "visibility_map",
    "registry_to_json",
    "to_jsonschema_property",
    "to_jsonschema_properties",
    "CATEGORY_MAIN_OPTIONS",
    "CATEGORY_TYPE_OPTIONS",
    "FURNISHED_OPTIONS",
    "OWNERSHIP_OPTIONS",
    "FURNISHED_CANONICAL",
    "OWNERSHIP_CANONICAL",
    "UNKNOWN_FILTER_VALUE",
    "BUILDING_MATERIAL_OPTIONS",
    "BUILDING_TYPE_OPTIONS",
    "CONDITION_OPTIONS",
    "ENERGY_RATING_OPTIONS",
    "DISPOSITION_OPTIONS",
    "POPULATION_OPTIONS",
    "DISPOSITION_MATCH_OPTIONS",
    "STATUS_OPTIONS",
    "PORTAL_OPTIONS",
]
