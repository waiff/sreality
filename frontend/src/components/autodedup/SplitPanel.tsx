/* The one split dialog (MS18), on the property page: the letters as the server
 * reads them before anything is written (`GET /properties/{id}/split`, kept on
 * screen while a changed statement is re-read) — where each letter lands, what
 * is ruled, what a letter's join takes back — and the acting account's own items
 * ("Vaše položky"): each goes where the rule sends it unless the user picks
 * another letter, and may be copied to others; a fold is shown, never chosen.
 * Each pick re-reads the preview with it, so a fold or a copy the click would
 * not make says so and why. One click sends the letters, the choices that
 * differ from the preview, an optional reason and the preview's `plan`; a
 * property or an item of yours that changed since is refused (`stale`), and the
 * page's adverts and the preview are re-read. The answer is one receipt toast.
 * Nothing here speaks of other accounts' items. */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import SplitPlanLines from '@/components/autodedup/SplitPlanLines';
import {
  DETACH_REASON_MAX,
  getSplitPreview,
  splitProperty,
  splitRefusal,
  type SplitChoice,
  type SplitLetters,
  type SplitPreview,
  type SplitPreviewItem,
  type SplitSkip,
  type SplitStatement,
} from '@/lib/api';
import { propertyPath } from '@/lib/listingUrl';
import {
  choicesParam,
  lettersParam,
  mergedAdvertsKeys,
  pushSplitReceipt,
  refreshAfterSplit,
  splitItemWords,
} from '@/lib/mergedAdverts';

export const STALE_TEXT =
  'Nemovitost se mezitím změnila — načteno znovu, nic se nezapsalo. Zkontrolujte písmena a rozdělte znovu.';

/* The server's preview of these letters with the user's picks; the last one
 * stays on screen while a changed statement is read. */
function useSplitPreview(propertyId: number, letters: SplitLetters, choices: Record<string, SplitChoice>) {
  const param = lettersParam(letters);
  const picked = choicesParam(choices);
  return useQuery<SplitPreview, Error>({
    queryKey: mergedAdvertsKeys.plan(propertyId, param, picked),
    queryFn: () => (picked ? getSplitPreview(propertyId, param, picked) : getSplitPreview(propertyId, param)),
    placeholderData: keepPreviousData,
  });
}

/* Why a fold is not re-made or a copy not made. */
const SKIP: Record<SplitSkip, string> = {
  held: 'už ji tam máte',
  card: 'máte tam otevřený obchod v pipeline',
  gone: 'to, s čím při sloučení splynula, už na nemovitosti není',
};

/* Why an item goes where it goes, in the dialog's words. */
function hint(item: SplitPreviewItem): string {
  switch (item.why) {
    case 'ad':
      return 's inzerátem, u kterého vznikla';
    case 'came_from':
      return `přišla z nemovitosti #${item.from_property_id}`;
    case 'fold':
      return item.skipped
        ? `neobnoví se — ${SKIP[item.skipped]}`
        : `obnoví se v ${item.letter} (při sloučení splynula)`;
    default:
      return 'zůstává';
  }
}

const button =
  'rounded-[var(--radius-sm)] border px-2 py-0.5 text-[0.72rem] transition-colors';

export default function SplitPanel({
  propertyId,
  letters,
  canonicalListingId,
  portalOf,
  priceOf,
  onDone,
  onCancel,
}: {
  propertyId: number;
  /* Every ad of the property with its letter. */
  letters: SplitLetters;
  canonicalListingId: number | null;
  portalOf: (listingId: number) => string;
  priceOf: (listingId: number) => string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const param = lettersParam(letters);
  /* The picks hold for the statement they were made over; a pick back to the
   * preview's own letter with no copy is no pick. */
  const [picks, setPicks] = useState<{ param: string; choices: Record<string, SplitChoice> }>({
    param,
    choices: {},
  });
  const choices = picks.param === param ? picks.choices : {};
  const previewQ = useSplitPreview(propertyId, letters, choices);
  const preview = previewQ.data;
  const [reason, setReason] = useState('');
  const [failure, setFailure] = useState<string | null>(null);

  const split = useMutation({
    mutationFn: (statement: SplitStatement) => splitProperty(propertyId, statement),
    onSuccess: (res) => {
      setFailure(null);
      pushSplitReceipt(res, (id) => navigate(propertyPath(id)));
      refreshAfterSplit(qc);
      onDone();
    },
    onError: (e) => {
      const refusal = splitRefusal(e);
      setFailure(refusal?.code === 'stale' ? STALE_TEXT : `Chyba: ${refusal?.message ?? e.message}`);
      if (refusal?.code === 'stale') refreshAfterSplit(qc);
    },
  });

  const inUse = preview?.letters.map((l) => l.letter) ?? [];
  const current = previewQ.isSuccess && !previewQ.isPlaceholderData ? preview : undefined;
  const refused = current?.letters.some((l) => l.refused) ?? false;
  const choose = (item: SplitPreviewItem, next: SplitChoice) => {
    setFailure(null);
    const rest = Object.fromEntries(Object.entries(choices).filter(([key]) => key !== item.item));
    const same = next.to === item.letter && next.copies.length === 0;
    setPicks({ param, choices: same ? rest : { ...rest, [item.item]: next } });
  };
  const send = () => {
    if (split.isPending || !current || refused) return;
    setFailure(null);
    const why = reason.trim();
    const changed = Object.entries(choices).filter(([item, c]) => {
      const was = current.curation.find((x) => x.item === item);
      return was && was.why !== 'fold' && (c.to !== was.letter || c.copies.length > 0);
    });
    split.mutate({
      letters,
      ...(changed.length > 0 ? { choices: Object.fromEntries(changed) } : {}),
      ...(why ? { reason: why } : {}),
      expect: current.plan,
    });
  };

  const staying = preview?.letters.find((l) => l.lands === 'kept');
  const primary = preview?.letters.find(
    (l) => canonicalListingId != null && l.listing_ids.includes(canonicalListingId),
  );
  const stale = splitRefusal(previewQ.error)?.code === 'stale';
  /* The adverts changed since the page read them: read them again, so the
   * letters are set over the property as it is now. */
  useEffect(() => {
    if (stale) refreshAfterSplit(qc);
  }, [stale, qc]);

  return (
    <div
      role="group"
      aria-label="Rozdělení nemovitosti"
      className="mt-3 space-y-2 rounded-[var(--radius-sm)] border border-dashed border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)] px-3 py-2"
    >
      {previewQ.isPending && (
        <p className="text-[0.72rem] text-[var(--color-ink-3)]">Načítám náhled rozdělení…</p>
      )}
      {previewQ.isError && !preview && (
        <p role="alert" className="text-[0.72rem] text-[var(--color-brick)]">
          {stale ? STALE_TEXT : `Náhled rozdělení se nepodařilo načíst: ${previewQ.error.message}`}
          <button
            type="button"
            onClick={() => (stale ? refreshAfterSplit(qc) : void previewQ.refetch())}
            className="ml-2 font-medium underline underline-offset-2 hover:no-underline"
          >
            {stale ? 'Načíst znovu' : 'Zkusit znovu'}
          </button>
        </p>
      )}
      {preview && (
        <div className={previewQ.isPlaceholderData ? 'opacity-60' : ''} aria-busy={previewQ.isFetching}>
          <SplitPlanLines preview={preview} portalOf={portalOf} priceOf={priceOf} />
        </div>
      )}
      {primary && staying && primary !== staying && (
        <p className="text-[0.72rem] leading-snug text-[var(--color-ink)]">
          Hlavní inzerát odejde s písmenem{' '}
          <span className="font-mono font-medium">{primary.letter}</span>: záhlaví nemovitosti{' '}
          <span className="font-mono tabular-nums">#{propertyId}</span> pak převezme inzerát písmene{' '}
          <span className="font-mono font-medium">{staying.letter}</span>.
        </p>
      )}
      {preview && preview.curation.length > 0 && (
        <fieldset className="space-y-1">
          <legend className="text-[0.62rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            Vaše položky
          </legend>
          <ul className="space-y-1 text-[0.72rem] leading-snug text-[var(--color-ink-2)]">
            {preview.curation.map((item) => (
              <ItemLine
                key={item.item}
                item={item}
                letters={inUse}
                choice={choices[item.item]}
                disabled={split.isPending}
                onChoose={(next) => choose(item, next)}
              />
            ))}
          </ul>
        </fieldset>
      )}
      <textarea
        aria-label="Důvod rozdělení (nepovinné)"
        placeholder="Důvod (nepovinné)"
        maxLength={DETACH_REASON_MAX}
        rows={2}
        value={reason}
        disabled={split.isPending}
        onChange={(e) => setReason(e.target.value)}
        className="block w-full max-w-[32rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.75rem] text-[var(--color-ink)]"
      />
      {failure && (
        <p role="alert" className="text-[0.72rem] leading-snug text-[var(--color-brick)]">
          {failure}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          /* Not disabled while in flight: disabling the button just clicked drops
           * focus onto <body>; a second click sends nothing. */
          aria-busy={split.isPending}
          aria-disabled={!current || refused}
          onClick={send}
          className={`${button} border-[var(--color-brick)] text-[var(--color-brick)] hover:bg-[var(--color-brick)]/10 ${
            split.isPending || !current || refused ? 'opacity-60' : ''
          }`}
        >
          {split.isPending ? 'Probíhá…' : 'Rozdělit nemovitost'}
        </button>
        <button
          type="button"
          disabled={split.isPending}
          onClick={onCancel}
          className={`${button} border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:bg-[var(--color-rule-soft)] disabled:opacity-50`}
        >
          Zrušit
        </button>
      </div>
    </div>
  );
}

/* One of the acting account's items: its letter (the preview's unless picked)
 * and a copy per other letter; a fold is read-only. */
function ItemLine({
  item,
  letters,
  choice,
  disabled,
  onChoose,
}: {
  item: SplitPreviewItem;
  letters: string[];
  choice: SplitChoice | undefined;
  disabled: boolean;
  onChoose: (next: SplitChoice) => void;
}) {
  const words = splitItemWords(item.kind, item.label);
  if (item.why === 'fold') {
    return (
      <li>
        {words} — <span className="text-[var(--color-ink-3)]">{hint(item)}</span>
      </li>
    );
  }
  const to = choice?.to ?? item.letter;
  const copies = choice?.copies ?? [];
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
      <span>
        {words} — <span className="text-[var(--color-ink-3)]">{hint(item)}</span>
      </span>
      <label className="inline-flex items-center gap-1 text-[0.68rem] text-[var(--color-ink-3)]">
        Písmeno
        <select
          aria-label={`Písmeno pro ${words}`}
          value={to}
          disabled={disabled}
          onChange={(e) =>
            onChoose({ to: e.target.value, copies: copies.filter((c) => c !== e.target.value) })
          }
          className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-1 py-0 text-[0.68rem] text-[var(--color-ink)]"
        >
          {letters.map((letter) => (
            <option key={letter} value={letter}>
              {letter}
            </option>
          ))}
        </select>
      </label>
      {letters
        .filter((letter) => letter !== to)
        .map((letter) => {
          const skipped = copies.includes(letter)
            ? item.copies?.find((c) => c.letter === letter)?.skipped
            : null;
          return (
            <label key={letter} className="inline-flex items-center gap-1 text-[0.68rem]">
              <input
                type="checkbox"
                checked={copies.includes(letter)}
                disabled={disabled}
                onChange={() =>
                  onChoose({
                    to,
                    copies: copies.includes(letter)
                      ? copies.filter((c) => c !== letter)
                      : [...copies, letter].sort(),
                  })
                }
              />
              + kopie do {letter}
              {skipped && (
                <span className="text-[var(--color-ink-3)]"> (nevytvoří se — {SKIP[skipped]})</span>
              )}
            </label>
          );
        })}
    </li>
  );
}
