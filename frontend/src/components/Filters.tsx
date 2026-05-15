import {
  type ListingFilters,
  DEFAULT_FILTERS,
  isDefault,
  listingFiltersToRegistryView,
  applyRegistryUpdate,
} from '@/lib/filters';
import { ControlGroup } from '@/components/controls';
import { FilterForm } from '@/components/FilterForm';
import {
  DistrictTypeahead,
  TagPicker,
} from '@/components/filter-controls';

interface SidebarProps {
  filters: ListingFilters;
  onChange: (next: ListingFilters) => void;
}

export function FilterSidebar({ filters, onChange }: SidebarProps) {
  // <FilterForm> reads snake_case registry ids; Browse keeps the
  // camelCase `ListingFilters` shape its queries / URL serialisation
  // already use. The adapter in lib/filters bridges both directions
  // (tri-state amenities pivot bool|null ↔ 'any'|'yes'|'no' inside it).
  const registryView = listingFiltersToRegistryView(filters);
  const handleRegistryChange = (id: string, value: unknown) =>
    onChange(applyRegistryUpdate(filters, id, value));

  // Rich widgets the controls library can't generically express get
  // plugged in via customWidgets — keyed by registry id. The widgets
  // own their own data fetching; FilterForm just wires the value /
  // onChange through.
  const customWidgets = {
    districts: DistrictTypeahead as never,
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
          <FilterForm
            scope="browse"
            state={registryView}
            onChange={handleRegistryChange}
            includeOnly={['districts']}
            labels={{ districts: 'District' }}
            customWidgets={customWidgets}
            flat
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
            includeOnly={['furnished', 'ownership', 'building_material']}
            labels={{
              furnished: 'Furnished',
              ownership: 'Ownership',
              building_material: 'Building material',
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

