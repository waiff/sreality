/* The MF "Cenová mapa nájemného" reference-rent card — ONE component for the
 * formula breakdown wherever it appears: the Listing Detail estimations
 * section (listings.mf_reference_rent, migration 134) and the orphan-run
 * estimation page (estimation_runs.reference_rent, migration 131). Both
 * columns carry the same JSON shape; the structural type below accepts
 * either, so the two surfaces can never drift apart. */
import { fmtCzk } from '@/lib/format';

const MF_ADJ_LABELS: Record<string, string> = {
  balcony: 'balkón',
  terrace: 'terasa',
  furnished: 'vybavenost',
  garage: 'garáž',
  elevator: 'výtah',
  other_material: 'jiný konstrukční materiál',
};

/* Structural common denominator of types.MfReferenceRent (listing column)
 * and types.ReferenceRent (run column). */
export interface MfReferenceLike {
  territory: { name: string; kraj: string | null };
  vk: number;
  is_novostavba: boolean;
  source_date?: string | null;
  base_per_m2: number;
  adjustments: ReadonlyArray<{ attribute: string; czk_per_m2: number }>;
  total_per_m2: number;
  area_m2: number;
  monthly_rent_czk: number;
}

export function MfReferenceCard({
  refRent,
  yieldPct = null,
}: {
  refRent: MfReferenceLike;
  yieldPct?: number | null;
}) {
  const perM2 = (n: number) => `${n.toLocaleString('cs-CZ')} Kč/m²`;
  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-3">
      <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        Odhad nájmu · cenová mapa MF
      </p>
      <div className="mt-1 flex items-baseline justify-between gap-3">
        <span className="text-lg font-medium tabular-nums">
          {fmtCzk(refRent.monthly_rent_czk)}
          <span className="ml-1 text-[0.7rem] text-[var(--color-ink-3)]">/měs</span>
        </span>
        {yieldPct != null && (
          <span className="text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
            hrubý výnos{' '}
            <span className="text-[var(--color-ink)] font-medium">
              {yieldPct.toLocaleString('cs-CZ', {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
              })}{' '}%
            </span>
          </span>
        )}
      </div>
      <dl className="mt-2 space-y-0.5 text-[0.72rem] tabular-nums">
        <div className="flex justify-between gap-3">
          <dt className="text-[var(--color-ink-3)]">
            Nájemné referenčního bytu
            {refRent.is_novostavba ? ' (novostavba)' : ''}
          </dt>
          <dd>{perM2(refRent.base_per_m2)}</dd>
        </div>
        {refRent.adjustments.map((a) => (
          <div key={a.attribute} className="flex justify-between gap-3">
            <dt className="text-[var(--color-ink-3)]">
              + {MF_ADJ_LABELS[a.attribute] ?? a.attribute}
            </dt>
            <dd>+{perM2(a.czk_per_m2)}</dd>
          </div>
        ))}
        <div className="flex justify-between gap-3 border-t border-[var(--color-rule)] pt-0.5">
          <dt>Celkem za m²</dt>
          <dd>{perM2(refRent.total_per_m2)}</dd>
        </div>
        <div className="flex justify-between gap-3">
          <dt className="text-[var(--color-ink-3)]">
            × plocha {refRent.area_m2.toLocaleString('cs-CZ')} m²
          </dt>
          <dd>{fmtCzk(refRent.monthly_rent_czk)}</dd>
        </div>
      </dl>
      <p className="mt-1.5 text-[0.58rem] text-[var(--color-ink-4)]">
        {refRent.territory.name}
        {refRent.territory.kraj ? `, ${refRent.territory.kraj}` : ''} · VK{refRent.vk} ·
        Ministerstvo financí
        {refRent.source_date ? ` (${refRent.source_date})` : ''}
      </p>
    </div>
  );
}
