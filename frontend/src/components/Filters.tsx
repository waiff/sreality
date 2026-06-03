import {
  type CenterRadius,
  type ListingFilters,
  type LocationMode,
  DEFAULT_FILTERS,
  isDefault,
  listingFiltersToRegistryView,
  applyRegistryUpdates,
} from '@/lib/filters';
import type { MapySuggestion } from '@/lib/maps';
import { CollapsibleGroup, ControlGroup, Section } from '@/components/controls';
import { useQuery } from '@tanstack/react-query';
import { supabase } from '@/lib/supabase';
import { FilterForm } from '@/components/FilterForm';
import CityIndexRulesPicker from '@/components/CityIndexRulesPicker';
import {
  LocationControl,
  LocationTypeahead,
  MultiselectChips,
  TagPicker,
} from '@/components/filter-controls';
import { SUBTYPE_LABELS_BY_MAIN } from '@/lib/enums';

interface SidebarProps {
  filters: ListingFilters;
  onChange: (next: ListingFilters) => void;
  /* Browse passes this so picking a place in the District typeahead
   * also re-centres the main map. The widget still updates the chip
   * filter via the normal onChange path; this is a side channel for
   * the map navigation. Optional so other consumers (Watchdog edit
   * form, etc.) can mount the sidebar without it. */
  onLocationPick?: (s: MapySuggestion) => void;
  /* Operator-resizable width in px (Browse persists it). Optional so
   * other consumers keep the default 320px sidebar. */
  width?: number;
}

/* Which `ListingFilters` keys live in each collapsible band. Used only to
 * light the copper "active" dot on a collapsed band — the band's content
 * still reads from the shared registry. `bounds` is deliberately omitted
 * from Essentials: the viewport bbox changes on every map pan, so counting
 * it would pin the dot permanently on. */
const ESSENTIALS_KEYS = [
  'categoryMain', 'categoryType', 'status', 'portals',
  'districts', 'locationMode', 'centerRadius',
  'dispositions', 'subtype',
  'areaMin', 'areaMax', 'estateAreaMin', 'estateAreaMax',
  'usableAreaMin', 'usableAreaMax',
  'conditionMatch', 'buildingMaterial',
  'priceMin', 'priceMax', 'pricePerM2Min', 'pricePerM2Max',
  'mfGrossYieldPctMin', 'mfGrossYieldPctMax',
] as const satisfies ReadonlyArray<keyof ListingFilters>;

const PROPERTY_KEYS = [
  'furnished', 'ownership',
  'buildingConditionLevelMin', 'apartmentConditionLevelMin',
  'hasBalcony', 'hasLift', 'hasParking', 'terrace', 'cellar', 'garage',
  'parkingLotsMin',
] as const satisfies ReadonlyArray<keyof ListingFilters>;

const SIGNALS_KEYS = [
  'firstSeenMinDays', 'firstSeenMaxDays',
  'lastSeenMinDays', 'lastSeenMaxDays', 'tomDaysMin', 'tomDaysMax',
  'distinctSiteCountMin', 'priceDropCountMin', 'priceRiseCountMin',
  'maxPriceDropPctMin',
] as const satisfies ReadonlyArray<keyof ListingFilters>;

const CURATION_KEYS = [
  'tags', 'cityIndexRules', 'minCityPopulation', 'maxCityPopulation',
  'nearCityProximity',
  'nearPop5kmMin', 'nearPop15kmMin', 'nearJobs5kmMin', 'nearJobs15kmMin',
  'nearYouth5kmMin', 'nearYouth15kmMin', 'nearOverall5kmMin', 'nearOverall15kmMin',
] as const satisfies ReadonlyArray<keyof ListingFilters>;

const bandActive = (
  f: ListingFilters,
  keys: ReadonlyArray<keyof ListingFilters>,
): boolean =>
  keys.some((k) => {
    const value = f[k];
    const def = DEFAULT_FILTERS[k];
    if (Array.isArray(def)) return Array.isArray(value) && value.length > 0;
    if (def === null) return value !== null && value !== undefined;
    return value !== def;
  });

export function FilterSidebar({ filters, onChange, onLocationPick, width = 320 }: SidebarProps) {
  // <FilterForm> reads snake_case registry ids; Browse keeps the
  // camelCase `ListingFilters` shape its queries / URL serialisation
  // already use. The adapter in lib/filters bridges both directions
  // (tri-state amenities pivot bool|null ↔ 'any'|'yes'|'no' inside it).
  const registryView = listingFiltersToRegistryView(filters);
  // Batched apply: every <FilterForm> emission ships an array of
  // updates, so paired range edits (min + max in one slider drag)
  // apply atomically. Without this, sequential id/value callbacks
  // would each start from the same stale `filters` closure and the
  // second update would overwrite the first — visible as a slider
  // that refuses to move and a number input that swallows keystrokes.
  const handleRegistryChange = (
    updates: ReadonlyArray<{ id: string; value: unknown }>,
  ) => {
    let next = applyRegistryUpdates(filters, updates);
    // Yield only applies to sale apartments; clear it when leaving Prodej so
    // a hidden, still-active filter never silently zeroes the rent cohort.
    if (
      next.categoryType !== 'prodej' &&
      (next.mfGrossYieldPctMin != null || next.mfGrossYieldPctMax != null)
    ) {
      next = { ...next, mfGrossYieldPctMin: null, mfGrossYieldPctMax: null };
    }
    // Subtype only applies to houses / commercial; clear it when leaving so a
    // hidden, still-active filter never silently empties the cohort.
    if (
      next.categoryMain !== 'dum' && next.categoryMain !== 'komercni' &&
      next.subtype.length > 0
    ) {
      next = { ...next, subtype: [] };
    }
    onChange(next);
  };

  // Rich widgets the controls library can't generically express get
  // plugged in via customWidgets — keyed by registry id. The widgets
  // own their own data fetching; FilterForm just wires the value /
  // onChange through. The District field renders inline (below) so it
  // can also surface its picked-suggestion side channel to Browse.
  const customWidgets = {
    tags: TagPicker as never,
    city_index_rules: CityIndexRulesPicker as never,
  };

  return (
    <aside
      style={{ width }}
      className="shrink-0 border-r border-[var(--color-rule)] sticky top-14 self-start max-h-[calc(100dvh-3.5rem)] overflow-y-auto"
    >
      <div className="px-5 py-4 flex items-center justify-between border-b border-[var(--color-rule-soft)]">
        <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Filters
        </h2>
        {!isDefault(filters) && (
          <button
            type="button"
            onClick={() => onChange(DEFAULT_FILTERS)}
            className="text-[0.7rem] tracking-wide uppercase text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
          >
            Reset
          </button>
        )}
      </div>

      <div className="px-5">
        {/* Three-tier hierarchy: collapsible bands (top) → ControlGroups
            (mid, bordered={false} so the band owns separation) → Section
            labels. Most groups are driven by <FilterForm>: the registry
            decides which widget renders per filter, so touching
            `toolkit/filter_registry.py` flows through here without edits.
            The hand-written sections (district picker, tags picker) wrap
            rich widgets <FilterForm> doesn't yet cover. */}

        <CollapsibleGroup
          title="Essentials"
          defaultOpen
          active={bandActive(filters, ESSENTIALS_KEYS)}
        >
          <ControlGroup title="Category" bordered={false}>
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={['category_main', 'category_type', 'status']}
              labels={{
                category_main: 'Type',
                category_type: 'Listing for',
                status: 'Status',
              }}
              flat
            />
          </ControlGroup>

          <ControlGroup title="Source portal" bordered={false}>
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={['portals']}
              labels={{ portals: 'Portal' }}
              flat
            />
          </ControlGroup>

          <ControlGroup title="Location" bordered={false}>
            <Section label="District">
              <LocationTypeahead
                value={filters.districts}
                onChange={(next) =>
                  onChange({ ...filters, districts: next ?? [] })
                }
                onPick={onLocationPick}
              />
            </Section>
            <LocationModeSection
              mode={filters.locationMode}
              centerRadius={filters.centerRadius}
              onModeChange={(mode) =>
                onChange({ ...filters, locationMode: mode })
              }
              onCenterRadiusChange={(cr) =>
                onChange({ ...filters, centerRadius: cr })
              }
            />
          </ControlGroup>

          {(filters.categoryMain === 'dum' || filters.categoryMain === 'komercni') && (
            <ControlGroup title="Subtype" bordered={false}>
              <MultiselectChips
                value={filters.subtype}
                options={SUBTYPE_LABELS_BY_MAIN[filters.categoryMain].map((o) => ({
                  value: o.slug,
                  label: o.label,
                }))}
                onChange={(next) =>
                  handleRegistryChange([{ id: 'subtype', value: next }])
                }
                cols={2}
              />
            </ControlGroup>
          )}

          <ControlGroup title="Disposition" bordered={false}>
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={['dispositions']}
              labels={{
                dispositions: 'Disposition',
              }}
              flat
            />
          </ControlGroup>

          <ControlGroup title="Size" bordered={false} layout="grid">
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={['min_area_m2', 'min_estate_area', 'min_usable_area']}
              labels={{
                min_area_m2: 'Area',
                min_estate_area: 'Lot area',
                min_usable_area: 'Usable area',
              }}
              flat
            />
          </ControlGroup>

          <ControlGroup title="Condition & material" bordered={false} layout="grid">
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={['condition_match', 'building_material']}
              labels={{
                condition_match: 'Condition (Stav objektu)',
                building_material: 'Building material',
              }}
              flat
            />
          </ControlGroup>

          <ControlGroup title="Price" bordered={false} layout="grid">
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={['min_price_czk', 'min_price_per_m2']}
              labels={{
                min_price_czk: 'Price',
                min_price_per_m2: 'Price / m²',
              }}
              flat
            />
          </ControlGroup>

          {/* Yield is the MF reference rent ÷ asking price — only meaningful
              for sale apartments, so it's offered on the Prodej tab only.
              (Rentals have no asking price; their yield is always NULL.) */}
          {filters.categoryType === 'prodej' && (
            <ControlGroup title="Yield" bordered={false}>
              <FilterForm
                scope="browse"
                state={registryView}
                onChange={handleRegistryChange}
                includeOnly={['min_mf_gross_yield_pct']}
                labels={{
                  min_mf_gross_yield_pct: 'MF gross yield %',
                }}
                flat
              />
            </ControlGroup>
          )}
        </CollapsibleGroup>

        <CollapsibleGroup
          title="Property"
          active={bandActive(filters, PROPERTY_KEYS)}
        >
          <ControlGroup title="Building" bordered={false} layout="grid">
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={[
                'furnished', 'ownership',
                'building_condition_level_min', 'apartment_condition_level_min',
              ]}
              labels={{
                furnished: 'Furnished',
                ownership: 'Ownership',
                building_condition_level_min: 'Min building condition (1–5)',
                apartment_condition_level_min: 'Min apartment condition (1–5)',
              }}
              flat
            />
          </ControlGroup>

          <ControlGroup title="Amenities" bordered={false} layout="grid">
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={[
                'has_balcony', 'has_lift', 'has_parking',
                'terrace', 'cellar', 'garage',
                'min_parking_lots',
              ]}
              labels={{
                has_balcony: 'Balcony',
                has_lift: 'Lift',
                has_parking: 'Parking',
                terrace: 'Terrace',
                cellar: 'Cellar',
                garage: 'Garage',
                min_parking_lots: 'Min parking spaces',
              }}
              flat
            />
          </ControlGroup>
        </CollapsibleGroup>

        <CollapsibleGroup
          title="Market signals"
          active={bandActive(filters, SIGNALS_KEYS)}
        >
          <ControlGroup title="Velocity" bordered={false} layout="grid">
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={[
                'first_seen_min_days',
                'last_seen_min_days',
                'tom_days_min',
              ]}
              labels={{
                first_seen_min_days: 'First seen (days ago)',
                last_seen_min_days: 'Last seen (days ago)',
                tom_days_min: 'Turned in (days)',
              }}
              flat
            />
          </ControlGroup>

          <ControlGroup title="Price history & sources" bordered={false} layout="grid">
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={[
                'distinct_site_count_min',
                'price_drop_count_min',
                'price_rise_count_min',
                'max_price_drop_pct_min',
              ]}
              labels={{
                distinct_site_count_min: 'Listed on N+ sites',
                price_drop_count_min: 'Price cut N+ times',
                price_rise_count_min: 'Price raised N+ times',
                max_price_drop_pct_min: 'Biggest price drop ≥ %',
              }}
              flat
            />
          </ControlGroup>
        </CollapsibleGroup>

        <CollapsibleGroup
          title="Curation & quality"
          active={bandActive(filters, CURATION_KEYS)}
        >
          <ControlGroup title="Curation" bordered={false}>
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={['tags']}
              labels={{ tags: 'Tags' }}
              customWidgets={customWidgets}
              flat
            />
          </ControlGroup>

          <ControlGroup title="City quality" bordered={false} layout="grid">
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={[
                'min_city_population',
                'max_city_population',
                'near_pop_5km_min',
                'near_pop_15km_min',
                'near_jobs_5km_min',
                'near_jobs_15km_min',
                'near_youth_5km_min',
                'near_youth_15km_min',
                'near_overall_5km_min',
                'near_overall_15km_min',
              ]}
              labels={{
                min_city_population: 'Min population (own city)',
                max_city_population: 'Max population (own city)',
                near_pop_5km_min: 'Pop ≥ N within 5 km',
                near_pop_15km_min: 'Pop ≥ N within 15 km',
                near_jobs_5km_min: 'Jobs index ≥ T within 5 km',
                near_jobs_15km_min: 'Jobs index ≥ T within 15 km',
                near_youth_5km_min: 'Young-migration ≥ T within 5 km',
                near_youth_15km_min: 'Young-migration ≥ T within 15 km',
                near_overall_5km_min: 'Overall index ≥ T within 5 km',
                near_overall_15km_min: 'Overall index ≥ T within 15 km',
              }}
              customWidgets={customWidgets}
              flat
            />
            {/* Hint spans the full grid width so it never wedges into a
                single narrow column beside the population inputs. */}
            <div className="[grid-column:1/-1]">
              <CityPopulationHint />
            </div>
          </ControlGroup>

          {/* Legacy flexible filter — kept for the ~30 indexes the fast
              predefined filters above don't cover (bezpečnost, lékárny,
              školy, …). Matches inside the curated-city footprint via the
              city-quality RPC (slower than the precomputed columns above). */}
          <ControlGroup title="Advanced city-quality rules" bordered={false}>
            <FilterForm
              scope="browse"
              state={registryView}
              onChange={handleRegistryChange}
              includeOnly={['city_index_rules']}
              labels={{
                city_index_rules:
                  'Curated city must satisfy all rules (any of ~30 indexes)',
              }}
              customWidgets={customWidgets}
              flat
            />
          </ControlGroup>
        </CollapsibleGroup>
      </div>
    </aside>
  );
}

/* -------------------------------------------------------------------------- */
/* Phase QUAL — population-data status hint                                   */
/*                                                                            */
/* The `min_city_population` / `max_city_population` filters compare against  */
/* the latest `city_population.population` reading. When `city_population` is */
/* empty (the seed loaded the indexes but the population CSV was missing /    */
/* the Wikidata fetcher hasn't run yet), every city's `c.population` resolves */
/* to NULL, so a `min ≥ 1` predicate excludes everything — counter-intuitive  */
/* enough that the operator filed it as a bug. This banner detects the empty- */
/* population state via a one-row count query and tells the operator how to  */
/* unstick it.                                                                */
/* -------------------------------------------------------------------------- */

function CityPopulationHint() {
  const { data } = useQuery<{ withPop: number; total: number }, Error>({
    queryKey: ['curated_cities_population_status'],
    queryFn: async () => {
      const total = await supabase
        .from('curated_cities_public')
        .select('*', { count: 'exact', head: true });
      const withPop = await supabase
        .from('curated_cities_public')
        .select('*', { count: 'exact', head: true })
        .not('population', 'is', null);
      return {
        withPop: withPop.count ?? 0,
        total:   total.count   ?? 0,
      };
    },
    staleTime: 60_000,
    gcTime: Infinity,
  });

  if (!data) return null;
  if (data.total === 0) return null;

  /* Hide once at least half the curated cities have populations —
   * the workflow ran, the filter works as expected, no banner needed. */
  if (data.withPop >= data.total / 2) {
    return (
      <p className="mt-2 text-[0.65rem] leading-snug text-[var(--color-ink-3)]">
        Pop. údaje: {data.withPop} / {data.total} měst.
      </p>
    );
  }

  return (
    <div className="mt-2 p-2 rounded-[var(--radius-sm)] bg-[var(--color-copper-soft)] border border-[var(--color-copper)]/30">
      <p className="text-[0.7rem] leading-snug text-[var(--color-ink-2)]">
        <strong className="text-[var(--color-copper)]">Pop. data nejsou nahraná</strong>
        {' '}({data.withPop}/{data.total} měst). Filtr min/max populace bude
        vždy 0 výsledků. Spusť workflow{' '}
        <em className="font-mono not-italic">Refresh city populations from Wikidata</em>
        {' '}a poté{' '}
        <em className="font-mono not-italic">Seed curated cities</em>.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Location mode: viewport vs centre+radius                                   */
/*                                                                            */
/* The pill toggles which spatial predicate drives the cohort. In `viewport`  */
/* mode (default) the main map's pan/zoom emits the bbox; in `center_radius`  */
/* mode an in-sidebar small map widget lets the operator drop a dot and       */
/* dial a radius — the cohort filters to that circle (approximated as a       */
/* bbox client-side; the main map still draws the precise circle overlay).    */
/* -------------------------------------------------------------------------- */

function LocationModeSection({
  mode,
  centerRadius,
  onModeChange,
  onCenterRadiusChange,
}: {
  mode: LocationMode;
  centerRadius: CenterRadius | null;
  onModeChange: (next: LocationMode) => void;
  onCenterRadiusChange: (next: CenterRadius | null) => void;
}) {
  return (
    <Section label="Map filter">
      <div className="inline-flex items-center gap-0.5 p-0.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule)]">
        {(['viewport', 'center_radius'] as const).map((m) => {
          const on = mode === m;
          return (
            <button
              key={m}
              type="button"
              onClick={() => onModeChange(m)}
              aria-pressed={on}
              className={[
                'px-2.5 py-1 text-[0.7rem] rounded-[var(--radius-xs)] transition-colors',
                on
                  ? 'bg-[var(--color-copper)] text-white'
                  : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
              ].join(' ')}
            >
              {m === 'viewport' ? 'Map viewport' : 'Centre + radius'}
            </button>
          );
        })}
      </div>
      {mode === 'center_radius' ? (
        <div className="mt-3">
          <LocationControl
            value={centerRadius}
            onChange={onCenterRadiusChange}
            hint={
              'The cohort filters to listings inside the dashed circle. ' +
              'Click the small map to set the centre or drag the marker. ' +
              'The full-page map still shows the circle for context.'
            }
          />
        </div>
      ) : (
        <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)]">
          Filtering by whatever the main map shows.
        </p>
      )}
    </Section>
  );
}

