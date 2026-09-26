/* The MF "Cenová mapa nájemného" reference-rent card — ONE component wherever
 * the result appears: the property page's estimations section
 * (properties_public.mf_reference_rent) and the orphan-run estimation page
 * (estimation_runs.reference_rent, frozen per run). It renders the result by
 * SHAPE (lib/mfReference): a value's breakdown, the published range with its
 * note behind an (i), or the note alone — and nothing when there is no result.
 * It holds no reason text of its own; the notes come from the SQL. */
import { InfoHint } from '@/components/settings/SectionChrome';
import { fmtCount, fmtCzk, fmtMeasuredPricePerM2 } from '@/lib/format';
import {
  mfShape,
  type ReferenceRent,
  type ReferenceRentAdjustment,
} from '@/lib/mfReference';

const MF_ADJ_LABELS: Record<string, string> = {
  balcony: 'balkón',
  terrace: 'terasa',
  furnished: 'vybavenost',
  garage: 'garáž',
  elevator: 'výtah',
  other_material: 'jiný konstrukční materiál',
};

/* Every per-m² figure in this card is a MONTHLY rent per m² — it is a rent
 * map — so it goes through the shared formatter on the rent basis. */
const perM2 = (n: number) => fmtMeasuredPricePerM2(n, 'rent');

const pct2 = (n: number) =>
  n.toLocaleString('cs-CZ', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function MfReferenceCard({
  refRent,
  yieldPct = null,
}: {
  refRent: ReferenceRent | null;
  /* The value's gross yield (the property's). A range carries its own. */
  yieldPct?: number | null;
}) {
  const shape = mfShape(refRent);
  if (shape.kind === 'none') return null;
  if (shape.kind === 'note') {
    return (
      <div className="border border-dashed border-[var(--color-rule)] rounded-[var(--radius-sm)] p-3">
        <Eyebrow />
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">{shape.note}</p>
      </div>
    );
  }

  const ref = shape.ref;
  const adjustments = ref.adjustments ?? [];
  if (shape.kind === 'range') {
    const { range, note } = shape;
    const yieldMin = range.yield_min_pct ?? null;
    const yieldMax = range.yield_max_pct ?? null;
    return (
      <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-3">
        <Eyebrow />
        <div className="mt-1 flex items-baseline justify-between gap-3">
          <span className="inline-flex items-baseline gap-1.5 text-lg font-medium tabular-nums">
            <span>
              {fmtCount(range.rent_min_czk)}–{fmtCzk(range.rent_max_czk)}
              <span className="ml-1 text-[0.7rem] text-[var(--color-ink-3)]">/měs</span>
            </span>
            {note && <InfoHint text={note} className="self-center" />}
          </span>
          {yieldMin != null && yieldMax != null && (
            <YieldFigure>
              {pct2(yieldMin)}–{pct2(yieldMax)}
            </YieldFigure>
          )}
        </div>
        <dl className="mt-2 space-y-0.5 text-[0.72rem] tabular-nums">
          <Row label={baseLabel(ref)}>
            {fmtCount(Math.round(range.per_m2_min))}–{perM2(range.per_m2_max)}
          </Row>
          <AdjustmentRows adjustments={adjustments} />
          {ref.area_m2 != null && (
            <Row label={`× plocha ${ref.area_m2.toLocaleString('cs-CZ')} m²`} strong>
              {fmtCount(range.rent_min_czk)}–{fmtCzk(range.rent_max_czk)}
            </Row>
          )}
        </dl>
        <Source refRent={ref} />
      </div>
    );
  }

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-3">
      <Eyebrow />
      <div className="mt-1 flex items-baseline justify-between gap-3">
        <span className="text-lg font-medium tabular-nums">
          {fmtCzk(ref.monthly_rent_czk)}
          <span className="ml-1 text-[0.7rem] text-[var(--color-ink-3)]">/měs</span>
        </span>
        {yieldPct != null && <YieldFigure>{pct2(yieldPct)}</YieldFigure>}
      </div>
      <dl className="mt-2 space-y-0.5 text-[0.72rem] tabular-nums">
        <Row label={baseLabel(ref)}>{perM2(ref.base_per_m2)}</Row>
        <AdjustmentRows adjustments={adjustments} />
        <Row label="Celkem za m²" strong>{perM2(ref.total_per_m2)}</Row>
        <Row label={`× plocha ${ref.area_m2.toLocaleString('cs-CZ')} m²`}>
          {fmtCzk(ref.monthly_rent_czk)}
        </Row>
      </dl>
      <Source refRent={ref} />
    </div>
  );
}

/* The reference flat's rate — a value's one cell, or a range's published span. */
function baseLabel(ref: ReferenceRent): string {
  return `Nájemné referenčního bytu${ref.is_novostavba ? ' (novostavba)' : ''}`;
}

function Eyebrow() {
  return (
    <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
      Odhad nájmu · cenová mapa MF
    </p>
  );
}

function YieldFigure({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
      hrubý výnos{' '}
      <span className="text-[var(--color-ink)] font-medium">{children} %</span>
    </span>
  );
}

function Row({
  label,
  strong = false,
  children,
}: {
  label: string;
  strong?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={
        strong
          ? 'flex justify-between gap-3 border-t border-[var(--color-rule)] pt-0.5'
          : 'flex justify-between gap-3'
      }
    >
      <dt className={strong ? undefined : 'text-[var(--color-ink-3)]'}>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function AdjustmentRows({ adjustments }: { adjustments: ReferenceRentAdjustment[] }) {
  return (
    <>
      {adjustments.map((a) => (
        <Row key={a.attribute} label={`+ ${MF_ADJ_LABELS[a.attribute] ?? a.attribute}`}>
          +{perM2(a.czk_per_m2)}
        </Row>
      ))}
    </>
  );
}

function Source({ refRent }: { refRent: ReferenceRent }) {
  const t = refRent.territory;
  return (
    <p className="mt-1.5 text-[0.58rem] text-[var(--color-ink-4)]">
      {t ? `${t.name}${t.kraj ? `, ${t.kraj}` : ''} · ` : ''}
      {refRent.vk != null ? `VK${refRent.vk} · ` : ''}
      Ministerstvo financí
      {refRent.source_date ? ` (${refRent.source_date})` : ''}
    </p>
  );
}
