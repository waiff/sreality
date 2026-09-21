/* Registered sales near this listing — the only realized prices in the app.
 *
 * Every other number on this page is an ASKING price. These are what the
 * cadastre recorded a transfer at, read through migration 545's
 * `sold_comparables` (the ONE SQL definition; the filters below are PostgREST
 * predicates on its returned columns, never a TS re-implementation of
 * "similar"). Nothing here is adjusted, indexed or averaged into the subject:
 * a sale is an external fact and stays one.
 *
 * WHAT THE BLOCK MUST SAY OUT LOUD, because an empty table is otherwise a lie:
 *   - we have never looked here (no coverage row) — fetching is gated on the
 *     deal pipeline, so "empty" usually means "not asked for";
 *   - we looked on <date> and this cell held nothing;
 *   - we looked and took N of the M sales the source says the cell holds.
 * Plus the source's own ~30-day publication lag, which caps how fresh the
 * freshest possible row is.
 *
 * Photos are HOT-LINKED from reas.cz (no R2 copy, no `images` rows), so they
 * are plain URLs through ImageCarousel's `TaggedImageUrl[]` path and carry
 * `referrerPolicy="no-referrer"`.
 */

import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { FilterForm, type FilterState } from '@/components/FilterForm';
import { Field, Segmented } from '@/components/controls';
import Dialog, { DialogClose } from '@/components/Dialog';
import ImageCarousel from '@/components/ImageCarousel';
import { Hairline, SectionLabel } from '@/components/section';
import { Th } from '@/components/table';
import { categoryMainLabel, listingKindLabel } from '@/lib/enums';
import {
  fmtArea,
  fmtCzk,
  fmtDateSlash,
  fmtDistanceM,
  fmtMeasuredPricePerM2,
  fmtPct,
  fmtShortDate,
} from '@/lib/format';
import { ppm2BasisFromToken, type Ppm2Basis } from '@/lib/measure';
import {
  fetchSoldComparables,
  fetchSoldCoverage,
  SOLD_COMPS_LIMIT,
} from '@/lib/queries';
import type { SoldComparable, SoldCoverage } from '@/lib/types';

/* 5 km is also W2's cell margin — the obec envelope is expanded by exactly this
 * much before it is fetched, so the largest radius the operator can ask for is
 * fully covered by construction rather than by luck. */
const RADII_M = [1000, 3000, 5000] as const;
type RadiusM = (typeof RADII_M)[number];

/* reas.cz's sold catalogue is byty + domy only — measured, and the reason the
 * parser refuses any other `type` (docs/architecture.md § Data sources). */
const COVERED_CATEGORIES = new Set(['byt', 'dum']);

/* A median over four sales is a number pretending to be a statistic. */
const SUMMARY_MIN_ROWS = 5;

/* The registry's ids prettify to "Category main in" / "Min area m2". */
const FILTER_LABELS: Record<string, string> = {
  category_main_in: 'Type',
  dispositions: 'Disposition',
  subtype: 'Sub-type',
  min_area_m2: 'Area',
  min_usable_area: 'Usable area',
  max_sold_age_days: 'Sold within',
};

export default function SoldCompsBlock({
  categoryMain,
  lat,
  lng,
}: {
  categoryMain: string | null;
  /* The listing's resolved point — the caller gates on both being present. */
  lat: number;
  lng: number;
}) {
  if (!COVERED_CATEGORIES.has(categoryMain ?? '')) {
    return (
      <div>
        <SectionLabel>Registered sales</SectionLabel>
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">
          reas.cz publishes registered sales of flats and houses only — there is
          nothing to show for a {categoryMainLabel(categoryMain).toLowerCase()}.
        </p>
      </div>
    );
  }
  return <SoldComps categoryMain={categoryMain as string} lat={lat} lng={lng} />;
}

function SoldComps({
  categoryMain,
  lat,
  lng,
}: {
  categoryMain: string;
  lat: number;
  lng: number;
}) {
  const [radiusM, setRadiusM] = useState<RadiusM>(RADII_M[0]);
  /* Seeded with the subject's own kind — a flat's comparables are flats — and
   * with nothing else: a default area band would be a number this app invented,
   * and the operator can see and clear the one chip that IS set. */
  const [filters, setFilters] = useState<FilterState>({
    category_main_in: [categoryMain],
  });
  const [active, setActive] = useState<SoldComparable | null>(null);

  const coverageQ = useQuery<SoldCoverage | null, Error>({
    queryKey: ['sold-coverage', lat, lng],
    queryFn: () => fetchSoldCoverage(lat, lng),
    staleTime: 5 * 60_000,
  });

  const rowsQ = useQuery<SoldComparable[], Error>({
    queryKey: ['sold-comps', lat, lng, radiusM, filters],
    queryFn: () => fetchSoldComparables(lat, lng, radiusM, filters),
    staleTime: 5 * 60_000,
  });

  const rows = rowsQ.data ?? [];
  const summary = summarize(rows);

  return (
    <div>
      <div className="flex items-baseline justify-between gap-4">
        <SectionLabel>
          <span>Registered sales</span>
          <span className="ml-2 font-mono tabular-nums text-[var(--color-ink-4)] tracking-normal">
            ({rows.length})
          </span>
        </SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)]">
          realized prices, not asking prices
        </p>
      </div>

      <Coverage
        coverage={coverageQ.data ?? null}
        pending={coverageQ.isLoading}
        error={coverageQ.error}
      />

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Radius">
          <Segmented
            options={RADII_M.map((m) => ({ value: m, label: `${m / 1000} km` }))}
            value={radiusM}
            onChange={setRadiusM}
          />
        </Field>
        <FilterForm
          scope="sold"
          state={filters}
          labels={FILTER_LABELS}
          flat
          onChange={(updates) =>
            setFilters((prev) => {
              const next = { ...prev };
              for (const u of updates) next[u.id] = u.value;
              return next;
            })
          }
        />
      </div>

      {summary && (
        <p className="mt-4 text-sm text-[var(--color-ink-2)]">
          <span className="font-mono tabular-nums">{summary.n}</span> sales ·
          median{' '}
          <span className="font-mono tabular-nums text-[var(--color-ink)]">
            {fmtMeasuredPricePerM2(summary.medianPpm2, summary.basis)}
          </span>
        </p>
      )}

      {rowsQ.isLoading ? (
        <p className="mt-4 text-sm text-[var(--color-ink-3)]">Loading…</p>
      ) : rowsQ.error ? (
        <p className="mt-4 text-sm text-[var(--color-brick)]">
          Failed to load: {rowsQ.error.message}
        </p>
      ) : rows.length === 0 ? (
        <p className="mt-4 text-sm text-[var(--color-ink-3)]">
          No registered sale within {radiusM / 1000} km matches these filters.
        </p>
      ) : (
        <>
          <div className="mt-4 rounded-[var(--radius-md)] border border-[var(--color-rule)] overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-[var(--color-paper-2)] border-b border-[var(--color-rule)]">
                <tr>
                  <Th size="xs">Photo</Th>
                  <Th size="xs" align="right">Sold for</Th>
                  <Th size="xs" align="right">Area</Th>
                  <Th size="xs">Type</Th>
                  <Th size="xs" align="right">Sold</Th>
                  <Th size="xs" align="right">Distance</Th>
                  <Th size="xs" align="right">Ask → sold</Th>
                  <Th size="xs" align="right">Listed</Th>
                  <Th size="xs">Source</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <SoldRow
                    key={`${row.source}:${row.source_record_id}`}
                    row={row}
                    onOpen={() => setActive(row)}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {rows.length === SOLD_COMPS_LIMIT && (
            <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)]">
              The {SOLD_COMPS_LIMIT} nearest — narrow the radius or the filters
              to see the rest.
            </p>
          )}
        </>
      )}

      {active && <SoldDialog row={active} onClose={() => setActive(null)} />}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Coverage — the three answers an empty table can have                       */
/* -------------------------------------------------------------------------- */

function Coverage({
  coverage,
  pending,
  error,
}: {
  coverage: SoldCoverage | null;
  pending: boolean;
  error: Error | null;
}) {
  /* Never render "not checked yet" from a read that hasn't answered: that is
   * the one sentence here that would be a claim about the world. */
  if (pending) return null;
  if (error) {
    return (
      <p className="mt-2 text-sm text-[var(--color-brick)]">
        Coverage unavailable: {error.message}
      </p>
    );
  }
  if (!coverage) {
    return (
      <p className="mt-2 text-sm text-[var(--color-ink-3)]">
        Not checked yet — registered sales are fetched only for towns where the
        deal pipeline has a live card. Add this property to the pipeline and
        this area gets looked up.
      </p>
    );
  }
  const taken =
    coverage.source_total != null
      ? `${coverage.record_count} of the ${coverage.source_total} sales the source lists for this area`
      : `${coverage.record_count} sales for this area`;
  return (
    <p className="mt-2 text-sm text-[var(--color-ink-3)]">
      reas.cz · checked {fmtShortDate(coverage.fetched_at)} · we hold {taken} ·
      a sale reaches the source about 30 days after the transfer.
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* Rows                                                                        */
/* -------------------------------------------------------------------------- */

function SoldRow({ row, onOpen }: { row: SoldComparable; onOpen: () => void }) {
  const ppm2 = fmtMeasuredPricePerM2(
    row.price_per_m2,
    ppm2BasisFromToken(row.price_per_m2_basis),
  );
  const gap = askToSoldPct(row);
  return (
    <tr
      onClick={onOpen}
      className="cursor-pointer border-b border-[var(--color-rule-soft)] last:border-b-0 transition-colors hover:bg-[var(--color-copper-soft)]/40"
    >
      <td className="px-3 py-2 align-middle">
        {/* The row's keyboard affordance — the <tr> click is the mouse's. */}
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onOpen();
          }}
          aria-label={`Sold ${fmtDateSlash(row.sold_at)} for ${fmtCzk(row.price_czk)}`}
          className="block rounded-[var(--radius-xs)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--color-copper)]"
        >
          <Thumb row={row} />
        </button>
      </td>
      <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtCzk(row.price_czk)}
        {ppm2 !== '—' && (
          <span className="block text-[0.7rem] text-[var(--color-ink-4)] font-normal">
            {ppm2}
          </span>
        )}
      </td>
      <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {fmtArea(row.area_m2)}
        {row.area_basis && (
          <span className="block text-[0.7rem] text-[var(--color-ink-4)] font-normal">
            {row.area_basis}
          </span>
        )}
      </td>
      <td className="px-3 py-2 align-middle text-[var(--color-ink-2)]">
        {listingKindLabel(row) ?? categoryMainLabel(row.category_main)}
      </td>
      <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {fmtDateSlash(row.sold_at)}
      </td>
      <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink-3)]">
        {fmtDistanceM(row.distance_m)}
      </td>
      <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink-3)]">
        {gap == null ? '—' : fmtPct(gap, { signed: true })}
      </td>
      <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink-3)]">
        {daysListed(row) == null ? '—' : `${daysListed(row)} d`}
      </td>
      <td className="px-3 py-2 align-middle">
        <SourceLink row={row} />
      </td>
    </tr>
  );
}

function Thumb({ row }: { row: SoldComparable }) {
  const url = row.photo_urls?.[0];
  if (!url) {
    return (
      <div className="w-14 h-10 rounded-[var(--radius-xs)] bg-[var(--color-inset)]" />
    );
  }
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      referrerPolicy="no-referrer"
      className="w-14 h-10 object-cover rounded-[var(--radius-xs)] bg-[var(--color-inset)]"
    />
  );
}

function SourceLink({ row }: { row: SoldComparable }) {
  if (!row.source_url) {
    return <span className="text-[var(--color-ink-4)]">—</span>;
  }
  return (
    <a
      href={row.source_url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      className="text-[0.78rem] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
    >
      {row.source}
    </a>
  );
}

/* -------------------------------------------------------------------------- */
/* The one sale, opened from a row                                            */
/* -------------------------------------------------------------------------- */

function SoldDialog({ row, onClose }: { row: SoldComparable; onClose: () => void }) {
  const photos = (row.photo_urls ?? []).map((url) => ({
    url,
    tag: null,
    confidence: null,
    renderScore: null,
  }));
  const gap = askToSoldPct(row);
  const days = daysListed(row);
  return (
    <Dialog
      open
      onClose={onClose}
      label={`Registered sale ${row.source_record_id}`}
      className="relative w-full max-w-2xl"
    >
      <DialogClose onClick={onClose} className="absolute top-3 right-3 z-10" />
      <div className="p-6">
        <p className="font-mono tabular-nums text-lg text-[var(--color-ink)]">
          {fmtCzk(row.price_czk)}
        </p>
        <p className="mt-1 text-sm text-[var(--color-ink-2)]">
          {row.address_text ?? '—'}
        </p>
        <p className="mt-1 text-[0.78rem] text-[var(--color-ink-4)]">
          Sold {fmtDateSlash(row.sold_at)} · {fmtDistanceM(row.distance_m)} from
          this listing
        </p>

        <Hairline tight />
        {photos.length > 0 ? (
          <ImageCarousel
            images={photos}
            aspect="aspect-[3/2]"
            className="rounded-[var(--radius-sm)]"
            referrerPolicy="no-referrer"
          />
        ) : (
          <p className="text-sm text-[var(--color-ink-3)]">
            The source published no photos for this sale.
          </p>
        )}

        <Hairline tight />
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
          <Fact label="Kč/m²">
            {fmtMeasuredPricePerM2(
              row.price_per_m2,
              ppm2BasisFromToken(row.price_per_m2_basis),
            )}
          </Fact>
          <Fact label={`Area${row.area_basis ? ` (${row.area_basis})` : ''}`}>
            {fmtArea(row.area_m2)}
          </Fact>
          <Fact label="Type">
            {listingKindLabel(row) ?? categoryMainLabel(row.category_main)}
          </Fact>
          <Fact label="Last asking price">{fmtCzk(row.asking_last_czk)}</Fact>
          <Fact label="Ask → sold">
            {gap == null ? '—' : fmtPct(gap, { signed: true })}
          </Fact>
          <Fact label="Listed before sale">{days == null ? '—' : `${days} d`}</Fact>
        </dl>

        <Hairline tight />
        <p className="text-[0.78rem] text-[var(--color-ink-4)]">
          {row.source} · record {row.source_record_id} · fetched{' '}
          {fmtShortDate(row.fetched_at)}
          {row.source_url && (
            <>
              {' · '}
              <a
                href={row.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
              >
                open the source record
              </a>
            </>
          )}
        </p>
      </div>
    </Dialog>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-[0.62rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {label}
      </dt>
      <dd className="mt-0.5 font-mono tabular-nums text-[var(--color-ink-2)]">
        {children}
      </dd>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Derivations — all from the row's own published facts                       */
/* -------------------------------------------------------------------------- */

/* Negative when the sale closed BELOW the last asking price, which is the usual
 * direction. Null when the source never carried an asking price, and when that
 * price is zero — a percentage of nothing is not a discount. */
function askToSoldPct(row: SoldComparable): number | null {
  const ask = row.asking_last_czk;
  if (ask == null || ask <= 0) return null;
  return ((row.price_czk - ask) / ask) * 100;
}

function daysListed(row: SoldComparable): number | null {
  if (!row.listed_at) return null;
  const ms = new Date(row.sold_at).getTime() - new Date(row.listed_at).getTime();
  if (!Number.isFinite(ms)) return null;
  return Math.max(0, Math.round(ms / 86_400_000));
}

/* The cohort's per-m² basis is READ from the rows' published basis token, never
 * re-derived: one token means that basis, more than one means 'mixed', which
 * `fmtMeasuredPricePerM2` refuses to put a unit on. */
function summarize(
  rows: readonly SoldComparable[],
): { n: number; medianPpm2: number | null; basis: Ppm2Basis | null } | null {
  if (rows.length < SUMMARY_MIN_ROWS) return null;
  const tokens = new Set(rows.map((r) => r.price_per_m2_basis));
  const basis =
    tokens.size === 1
      ? ppm2BasisFromToken([...tokens][0])
      : ('mixed' as Ppm2Basis);
  const values = rows
    .map((r) => r.price_per_m2)
    .filter((v): v is number => v != null)
    .sort((a, b) => a - b);
  if (values.length === 0) return { n: rows.length, medianPpm2: null, basis };
  const mid = Math.floor(values.length / 2);
  const medianPpm2 =
    values.length % 2 === 1 ? values[mid] : (values[mid - 1] + values[mid]) / 2;
  return { n: rows.length, medianPpm2, basis };
}
