/* One draggable card on the deal-pipeline board.
 *
 * Extracted out of Pipeline.tsx when the card grew two data signals (the price
 * delta and the civic index strip): the page was 863 lines with every component
 * private to it, so nothing here could be tested or reused. `CardFace` is
 * deliberately separate from `BoardCard` — the drag overlay renders the face
 * without the drag/remove chrome, and the two must never drift.
 *
 * Reading order on the card, densest-first, because the operator is triaging:
 *   price + how it moved  →  place  →  disposition/area + yield  →  broker
 *   →  the municipality's civic indexes
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import { CSS } from '@dnd-kit/utilities';
import { useDraggable } from '@dnd-kit/core';
import { fmtArea, fmtCzk } from '@/lib/format';
import { listingKindLabel } from '@/lib/enums';
import { listingRowPath } from '@/lib/listingUrl';
import { placePrimary } from '@/lib/placeLabel';
import { TrashIcon } from '@/components/icons';
import PriceDelta from '@/components/PriceDelta';
import CityIndexStrip from '@/components/CityIndexStrip';
import type { CityQualityByObec } from '@/lib/useCityQuality';
import { useCardHydration } from '@/lib/hydration';
import type { PipelineBoardCard, PipelineCardBroker } from '@/lib/types';

export const CARD_PREFIX = 'card:';
export const STAGE_PREFIX = 'stage:';

function CardThumb({ url, inactive }: { url: string | null; inactive: boolean }) {
  const cls =
    'h-12 w-12 shrink-0 rounded-[var(--radius-sm)] border border-[var(--color-rule)]';
  if (!url) return <div className={`${cls} bg-[var(--color-inset)]`} aria-hidden />;
  // Same gentle desaturation Browse's card photo gets when is_active=false —
  // the only signal left in the photo lane once the surface carries status.
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      className={`${cls} object-cover ${inactive ? 'saturate-[0.4] brightness-[0.97]' : ''}`}
    />
  );
}

/* Native-title hover box for a card's broker — name, firm, and contact on one
 * line (the codebase's tooltip convention). The name itself links to the broker
 * page for the full record.
 *
 * A non-admin session gets has_email/has_phone instead of the values, so the
 * tooltip says the contact exists but is admin-only rather than omitting it and
 * implying the broker has none. Exported for its test. */
export function brokerHoverTitle(b: PipelineCardBroker): string {
  const parts = [b.display_name, b.firm_label, b.phone, b.email].filter(Boolean);
  const hidden = (b.has_phone && !b.phone) || (b.has_email && !b.email);
  if (hidden) parts.push('kontakt jen pro adminy');
  return parts.join(' · ') || 'Zobrazit makléře';
}

/* The card's visible content — reused by the in-column card and the drag ghost. */
export function CardFace({
  card,
  cityQuality,
}: {
  card: PipelineBoardCard;
  cityQuality?: CityQualityByObec;
}) {
  /* Decorations come from context, not from `card` — see lib/hydration. The
   * board's structural read no longer carries them, and CardFace renders both
   * in-column and inside the drag overlay, so context is what keeps those two
   * mount points from drifting apart. */
  const { coverFor, brokerFor, brokersPending } = useCardHydration();
  const cover = coverFor(card.listing_id);
  const broker = brokerFor(card.listing_id);

  const inactive = !card.is_active;
  const priceColor = inactive ? 'text-[var(--color-ink-2)]' : 'text-[var(--color-ink)]';
  /* placePrimary names the TOWN, not the okres — the shared resolver every
     other surface uses. The board used to hand-roll `[street, district]`, which
     rendered "okres Beroun" for a village and made the Město sort unverifiable
     against what the card showed. Street is prefixed by the resolver itself. */
  const place = placePrimary({
    locality: card.locality,
    district: card.district,
    obec: card.obec,
    okres: card.okres,
    street: card.street,
  });
  const dims = [
    listingKindLabel(card),
    card.area_m2 != null ? fmtArea(card.area_m2) : null,
  ]
    .filter(Boolean)
    .join(' · ');
  const quality = card.obec_id != null ? cityQuality?.get(card.obec_id) : undefined;

  return (
    <div>
      <div className="flex gap-2.5">
        <CardThumb url={cover} inactive={inactive} />
        <div className="min-w-0 flex-1">
          {/* Price and its movement are one typographic unit — the delta sits on
              the price's own baseline rather than reading as a separate badge. */}
          <div className="flex items-baseline gap-1.5">
            {/* listingRowPath is canonical-first (source + source_id_native from
                properties_public), so the card links straight to the clean
                /listing/{source}/{native} URL; it falls back to the legacy/property
                route only for a representative with no natural key. */}
            <Link
              to={listingRowPath(card)}
              state={{ listingId: card.listing_id ?? undefined }}
              title={inactive ? 'Neaktivní inzerát' : undefined}
              className={`font-mono tabular-nums text-sm hover:text-[var(--color-copper)] hover:underline underline-offset-2 ${priceColor}`}
            >
              {fmtCzk(card.price_czk)}
            </Link>
            <PriceDelta
              pct={card.total_price_change_pct}
              changes={card.price_change_count}
              muted={inactive}
            />
          </div>
          {place && (
            <p className="mt-0.5 truncate text-xs text-[var(--color-ink-2)]">{place}</p>
          )}
          <div className="mt-0.5 flex items-center justify-between gap-2">
            <span className="truncate font-mono tabular-nums text-xs text-[var(--color-ink-4)]">
              {dims || '—'}
            </span>
            {card.mf_gross_yield_pct != null && (
              <span
                className="shrink-0 font-mono tabular-nums text-[0.68rem] text-[var(--color-ink-3)]"
                title="Hrubý výnos dle cenové mapy nájemného MF"
              >
                MF{' '}
                {card.mf_gross_yield_pct.toLocaleString('cs-CZ', {
                  minimumFractionDigits: 1,
                  maximumFractionDigits: 1,
                })}{' '}
                %
              </span>
            )}
          </div>
          {/* The broker line streams in behind the card. Its height is
              reserved while the read is in flight so the column does not
              reflow when it lands; once resolved, a card with no attributed
              broker collapses the space rather than holding an empty row. */}
          {broker ? (
            <p className="mt-0.5 truncate text-[0.7rem] text-[var(--color-ink-3)]">
              <Link
                to={`/brokers/${broker.broker_id}`}
                title={brokerHoverTitle(broker)}
                className="hover:text-[var(--color-copper)] hover:underline underline-offset-2"
              >
                {broker.display_name ?? 'Makléř'}
              </Link>
              {broker.firm_label && (
                <span className="text-[var(--color-ink-4)]"> · {broker.firm_label}</span>
              )}
            </p>
          ) : brokersPending ? (
            <p className="mt-0.5 h-[0.95rem]" aria-hidden />
          ) : null}
        </div>
      </div>
      {/* Full card width, below the photo column — the strip is about the
          MUNICIPALITY, not this listing, so it reads as a footer rather than as
          another attribute of the property. Renders nothing when the property
          is not in a curated city (about half of them). */}
      <CityIndexStrip quality={quality} muted={inactive} />
    </div>
  );
}

export default function BoardCard({
  card,
  cityQuality,
  onRemove,
}: {
  card: PipelineBoardCard;
  cityQuality?: CityQualityByObec;
  onRemove: (propertyId: number) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `${CARD_PREFIX}${card.property_id}`,
  });
  const [confirming, setConfirming] = useState(false);
  const style = {
    transform: CSS.Translate.toString(transform),
    opacity: isDragging ? 0.4 : undefined,
  };
  // Inactive cards recede via surface tint, matching Browse's "filed away"
  // treatment for delisted listings (rule: app-wide unification of the
  // is_active signal, not a bespoke Pipeline-only style).
  const surface = !card.is_active
    ? 'border-[var(--color-rule-soft)] bg-[var(--color-inset)]'
    : 'border-[var(--color-rule)] bg-[var(--color-paper-2)]';

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`rounded-[var(--radius-md)] border p-2.5 ${surface}`}
    >
      <div className="flex items-start gap-1.5">
        <button
          type="button"
          {...attributes}
          {...listeners}
          aria-label="Přetáhnout kartu do jiné fáze"
          className="shrink-0 cursor-grab touch-none pt-0.5 leading-none text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] active:cursor-grabbing"
        >
          ⠿
        </button>
        <div className="min-w-0 flex-1">
          <CardFace card={card} cityQuality={cityQuality} />
        </div>
        <button
          type="button"
          onClick={() => setConfirming((v) => !v)}
          aria-label="Odebrat z pipeline"
          aria-expanded={confirming}
          title="Odebrat z pipeline"
          className="shrink-0 rounded-[var(--radius-xs)] pt-0.5 text-[var(--color-ink-4)] hover:text-[var(--color-brick)] focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-rule-strong)]"
        >
          <TrashIcon className="h-3.5 w-3.5" />
        </button>
      </div>
      {/* Inline two-step confirm (the app's destructive-action pattern) — removing
          a property from the pipeline drops the card entirely. Stage moves are
          drag-only now; the select fallback was removed. */}
      {confirming && (
        <div className="mt-2 flex items-center gap-2 border-t border-[var(--color-rule-soft)] pt-2 text-[0.72rem]">
          <span className="mr-auto text-[var(--color-ink-3)]">Odebrat z pipeline?</span>
          <button
            type="button"
            onClick={() => {
              setConfirming(false);
              onRemove(card.property_id);
            }}
            className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-2 py-0.5 text-[var(--color-brick)] hover:bg-[var(--color-brick)]/10"
          >
            Odebrat
          </button>
          <button
            type="button"
            onClick={() => setConfirming(false)}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:bg-[var(--color-rule-soft)]"
          >
            Zrušit
          </button>
        </div>
      )}
    </div>
  );
}
