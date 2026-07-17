import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import FilterChip from '@/components/FilterChip';
import InfiniteSentinel from '@/components/InfiniteSentinel';
import ImageTagBadge from '@/components/ImageTagBadge';
import ImageLightbox from '@/components/ImageLightbox';
import NoteFlagControl from '@/components/NoteFlagControl';
import LabelCombobox from '@/components/LabelCombobox';
import DedupBreakdown from '@/components/DedupBreakdown';
import { useInfiniteList } from '@/lib/useInfiniteList';
import {
  fetchPhashPairNotesForImageIds,
  fetchTrainingExamplesForImageIds,
  fetchDistinctTrainingLabels,
} from '@/lib/queries';
import {
  getPhashAudit,
  setPhashNote,
  deletePhashNote,
  setTrainingExample,
  deleteTrainingExample,
  type PhashAuditRow,
  type PhashAuditImageRef,
  type TrainingExample,
} from '@/lib/api';
import { CATEGORY_MAIN_TABS } from '@/lib/categoryMainTabs';
import { IMAGE_TAG_LABELS, imageTagLabel } from '@/lib/imageTags';
import { fmtRelative, fmtCount } from '@/lib/format';
import { imageSrc } from '@/lib/imageUrl';
import type { ImagePublic } from '@/lib/types';

/* /phash-audit — evidence for whether the pHash merge bar (Hamming <= 6, the hardcoded
 * PHASH_IDENTICAL_MAX) could safely widen. A direct range browse: pick a Hamming window,
 * see the matching-tag photo pairs from decisions the engine already made that fall in
 * it — no recompute, no engine change, read-only. */

const OUTCOMES = [
  { id: '', label: 'Vše' },
  { id: 'merged', label: 'Sloučeno' },
  { id: 'dismissed', label: 'Zamítnuto' },
];

const TAG_OPTIONS = Object.keys(IMAGE_TAG_LABELS).filter(
  (k) => !['situation_plan', 'cadastral_map', 'aerial_plot', 'location_map',
           'energy_certificate', 'document_text'].includes(k),
);

const PAGE_SIZE = 50;
const DEFAULT_MIN = 7; // just above the current merge bar (Hamming <= 6)
const DEFAULT_MAX = 15;

interface PhashPage {
  rows: PhashAuditRow[];
  nextCursor: number | undefined;
  scanned_pairs: number;
  scan_cap: number;
}

function chunk<T>(arr: readonly T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

export default function PhashAudit() {
  const [outcome, setOutcome] = useState('');
  const [categoryMain, setCategoryMain] = useState('');
  const [roomType, setRoomType] = useState('');
  const [minText, setMinText] = useState(String(DEFAULT_MIN));
  const [maxText, setMaxText] = useState(String(DEFAULT_MAX));
  const [hammingMin, setHammingMin] = useState(DEFAULT_MIN);
  const [hammingMax, setHammingMax] = useState(DEFAULT_MAX);

  const commitRange = () => {
    const lo = parseInt(minText, 10);
    const hi = parseInt(maxText, 10);
    if (Number.isFinite(lo)) setHammingMin(lo);
    if (Number.isFinite(hi)) setHammingMax(hi);
  };

  const list = useInfiniteList<PhashAuditRow, PhashPage>({
    queryKey: ['phash-audit', outcome, categoryMain, roomType, hammingMin, hammingMax],
    queryFn: async (cursor) => {
      const offset = (cursor as number | null) ?? 0;
      const resp = await getPhashAudit({
        hamming_min: hammingMin,
        hamming_max: hammingMax,
        category_main: categoryMain || undefined,
        outcome: outcome || undefined,
        room_type: roomType || undefined,
        limit: PAGE_SIZE,
        offset,
      });
      return {
        rows: resp.data,
        nextCursor: resp.data.length === PAGE_SIZE ? offset + PAGE_SIZE : undefined,
        scanned_pairs: resp.scanned_pairs,
        scan_cap: resp.scan_cap,
      };
    },
    pageSize: PAGE_SIZE,
    getRowId: (r) => `${r.audit_id}:${r.left_image.image_id}:${r.right_image.image_id}`,
  });

  const scope = list.firstPage;
  const pages = useMemo(() => chunk(list.rows, PAGE_SIZE), [list.rows]);

  // The label combobox's suggestion list: the fixed CLIP taxonomy (Czech labels, same
  // set as the Tag filter above) + whatever the operator has already typed into the
  // training set — fetched once for the whole page, not per page-group.
  const trainingLabelsQ = useQuery({
    queryKey: ['phash-audit', 'training-labels'],
    queryFn: fetchDistinctTrainingLabels,
    staleTime: 30_000,
  });
  const labelOptions = useMemo(() => {
    const taxonomy = TAG_OPTIONS.map((t) => imageTagLabel(t) ?? t);
    return [...new Set([...taxonomy, ...(trainingLabelsQ.data ?? [])])].sort();
  }, [trainingLabelsQ.data]);

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">pHash Audit</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] max-w-3xl">
          Browse matching-photo pairs by Hamming distance — evidence for whether the
          merge threshold could widen. Read-only: nothing here changes the live engine.
        </p>
      </header>

      <Explainer />

      <div className="mt-6 flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)] mr-1">
            Výsledek
          </span>
          {OUTCOMES.map((o) => (
            <FilterChip key={o.id} on={outcome === o.id} label={o.label} onClick={() => setOutcome(o.id)} />
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)] mr-1">
            Typ
          </span>
          {CATEGORY_MAIN_TABS.map((t) => (
            <FilterChip key={t.id} on={categoryMain === t.id} label={t.label} onClick={() => setCategoryMain(t.id)} />
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)] mr-1">
            Tag
          </span>
          <FilterChip on={roomType === ''} label="Vše" onClick={() => setRoomType('')} />
          {TAG_OPTIONS.map((tag) => (
            <FilterChip
              key={tag}
              on={roomType === tag}
              label={imageTagLabel(tag) ?? tag}
              onClick={() => setRoomType(tag)}
            />
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[0.78rem] text-[var(--color-ink-3)]">
          <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)]">
            Hammingova vzdálenost
          </span>
          <input
            value={minText}
            onChange={(e) => setMinText(e.target.value)}
            onBlur={commitRange}
            onKeyDown={(e) => e.key === 'Enter' && commitRange()}
            inputMode="numeric"
            aria-label="Min Hamming"
            className="w-14 px-1.5 py-0.5 font-mono tabular-nums rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)]"
          />
          <span>–</span>
          <input
            value={maxText}
            onChange={(e) => setMaxText(e.target.value)}
            onBlur={commitRange}
            onKeyDown={(e) => e.key === 'Enter' && commitRange()}
            inputMode="numeric"
            aria-label="Max Hamming"
            className="w-14 px-1.5 py-0.5 font-mono tabular-nums rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)]"
          />
          <span className="text-[0.7rem] text-[var(--color-ink-4)]">
            (aktuální práh sloučení: ≤ 6)
          </span>
        </div>
      </div>

      {scope && (
        <p className="mt-3 text-[0.72rem] text-[var(--color-ink-4)]">
          {fmtCount(scope.scanned_pairs)} rozhodnutí odpovídá filtru · prohledáno
          nejnovějších {fmtCount(scope.scan_cap)}
          {scope.scanned_pairs > scope.scan_cap ? ' (staré páry nejsou zahrnuty)' : ''}
        </p>
      )}

      <div className="mt-4 flex flex-col gap-3">
        {list.isLoading ? (
          <p className="text-sm text-[var(--color-ink-3)]">Načítám…</p>
        ) : pages.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-3)]">
            Žádné páry fotek v tomto rozsahu Hammingovy vzdálenosti.
          </p>
        ) : (
          pages.map((page, i) => (
            <PhashPageGroup key={i} rows={page} labelOptions={labelOptions} />
          ))
        )}
      </div>

      <InfiniteSentinel
        onReach={list.fetchNextPage}
        hasNextPage={list.hasNextPage}
        isFetchingNextPage={list.isFetchingNextPage}
        loadedCount={list.loadedCount}
        total={null}
      />
    </div>
  );
}

function Explainer() {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-4 border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-2)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full px-4 py-2.5 flex items-center justify-between gap-2 text-left text-sm"
      >
        <span>Algoritmus &amp; parametry</span>
        <span className="text-[var(--color-ink-4)]" aria-hidden>{open ? '▴' : '▾'}</span>
      </button>
      {open && (
        <div className="px-4 pb-4 text-[0.82rem] text-[var(--color-ink-2)] leading-relaxed space-y-2">
          <p>
            dHash (difference hash): each photo resizes to 9×8 grayscale, adjacent pixels
            compare to produce a 64-bit fingerprint. Two photos' distance is the Hamming
            distance (bits that differ) between their two 64-bit values — cheap,
            reused-photo detection, not a visual-similarity model.
          </p>
          <p>
            <strong className="text-[var(--color-ink)]">Hardcoded in code</strong> (not a
            Settings knob — changing these needs a deploy):{' '}
            <span className="font-mono">PHASH_IDENTICAL_MAX = 6</span> (a pair counts as
            near-identical at or below this distance) and{' '}
            <span className="font-mono">PHASH_MIN_IDENTICAL_PAIRS = 2</span> (how many
            near-identical pairs the engine needs to auto-merge, one for the two
            distinctive rooms kitchen/bathroom). This page is read-only evidence —
            it doesn't change either.
          </p>
        </div>
      )}
    </div>
  );
}

function PhashPageGroup({
  rows,
  labelOptions,
}: {
  rows: PhashAuditRow[];
  labelOptions: string[];
}) {
  const imageIds = useMemo(() => {
    const s = new Set<number>();
    for (const r of rows) {
      s.add(r.left_image.image_id);
      s.add(r.right_image.image_id);
    }
    return [...s];
  }, [rows]);

  const notesQ = useQuery({
    queryKey: ['phash-audit', 'notes', imageIds],
    queryFn: () => fetchPhashPairNotesForImageIds(imageIds),
    enabled: imageIds.length > 0,
  });
  const trainingQ = useQuery({
    queryKey: ['phash-audit', 'training', imageIds],
    queryFn: () => fetchTrainingExamplesForImageIds(imageIds),
    enabled: imageIds.length > 0,
  });

  return (
    <>
      {rows.map((r) => (
        <PhashRow
          key={`${r.audit_id}:${r.left_image.image_id}:${r.right_image.image_id}`}
          row={r}
          note={notesQ.data?.get(`${r.left_image.image_id}:${r.right_image.image_id}`)}
          training={trainingQ.data}
          labelOptions={labelOptions}
        />
      ))}
    </>
  );
}

function toImagePublic(sreality_id: number | null, ref: PhashAuditImageRef): ImagePublic {
  return {
    id: ref.image_id,
    sreality_id: sreality_id ?? 0,
    sequence: null,
    sreality_url: ref.sreality_url ?? '',
    storage_path: ref.storage_path,
    clip_fine_tag: ref.fine_tag,
    clip_logical_tag: ref.room_type,
    clip_confidence: ref.confidence,
    clip_render_score: ref.render_score,
    phash: null,
  };
}

function PhashRow({
  row,
  note,
  training,
  labelOptions,
}: {
  row: PhashAuditRow;
  note: { note: string | null } | undefined;
  training: Map<number, TrainingExample> | undefined;
  labelOptions: string[];
}) {
  const qc = useQueryClient();
  const [lightboxAt, setLightboxAt] = useState<number | null>(null);
  const images = useMemo(
    () => [
      toImagePublic(row.left_sreality_id, row.left_image),
      toImagePublic(row.right_sreality_id, row.right_image),
    ],
    [row],
  );

  const save = useMutation({
    mutationFn: (input: { note: string | null }) =>
      setPhashNote({
        image_id_a: row.left_image.image_id,
        image_id_b: row.right_image.image_id,
        note: input.note,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phash-audit', 'notes'] }),
  });
  const remove = useMutation({
    mutationFn: () =>
      deletePhashNote(row.left_image.image_id, row.right_image.image_id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phash-audit', 'notes'] }),
  });

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)] p-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-[0.78rem]">
        <span className="font-mono tabular-nums text-[var(--color-copper)]">
          ⟷ {row.hamming}
        </span>
        <span
          className={row.outcome === 'merged' ? 'text-[var(--color-copper)]' : 'text-[var(--color-brick)]'}
        >
          {row.outcome === 'merged' ? 'sloučeno' : 'zamítnuto'}
        </span>
        <span className="text-[var(--color-ink-4)] font-mono" title="Fáze, která o páru skutečně rozhodla">
          {row.stage}
        </span>
        <span className="text-[var(--color-ink-4)] uppercase tracking-[0.06em]">
          {row.category_main}
        </span>
        <span className="text-[var(--color-ink-4)]">{fmtRelative(row.run_at)}</span>
        {row.left_property_id != null && (
          <Link
            to={`/dedup?audit_property=${row.left_property_id}#history`}
            className="ml-auto text-[var(--color-ink-3)] hover:text-[var(--color-copper)] underline decoration-dotted underline-offset-2"
          >
            zobrazit v Decision history →
          </Link>
        )}
      </div>

      {/* The Hamming number above is THIS pair's live distance — not necessarily what
          decided the pair (a stage like 'visual' can dismiss/merge even when phash found
          nothing at all, e.g. phash_pairs=0). This breakdown is the actual decision. */}
      <DedupBreakdown rungs={row.audit_breakdown} />

      <div className="flex flex-wrap gap-3">
        {images.map((img, i) => (
          <TrainableImage
            key={img.id}
            image={img}
            srealityId={i === 0 ? row.left_sreality_id : row.right_sreality_id}
            example={training?.get(img.id)}
            labelOptions={labelOptions}
            onOpen={() => setLightboxAt(i)}
          />
        ))}
      </div>

      <NoteFlagControl
        flagged={!!note?.note}
        note={note?.note}
        flagLabel="Přidat poznámku"
        flaggedLabel="Poznámka"
        notePlaceholder="Vypadají tyto fotky jako stejná fotka?"
        busy={save.isPending || remove.isPending}
        onSave={(input) => save.mutate({ note: input.note })}
        onRemove={() => remove.mutate()}
      />

      {lightboxAt != null && (
        <ImageLightbox images={images} startIndex={lightboxAt} onClose={() => setLightboxAt(null)} />
      )}
    </div>
  );
}

/* One image + its "Train" data-collection control (the linear-probe training-set label
 * picker) — sized to match Browse's Large card image (23rem, aspect-[5/4]; the fluid
 * aspect-square 2-column grid this replaces ran noticeably larger than Browse at
 * typical widths). Defaults the combobox to the CLIP-assigned label (fine_tag, the
 * same value the badge already shows) so confirming a correct call is one click. */
function TrainableImage({
  image,
  srealityId,
  example,
  labelOptions,
  onOpen,
}: {
  image: ImagePublic;
  srealityId: number | null;
  example: TrainingExample | undefined;
  labelOptions: string[];
  onOpen: () => void;
}) {
  const qc = useQueryClient();
  const defaultLabel = imageTagLabel(image.clip_fine_tag) ?? image.clip_fine_tag ?? '';
  const [label, setLabel] = useState(example?.label ?? defaultLabel);

  // Re-sync when the saved example changes (e.g. after invalidation confirms a
  // write, or an example trained from a different session loads in).
  useEffect(() => {
    if (example?.label != null) setLabel(example.label);
  }, [example?.label]);

  const trained = !!example;

  const train = useMutation({
    mutationFn: () => setTrainingExample({ image_id: image.id, label }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phash-audit', 'training'] }),
  });
  const untrain = useMutation({
    mutationFn: () => deleteTrainingExample(image.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phash-audit', 'training'] }),
  });

  return (
    <div className="w-full max-w-[23rem] flex flex-col gap-1.5">
      <button
        type="button"
        onClick={onOpen}
        className="group relative block w-full aspect-[5/4] overflow-hidden rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)]"
      >
        <img src={imageSrc(image)} alt="" loading="lazy" className="w-full h-full object-cover" />
        <ImageTagBadge
          tag={image.clip_fine_tag}
          confidence={image.clip_confidence}
          className="absolute bottom-1 left-1 max-w-[calc(100%-0.5rem)] truncate"
        />
        <span className="absolute top-1 right-1 px-1 py-0.5 text-[0.6rem] font-mono tabular-nums rounded-[var(--radius-xs)] bg-[var(--color-paper-3)]/85 border border-[var(--color-rule)] text-[var(--color-ink-3)]">
          {srealityId ?? '—'}
        </span>
      </button>
      <div className="flex items-center gap-1.5">
        <div className="min-w-0 flex-1">
          <LabelCombobox value={label} onChange={setLabel} options={labelOptions} />
        </div>
        <button
          type="button"
          onClick={() => train.mutate()}
          disabled={train.isPending || label.trim().length === 0}
          title={trained ? `V trénovací sadě: „${example.label}“` : 'Přidat do trénovací sady s tímto štítkem'}
          className={[
            'shrink-0 px-2 py-1 text-[0.72rem] rounded-[var(--radius-xs)] border transition-colors disabled:opacity-50',
            trained
              ? 'border-[var(--color-sage)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]'
              : 'border-[var(--color-copper)] text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)]',
          ].join(' ')}
        >
          {train.isPending ? '…' : trained ? '✓ Train' : 'Train'}
        </button>
      </div>
      {trained && (
        <button
          type="button"
          onClick={() => untrain.mutate()}
          disabled={untrain.isPending}
          className="self-start text-[0.68rem] text-[var(--color-ink-4)] hover:text-[var(--color-brick)] underline decoration-dotted underline-offset-2 disabled:opacity-50"
        >
          {untrain.isPending ? 'Odebírám…' : 'Odebrat z trénovací sady'}
        </button>
      )}
    </div>
  );
}
