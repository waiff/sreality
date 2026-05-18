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
import { ControlGroup, Section } from '@/components/controls';
import { FilterForm } from '@/components/FilterForm';
import {
  LocationControl,
  LocationTypeahead,
  TagPicker,
} from '@/components/filter-controls';

interface SidebarProps {
  filters: ListingFilters;
  onChange: (next: ListingFilters) => void;
  /* Browse passes this so picking a place in the District typeahead
   * also re-centres the main map. The widget still updates the chip
   * filter via the normal onChange path; this is a side channel for
   * the map navigation. Optional so other consumers (Watchdog edit
   * form, etc.) can mount the sidebar without it. */
  onLocationPick?: (s: MapySuggestion) => void;
}

export function FilterSidebar({ filters, onChange, onLocationPick }: SidebarProps) {
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
  ) => onChange(applyRegistryUpdates(filters, updates));

  // Rich widgets the controls library can't generically express get
  // plugged in via customWidgets — keyed by registry id. The widgets
  // own their own data fetching; FilterForm just wires the value /
  // onChange through. The District field renders inline (below) so it
  // can also surface its picked-suggestion side channel to Browse.
  const customWidgets = {
    tags: TagPicker as never,
  };

  return (
    <aside className="w-[320px] shrink-0 border-r border-[var(--color-rule)] sticky top-14 self-start max-h-[calc(100dvh-3.5rem)] overflow-y-auto">
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

      <div className="px-5 py-5 space-y-7">
        {/* Category, disposition, price, size, status & velocity,
            building, amenities — all driven by <FilterForm>. The
            registry decides which widget renders per filter; touching
            `toolkit/filter_registry.py` flows through here without
            edits. The remaining hand-written sections (district picker,
            tags picker) wrap rich widgets `<FilterForm>` doesn't yet
            cover. */}

        <ControlGroup title="Category">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={['category_main', 'category_type']}
            labels={{
              category_main: 'Type',
              category_type: 'Listing for',
            }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Location">
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

        <ControlGroup title="Disposition">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={['dispositions', 'category_sub_cb']}
            labels={{
              dispositions: 'Disposition',
              category_sub_cb: 'Subtype',
            }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Price">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={['min_price_czk']}
            labels={{ min_price_czk: 'Price' }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Size">
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

        <ControlGroup title="Status & velocity">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={[
              'status',
              'first_seen_min_days',
              'last_seen_min_days',
              'tom_days_min',
            ]}
            labels={{
              status: 'Status',
              first_seen_min_days: 'First seen (days ago)',
              last_seen_min_days: 'Last seen (days ago)',
              tom_days_min: 'Turned in (days)',
            }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Building">
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={[
              'furnished', 'ownership', 'condition_match', 'building_material',
              'building_condition_level_min', 'apartment_condition_level_min',
            ]}
            labels={{
              furnished: 'Furnished',
              ownership: 'Ownership',
              condition_match: 'Condition (Stav objektu)',
              building_material: 'Building material',
              building_condition_level_min: 'Min building condition (1–5)',
              apartment_condition_level_min: 'Min apartment condition (1–5)',
            }}
            flat
          />
        </ControlGroup>

        <ControlGroup title="Amenities">
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

        <ControlGroup title="Curation">
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
      </div>
    </aside>
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

