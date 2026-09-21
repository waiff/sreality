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
 *   - we have never looked in this municipality — fetching is gated on the deal
 *     pipeline, so "empty" usually means "not asked for";
 *   - we tried and the fetch FAILED, which is our outage and not an empty
 *     market;
 *   - we looked on <date> and this municipality held nothing;
 *   - we looked and hold N sales from the source's 24-month window, against the
 *     M it says have ever been registered there — two populations, never
 *     rendered as a shortfall.
 * Plus the two limits of the source itself: a ~30-day publication lag, which
 * caps how fresh the freshest possible row is, and the fact that reas.cz
 * matches only a minority of registered transfers, which makes every cohort
 * here a sample rather than the register.
 *
 * Photos are HOT-LINKED from reas.cz (no R2 copy, no `images` rows), so they
 * are plain URLs through ImageCarousel's `TaggedImageUrl[]` path and carry
 * `referrerPolicy="no-referrer"`.
 */

import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import {
  FilterForm,
  type CustomFilterWidget,
  type FilterState,
} from '@/components/FilterForm';
import { MultiselectChips } from '@/components/filter-controls';
import { Field, Segmented } from '@/components/controls';
import Dialog, { DialogClose } from '@/components/Dialog';
import ImageCarousel from '@/components/ImageCarousel';
import { Hairline, SectionLabel } from '@/components/section';
import { Th } from '@/components/table';
import { areaBasisLabel, categoryMainLabel, listingKindLabel } from '@/lib/enums';
import { FILTER_REGISTRY } from '@/lib/filterRegistry.generated';
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
  fetchPipelineMembers,
  fetchSoldComparables,
  fetchSoldCoverage,
  pipelineKeys,
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

/* Sales under this area are held OUT of the median (never out of the table:
 * each row is an honest fact). Their Kč/m² is a denominator defect, not a
 * market fact — a small transfer routinely bundles a cellar or a parking share,
 * or states the unit's own floor area rather than the area transferred. It is
 * measured, not assumed: Prague flats 0–30 m² median 239,600 Kč/m² against
 * 144,506 in the 60–80 m² band (design/verify-reas-field-mapping.md). */
const SMALL_UNIT_M2 = 30;

/* The registry's ids prettify to "Category main in" / "Min area m2". */
const FILTER_LABELS: Record<string, string> = {
  category_main_in: 'Type',
  dispositions: 'Disposition',
  min_area_m2: 'Area',
  min_usable_area: 'Usable area',
  max_sold_age_days: 'Sold within',
};

/* Type, narrowed to the two categories that can answer. The registry's
 * five-member option list is shared with Browse and Watchdog, which read
 * `listings` and really do hold land and commercial rows; here the other three
 * are filters that can only ever return nothing. The options are still the
 * REGISTRY's — filtered to the set this block already owns — handed to
 * FilterForm through its existing per-filter widget override, so there is no
 * second spelling of the labels and no per-agenda option machinery to build.
 * `subtype` needed no mechanism at all: it is simply not an Agenda.SOLD filter
 * any more — a flat has no subtype and reas publishes no commercial building. */
const COVERED_TYPE_OPTIONS = (
  FILTER_REGISTRY.filters.find((f) => f.id === 'category_main_in')?.enum_values ?? []
)
  .filter((o) => COVERED_CATEGORIES.has(String(o.value)))
  .map((o) => ({ value: String(o.value), label: o.label_cs }));

const FILTER_WIDGETS: Record<string, CustomFilterWidget> = {
  category_main_in: ({ value, onChange }) => (
    <MultiselectChips
      value={(value as string[] | null) ?? []}
      options={COVERED_TYPE_OPTIONS}
      onChange={(next) => onChange(next.length === 0 ? null : next)}
    />
  ),
};

export default function SoldCompsBlock({
  categoryMain,
  lat,
  lng,
  propertyId,
}: {
  categoryMain: string | null;
  /* The listing's resolved point — the caller gates on both being present. */
  lat: number;
  lng: number;
  /* Only to answer "does this property already hold a pipeline card", which is
   * what the never-checked sentence must know before it tells the operator to
   * add one. Read from the members map this page already has in cache. */
  propertyId: number | null;
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
  return (
    <SoldComps
      categoryMain={categoryMain as string}
      lat={lat}
      lng={lng}
      propertyId={propertyId}
    />
  );
}

function SoldComps({
  categoryMain,
  lat,
  lng,
  propertyId,
}: {
  categoryMain: string;
  lat: number;
  lng: number;
  propertyId: number | null;
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

  /* The SAME members query every funnel on this page reads — PipelineToggle's
   * key and its staleTime — so this is a cache hit, not a second read. */
  const membersQ = useQuery({
    queryKey: pipelineKeys.members,
    queryFn: fetchPipelineMembers,
    staleTime: 30_000,
    enabled: propertyId != null,
  });
  /* null = not answerable, so the advice clause is left unwritten: either the
   * members read is still in flight, or this listing carries no property row
   * and therefore no card to hold. */
  const inPipeline =
    propertyId == null || !membersQ.data
      ? null
      : membersQ.data.get(propertyId) != null;

  /* Whether anyone has LOOKED in this municipality — which is what a filter
   * panel and a "no match" line silently assert. A coverage read that hasn't
   * answered, or that failed, claims neither way: the cohort read is a fact of
   * its own, so it still runs and a broken coverage function cannot hide sales
   * we actually hold. */
  const looked = coverageQ.data?.fetched_at != null;
  const asked = looked || coverageQ.isError;

  const rowsQ = useQuery<SoldComparable[], Error>({
    queryKey: ['sold-comps', lat, lng, radiusM, filters],
    queryFn: () => fetchSoldComparables(lat, lng, radiusM, filters),
    staleTime: 5 * 60_000,
    enabled: asked,
  });

  /* The fetcher asks for one row past the cap, so a full page PROVES there is
   * more rather than merely suggesting it — and that extra row is not part of
   * the cohort, so it is dropped before anything counts or renders. */
  const page = rowsQ.data ?? [];
  const truncated = page.length > SOLD_COMPS_LIMIT;
  const rows = truncated ? page.slice(0, SOLD_COMPS_LIMIT) : page;
  const summary = rowsQ.isSuccess ? summarize(rows) : null;

  return (
    <div>
      <div className="flex items-baseline justify-between gap-4">
        <SectionLabel>
          <span>Registered sales</span>
          {/* Only once the read has ANSWERED: a "(0)" beside the heading while
              the query is in flight — or after it failed — is the loudest
              number on the block asserting the one thing it does not know. */}
          {rowsQ.isSuccess && (
            <span className="ml-2 font-mono tabular-nums text-[var(--color-ink-4)] tracking-normal">
              ({rows.length}
              {truncated && '+'})
            </span>
          )}
        </SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)]">
          realized prices, not asking prices
        </p>
      </div>

      <Coverage
        coverage={coverageQ.data ?? null}
        pending={coverageQ.isLoading}
        error={coverageQ.error}
        inPipeline={inPipeline}
      />

      {/* Everything below belongs to a municipality somebody has READ. A radius
          control, a filter panel and "no registered sale matches these filters"
          are three ways of saying we looked and found nothing — over a town
          nobody has fetched, that contradicts the sentence above it. */}
      {asked && (
        <>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Radius">
              <Segmented
                options={RADII_M.map((m) => ({
                  value: m,
                  label: `${m / 1000} km`,
                }))}
                value={radiusM}
                onChange={setRadiusM}
              />
            </Field>
            <FilterForm
              scope="sold"
              state={filters}
              labels={FILTER_LABELS}
              customWidgets={FILTER_WIDGETS}
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

          {summary && <SummaryLine summary={summary} truncated={truncated} />}

          {rowsQ.isLoading ? (
            <p className="mt-4 text-sm text-[var(--color-ink-3)]">Loading…</p>
          ) : rowsQ.error ? (
            <p className="mt-4 text-sm text-[var(--color-brick)]">
              Failed to load: {rowsQ.error.message}
            </p>
          ) : rows.length === 0 ? (
            <p className="mt-4 text-sm text-[var(--color-ink-3)]">
              No registered sale within {radiusM / 1000} km matches these
              filters.
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
              {truncated && (
                <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)]">
                  The {SOLD_COMPS_LIMIT} nearest — narrow the radius or the
                  filters to see the rest.
                </p>
              )}
              {/* The distance column is what the operator ranks relevance by,
                  so say what it is measured between. The sale's point is its
                  BUILDING (units in one building share it exactly); this
                  listing's own point can be a street or a town centroid. */}
              <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)]">
                Distances run between geocoded points: the sale's is its
                building, this listing's own may be street- or town-grain.
              </p>
            </>
          )}
        </>
      )}

      {active && <SoldDialog row={active} onClose={() => setActive(null)} />}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Coverage — the four answers an empty table can have                        */
/* -------------------------------------------------------------------------- */

function Coverage({
  coverage,
  pending,
  error,
  inPipeline,
}: {
  coverage: SoldCoverage | null;
  pending: boolean;
  error: Error | null;
  /* null while the pipeline members read is still in flight — the advice half
   * of the never-checked sentence is then not written at all, rather than
   * guessed at. */
  inPipeline: boolean | null;
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
  const where = coverage ? `${coverage.obec_name} ` : '';
  /* A cell that has only ever FAILED is our outage, not operator inaction —
   * telling them to add a card they may already hold would hide a broken lane
   * on the one surface that reads this ledger. */
  if (coverage && coverage.fetched_at == null && coverage.last_attempt_at) {
    return (
      <p className="mt-2 text-sm text-[var(--color-brick)]">
        {where}was last tried on {fmtShortDate(coverage.last_attempt_at)} and
        the fetch failed — nothing has been read here yet, and that is our side,
        not an empty market.
      </p>
    );
  }
  if (!coverage || coverage.fetched_at == null) {
    /* What to DO about it depends on something this page already knows.
     * Telling an operator to add a card that is on screen, already added, is
     * the block giving a wrong instruction about its own mechanism. */
    return (
      <p className="mt-2 text-sm text-[var(--color-ink-3)]">
        {where}has not been checked yet — registered sales are fetched only for
        towns where the deal pipeline has a live card.
        {inPipeline === true &&
          ' This property holds one, so the town is queued: the fetcher reads' +
            ' it on one of its next passes.'}
        {inPipeline === false &&
          ' Add this property to the pipeline and this area gets looked up.'}
      </p>
    );
  }
  return (
    <p className="mt-2 text-sm text-[var(--color-ink-3)]">
      {coverage.obec_name} · reas.cz · checked{' '}
      {fmtShortDate(coverage.fetched_at)} ·{' '}
      {coverage.truncated
        ? `we hold ${coverage.record_count} of the sales it publishes here from the last 24 months — our page cap cut that walk short, so this town is incomplete`
        : `we hold all ${coverage.record_count} sales it publishes here from the last 24 months`}
      {coverage.source_total != null && (
        <> — it says {coverage.source_total} have ever been registered here</>
      )}
      . A sale reaches the source about 30 days after the transfer, and reas.cz
      matches only a minority of registered transfers: this is a sample, not the
      register.
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
        {areaBasisLabel(row.area_basis) && (
          <span className="block text-[0.7rem] text-[var(--color-ink-4)] font-normal">
            {areaBasisLabel(row.area_basis)}
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
          <Fact
            label={`Area${areaBasisLabel(row.area_basis) ? ` (${areaBasisLabel(row.area_basis)})` : ''}`}
          >
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

/* Null — not 0 — when the interval runs backwards. A re-listed advertisement or
 * a source backfill can date the sale before the listing, and clamping that to
 * zero would manufacture "sold the day it hit the market", the single most
 * attention-grabbing velocity claim this table can make, out of bad data. */
function daysListed(row: SoldComparable): number | null {
  if (!row.listed_at) return null;
  const ms = new Date(row.sold_at).getTime() - new Date(row.listed_at).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  return Math.round(ms / 86_400_000);
}

type Summary = {
  /* The sales the median is OF — not the cohort's size. */
  n: number;
  /* Held out for being under SMALL_UNIT_M2, and said out loud. */
  heldOut: number;
  medianPpm2: number | null;
  basis: Ppm2Basis | null;
  /* Flats and houses sell at different Kč/m² (measured: 114,519 against
   * 50,000), so one median over both is not a statistic about either. */
  mixedKinds: boolean;
};

/* The cohort's per-m² basis is READ from the rows' published basis token, never
 * re-derived: one token means that basis, more than one means 'mixed', which
 * `fmtMeasuredPricePerM2` refuses to put a unit on. */
function summarize(rows: readonly SoldComparable[]): Summary | null {
  const kept = rows.filter((r) => r.area_m2 == null || r.area_m2 >= SMALL_UNIT_M2);
  if (kept.length < SUMMARY_MIN_ROWS) return null;
  const tokens = new Set(kept.map((r) => r.price_per_m2_basis));
  const basis =
    tokens.size === 1
      ? ppm2BasisFromToken([...tokens][0])
      : ('mixed' as Ppm2Basis);
  const shape = {
    n: kept.length,
    heldOut: rows.length - kept.length,
    basis,
    mixedKinds: new Set(rows.map((r) => r.category_main)).size > 1,
  };
  const values = kept
    .map((r) => r.price_per_m2)
    .filter((v): v is number => v != null)
    .sort((a, b) => a - b);
  if (values.length === 0) return { ...shape, medianPpm2: null };
  const mid = Math.floor(values.length / 2);
  return {
    ...shape,
    n: values.length,
    medianPpm2:
      values.length % 2 === 1 ? values[mid] : (values[mid - 1] + values[mid]) / 2,
  };
}

function SummaryLine({
  summary,
  truncated,
}: {
  summary: Summary;
  truncated: boolean;
}) {
  if (summary.mixedKinds) {
    return (
      <p className="mt-4 text-sm text-[var(--color-ink-2)]">
        No median: these sales are flats and houses together, and the two trade
        at different Kč/m². Filter to one kind.
      </p>
    );
  }
  return (
    <p className="mt-4 text-sm text-[var(--color-ink-2)]">
      median{' '}
      <span className="font-mono tabular-nums text-[var(--color-ink)]">
        {fmtMeasuredPricePerM2(summary.medianPpm2, summary.basis)}
      </span>{' '}
      over {summary.n} sales
      {truncated && ` — the ${SOLD_COMPS_LIMIT} nearest, not the whole cohort`}
      {summary.heldOut > 0 && (
        <span className="text-[var(--color-ink-3)]">
          {' '}
          · {summary.heldOut} under {SMALL_UNIT_M2} m² held out: a small unit's
          stated area is often not the area transferred
        </span>
      )}
    </p>
  );
}
