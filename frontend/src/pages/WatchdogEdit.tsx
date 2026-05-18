import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';

import {
  createWatchdogSubscription,
  getWatchdogSubscription,
  updateWatchdogSubscription,
} from '@/lib/api';
import { watchdogKeys } from '@/lib/queries';
import {
  DEFAULT_WATCHDOG_FILTER_SPEC,
  type WatchdogFilterSpec,
} from '@/lib/types';
import { FilterForm } from '@/components/FilterForm';
import CityIndexRulesPicker from '@/components/CityIndexRulesPicker';
import {
  LocationControl,
  LocationTypeahead,
  type CenterRadius,
} from '@/components/filter-controls';

export default function WatchdogEdit() {
  const { id } = useParams<{ id?: string }>();
  const isEdit = Boolean(id);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const existingQ = useQuery({
    queryKey: id ? watchdogKeys.subscription(id) : ['watchdog', 'new'],
    queryFn: () => (id ? getWatchdogSubscription(id) : Promise.resolve(null)),
    enabled: isEdit,
  });

  const [name, setName] = useState('');
  const [spec, setSpec] = useState<WatchdogFilterSpec>(
    DEFAULT_WATCHDOG_FILTER_SPEC,
  );
  const [isActive, setIsActive] = useState(true);
  const [hydrated, setHydrated] = useState(!isEdit);

  useEffect(() => {
    if (isEdit && existingQ.data && !hydrated) {
      setName(existingQ.data.name);
      setSpec({ ...DEFAULT_WATCHDOG_FILTER_SPEC, ...existingQ.data.filter_spec });
      setIsActive(existingQ.data.is_active);
      setHydrated(true);
    }
  }, [isEdit, existingQ.data, hydrated]);

  const saveMut = useMutation({
    mutationFn: async () => {
      if (isEdit && id) {
        return updateWatchdogSubscription(id, {
          name,
          filter_spec: spec,
          is_active: isActive,
        });
      }
      return createWatchdogSubscription({
        name,
        filter_spec: spec,
        is_active: isActive,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: watchdogKeys.all });
      navigate('/watchdog/manage');
    },
  });

  const canSave = name.trim().length > 0 && !saveMut.isPending;
  const submitError = saveMut.error?.message ?? null;

  return (
    <div className="px-6 py-8 max-w-3xl mx-auto">
      <Header isEdit={isEdit} />

      <form
        className="mt-6 space-y-8"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSave) saveMut.mutate();
        }}
      >
        <Section title="Identity">
          <Row label="Name">
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. 2+kk Praha 2 under 25 000"
              className="w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
              required
            />
          </Row>
          <Row label="Status">
            <Toggle
              on={isActive}
              onChange={setIsActive}
              labelOn="Active"
              labelOff="Paused"
            />
          </Row>
        </Section>

        <Section title="Spatial centre">
          <Row label="Centre + radius">
            <LocationControl
              value={
                spec.lat != null && spec.lng != null && spec.radius_m != null
                  ? { lat: spec.lat, lng: spec.lng, radius_m: spec.radius_m }
                  : null
              }
              onChange={(next: CenterRadius | null) => {
                setSpec((prev) => ({
                  ...prev,
                  lat: next?.lat ?? null,
                  lng: next?.lng ?? null,
                  radius_m: next?.radius_m ?? null,
                }));
              }}
              hint={
                'Optional. Drag the dot or click the map to set the ' +
                'centre; drag the slider for the radius. Restricts the ' +
                'watchdog to listings inside the dashed circle. Clear ' +
                'to drop the spatial filter entirely.'
              }
            />
          </Row>
        </Section>

        <FilterForm
          scope="watchdog"
          state={spec as unknown as Record<string, unknown>}
          onChange={(updates) =>
            setSpec((prev) => {
              const next = { ...prev } as Record<string, unknown>;
              for (const u of updates) next[u.id] = u.value;
              return next as unknown as WatchdogFilterSpec;
            })
          }
          exclude={['location']}
          customWidgets={{
            // Same Mapy.cz typeahead as Browse — keeps the operator's
            // muscle memory consistent across surfaces. Tags don't
            // surface here yet because Watchdog stores numeric tag ids
            // and the rich picker needs the listings-attached colour
            // palette context.
            districts: LocationTypeahead as never,
            // Phase QUAL — city-quality rule picker. The matcher in
            // api/notifications._build_match_clauses already understands
            // the filter; surfacing the widget here completes the
            // Watchdog-side parity with Browse.
            city_index_rules: CityIndexRulesPicker as never,
          }}
          labels={{
            category_main: 'Category main',
            category_type: 'Deal',
            category_sub_cb: 'Subtype',
            dispositions: 'Dispositions',
            districts: 'District name(s)',
            locality_district_id: 'District id',
            locality_region_id: 'Region id',
            min_price_czk: 'Price Kč',
            min_area_m2: 'Area m²',
            min_usable_area: 'Usable area m²',
            min_estate_area: 'Estate area m²',
            min_garden_area: 'Garden area m²',
            min_parking_lots: 'Min parking lots',
            building_condition_level_min: 'Min building condition (1–5)',
            apartment_condition_level_min: 'Min apartment condition (1–5)',
            has_balcony: 'Balcony',
            has_lift: 'Lift',
            has_parking: 'Parking',
            terrace: 'Terrace',
            cellar: 'Cellar',
            garage: 'Garage',
            furnished: 'Furnished',
            ownership: 'Ownership',
            building_material: 'Building material',
            tags: 'Tags',
          }}
        />

        {submitError ? (
          <p className="text-sm text-[var(--color-brick)]">{submitError}</p>
        ) : null}

        <div className="flex items-center justify-between gap-3 border-t border-[var(--color-rule)] pt-5">
          <Link
            to="/watchdog/manage"
            className="text-sm text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
          >
            ← Cancel
          </Link>
          <button
            type="submit"
            disabled={!canSave}
            className="px-4 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors disabled:opacity-50"
          >
            {saveMut.isPending
              ? 'Saving…'
              : isEdit
                ? 'Save changes'
                : 'Create watchdog'}
          </button>
        </div>
      </form>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

function Header({ isEdit }: { isEdit: boolean }) {
  return (
    <header>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Watchdog
      </p>
      <h1
        className="mt-1.5 text-[2.1rem] leading-tight"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {isEdit ? 'Edit watchdog' : 'New watchdog'}
      </h1>
      <p className="mt-2 text-sm text-[var(--color-ink-2)]">
        Set the filter once; the matcher fires a notification whenever
        a newly scraped listing matches.
      </p>
    </header>
  );
}

/* -------------------------------------------------------------------------- */
/* Form scaffolding                                                           */
/* -------------------------------------------------------------------------- */

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <fieldset className="m-0 p-0 border-0 border-t border-[var(--color-rule-strong)] pt-5 first:border-t-0 first:pt-0">
      <legend
        className="block w-full mb-3 text-[0.65rem] tracking-[0.22em] uppercase text-[var(--color-ink-2)] font-medium"
        style={{ fontFamily: 'var(--font-display)' }}
      >
        {title}
      </legend>
      <div className="space-y-4">{children}</div>
    </fieldset>
  );
}

function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[140px_1fr] items-start gap-3">
      <span className="pt-1.5 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {label}
      </span>
      <div>{children}</div>
    </div>
  );
}

function Toggle({
  on,
  onChange,
  labelOn,
  labelOff,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  labelOn: string;
  labelOff: string;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!on)}
      aria-pressed={on}
      className={[
        'inline-flex items-center gap-2 px-3 py-1.5 rounded-[var(--radius-sm)] border text-sm transition-colors',
        on
          ? 'border-[var(--color-sage)]/40 text-[var(--color-sage)] bg-[var(--color-sage-soft)]/40'
          : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
      ].join(' ')}
    >
      <span
        className={[
          'w-1.5 h-1.5 rounded-full',
          on ? 'bg-[var(--color-sage)]' : 'bg-[var(--color-ink-4)]',
        ].join(' ')}
        aria-hidden
      />
      {on ? labelOn : labelOff}
    </button>
  );
}

