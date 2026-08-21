/* THE save-to-collection control (rule #18) — one component on every surface a
 * property appears on: the Browse cards and the listing-detail header action
 * bar, next to the pipeline affordance.
 *
 * Adjacent to the pipeline funnel but deliberately orthogonal to it (rule #22
 * keeps the funnel the sole pipeline affordance): a collection is an m2m
 * grouping and monitoring opts one into change alerts, so this is a layers
 * glyph opening a MULTI-select menu of collections — monitored ones first,
 * marked with a bell — not a single toggle.
 *
 * The menu is an `AnchoredPopover` for the same reason the stage menu is: on a
 * Browse card this control sits inside an `overflow-hidden` wrapper AND inside
 * the card's <Link>, so an absolutely-positioned panel is clipped to the photo
 * and every click inside it navigates away.
 */

import { useCallback, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import AnchoredPopover from '@/components/AnchoredPopover';
import { listCollections } from '@/lib/api';
import {
  curationKeys,
  fetchPropertyCollectionIds,
  fetchPropertyCollectionMemberSet,
} from '@/lib/queries';
import { useCollectionMembership } from '@/lib/useCollectionMembership';
import { useMenuKeyboard } from '@/lib/useMenuKeyboard';

/* Where membership is read from — an explicit choice, not derived from the
 * chrome. 'shared' uses the ONE all-properties map React Query dedupes across
 * every control on a list screen (a grid of 60 cards issues one read, not 60);
 * 'single' reads just this property, which is what a record page wants — it
 * would otherwise download every membership in the account to answer a question
 * about one property. */
export type MembershipSource = 'shared' | 'single';

export interface CollectionSaveButtonProps {
  property_id: number;
  /* Chrome only. 'overlay' floats on a card photo (translucent, icon-only);
   * 'inline' sits on a solid row background (icon-only); 'header' is the
   * record-page action bar — icon + label, sized to match PipelineToggle. */
  variant?: 'overlay' | 'inline' | 'header';
  source?: MembershipSource;
  /* Stop the trigger's click from following an enclosing Link / row navigation.
   * (Clicks inside the menu are swallowed by AnchoredPopover itself.) */
  stopPropagation?: boolean;
}

export default function CollectionSaveButton({
  property_id,
  variant = 'overlay',
  source = 'shared',
  stopPropagation = true,
}: CollectionSaveButtonProps) {
  const btnRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  /* Stable so the popover's positioning effect doesn't re-subscribe each render. */
  const close = useCallback(() => setOpen(false), []);

  const memberIds = useMemberIds(property_id, source);
  const inAny = memberIds.size > 0;

  const label = inAny
    ? memberIds.size === 1
      ? 'V kolekci'
      : 'V kolekcích'
    : 'Uložit do kolekce';

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={(e) => {
          if (stopPropagation) {
            e.preventDefault();
            e.stopPropagation();
          }
          setOpen((v) => !v);
        }}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        title={label}
        className={triggerClass(variant, inAny)}
      >
        <CollectionGlyph filled={inAny} />
        {variant === 'header' && (
          <>
            <span>{label}</span>
            {memberIds.size > 1 && (
              <span className="font-mono text-[0.72rem] tabular-nums opacity-70">
                {memberIds.size}
              </span>
            )}
          </>
        )}
      </button>
      {open && (
        <CollectionPickerMenu
          property_id={property_id}
          memberIds={memberIds}
          anchorRef={btnRef}
          onClose={close}
        />
      )}
    </>
  );
}

function CollectionPickerMenu({
  property_id,
  memberIds,
  anchorRef,
  onClose,
}: {
  property_id: number;
  memberIds: ReadonlySet<number>;
  anchorRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  const listRef = useRef<HTMLDivElement>(null);

  /* The collection index is only needed once the menu opens, so a screen full
   * of these controls costs no API call until one is used. */
  const collectionsQ = useQuery({
    queryKey: curationKeys.collections,
    queryFn: listCollections,
    staleTime: 30_000,
  });

  const { toggle } = useCollectionMembership(property_id);

  // Monitored collections first, then alphabetical.
  const sorted = useMemo(
    () =>
      [...(collectionsQ.data?.data ?? [])].sort(
        (a, b) =>
          (b.monitoring_enabled ? 1 : 0) - (a.monitoring_enabled ? 1 : 0) ||
          a.name.localeCompare(b.name),
      ),
    [collectionsQ.data],
  );

  const onKeyDown = useMenuKeyboard(listRef, { deps: [sorted.length] });

  /* The menu deliberately stays open after a tick: it is a multi-select, and
   * the optimistic patch (collectionCache) repaints the checkbox immediately,
   * so filing into several collections is one uninterrupted pass. */
  return (
    <AnchoredPopover
      anchorRef={anchorRef}
      onClose={onClose}
      ariaLabel="Kolekce"
      className="w-[15.5rem] py-1"
    >
      <div ref={listRef} role="menu" aria-label="Kolekce" onKeyDown={onKeyDown}>
        <p className="px-2.5 pb-1 pt-1 text-[0.62rem] uppercase tracking-[0.18em] text-[var(--color-ink-4)]">
          Uložit do kolekce
        </p>

        {collectionsQ.isLoading ? (
          <p className="px-2.5 py-1.5 text-[0.75rem] text-[var(--color-ink-4)]">Načítám…</p>
        ) : sorted.length === 0 ? (
          <Link
            to="/collections"
            role="menuitem"
            tabIndex={-1}
            onClick={onClose}
            className="block px-2.5 py-1.5 text-[0.78rem] text-[var(--color-copper)] hover:underline"
          >
            Založit kolekci →
          </Link>
        ) : (
          <ul className="max-h-64 overflow-y-auto">
            {sorted.map((c) => {
              const member = memberIds.has(c.id);
              return (
                <li key={c.id}>
                  <button
                    type="button"
                    role="menuitemcheckbox"
                    aria-checked={member}
                    tabIndex={-1}
                    onClick={() => toggle(c.id, member)}
                    className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[0.8rem] text-[var(--color-ink-2)] transition-colors hover:bg-[var(--color-paper-2)]"
                  >
                    <span
                      aria-hidden
                      className={[
                        'inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-[3px] border text-[0.6rem] leading-none',
                        member
                          ? 'border-[var(--color-copper)] bg-[var(--color-copper)] text-white'
                          : 'border-[var(--color-rule-strong)] text-transparent',
                      ].join(' ')}
                    >
                      ✓
                    </span>
                    <span className="min-w-0 flex-1 truncate text-[var(--color-ink)]">
                      {c.name}
                    </span>
                    {c.monitoring_enabled && (
                      <span
                        title="Sledovaná — upozorní na změny"
                        className="shrink-0 text-[var(--color-copper)]"
                      >
                        <BellGlyph />
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        {sorted.length > 0 && (
          <div className="mt-1 border-t border-[var(--color-rule-soft)] pt-1">
            <Link
              to="/collections"
              role="menuitem"
              tabIndex={-1}
              onClick={onClose}
              className="block px-2.5 py-1.5 text-[0.75rem] text-[var(--color-ink-3)] transition-colors hover:bg-[var(--color-paper-2)] hover:text-[var(--color-copper)]"
            >
              Spravovat kolekce →
            </Link>
          </div>
        )}
      </div>
    </AnchoredPopover>
  );
}

/* Both reads are declared unconditionally (hooks rules) and gated by `enabled`,
 * so only the one this surface asked for is ever issued. */
function useMemberIds(
  property_id: number,
  source: MembershipSource,
): ReadonlySet<number> {
  const sharedQ = useQuery({
    queryKey: curationKeys.propertyCollectionMembers,
    queryFn: fetchPropertyCollectionMemberSet,
    staleTime: 30_000,
    enabled: source === 'shared',
  });
  const singleQ = useQuery({
    queryKey: curationKeys.propertyCollections(property_id),
    queryFn: () => fetchPropertyCollectionIds(property_id),
    staleTime: 30_000,
    enabled: source === 'single',
  });

  const ids = source === 'shared' ? sharedQ.data?.get(property_id) : singleQ.data;
  return useMemo(() => new Set(ids ?? []), [ids]);
}

function triggerClass(
  variant: 'overlay' | 'inline' | 'header',
  inAny: boolean,
): string {
  if (variant === 'header') {
    /* Sized to sit beside PipelineToggle. Deliberately NOT copper-outlined when
     * empty: the pipeline is the deal verb that owns the accent, filing into a
     * collection is secondary until it has happened. */
    return [
      'inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border px-3 py-1.5 text-[0.8rem] transition-colors',
      inAny
        ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
        : 'border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)]',
    ].join(' ');
  }
  return [
    'flex h-6 w-6 items-center justify-center rounded-[var(--radius-xs)] border transition-colors',
    variant === 'overlay' ? 'backdrop-blur' : '',
    inAny
      ? variant === 'overlay'
        ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)]/90 text-[var(--color-copper)]'
        : 'border-[var(--color-copper)] bg-transparent text-[var(--color-copper)]'
      : variant === 'overlay'
        ? 'border-[var(--color-rule)] bg-[var(--color-paper-3)]/85 text-[var(--color-ink-3)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)]'
        : 'border-transparent text-[var(--color-ink-4)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)]',
  ].join(' ');
}

function CollectionGlyph({ filled }: { filled: boolean }) {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 16 16"
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M4 2.5 H12 V13.5 L8 10.75 L4 13.5 Z" strokeLinecap="round" />
    </svg>
  );
}

function BellGlyph() {
  return (
    <svg
      width="9"
      height="9"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M8 1.5a3.5 3.5 0 0 0-3.5 3.5c0 3-1.5 4-1.5 4h10s-1.5-1-1.5-4A3.5 3.5 0 0 0 8 1.5ZM6.5 12.5a1.5 1.5 0 0 0 3 0" />
    </svg>
  );
}
