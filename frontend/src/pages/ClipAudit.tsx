import { useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import Tabs, { type Tab } from '@/components/Tabs';
import FilterChip from '@/components/FilterChip';
import InfiniteSentinel from '@/components/InfiniteSentinel';
import ImageTagBadge from '@/components/ImageTagBadge';
import ImageRenderBadge from '@/components/ImageRenderBadge';
import ImageLightbox from '@/components/ImageLightbox';
import NoteFlagControl from '@/components/NoteFlagControl';
import TrainControl from '@/components/TrainControl';
import type { LabelOption } from '@/components/LabelCombobox';
import DedupBreakdown from '@/components/DedupBreakdown';
import { useInfiniteList } from '@/lib/useInfiniteList';
import {
  fetchClipAuditProperties,
  fetchPropertySourcesByPropertyIds,
  fetchImagesByListingIds,
  fetchImageAnnotationsByImageIds,
  fetchTrainingExamplesForImageIds,
  fetchBorderCasesByImageIds,
  fetchDistinctTrainingLabels,
  CLIP_AUDIT_PAGE_SIZE,
  type ClipAuditPropertyRow,
} from '@/lib/queries';
import type { KeysetCursor } from '@/lib/keyset';
import {
  getDedupAudit,
  setImageAnnotation,
  deleteImageAnnotation,
  type DedupAuditRow,
  type ImageAnnotation,
  type TrainingExample,
} from '@/lib/api';
import { CATEGORY_MAIN_TABS } from '@/lib/categoryMainTabs';
import { IMAGE_TAG_LABELS, FINE_TAG_KEYS, imageTagLabel } from '@/lib/imageTags';
import { fmtRelative } from '@/lib/format';
import { listingPath } from '@/lib/listingUrl';
import { portalLabel } from '@/lib/portals';
import { imageSrc } from '@/lib/imageUrl';
import type { ImagePublic, PropertySource } from '@/lib/types';

/* /clip-audit — CLIP model auditing: how well the self-hosted tagger (room/plan type)
 * and its orthogonal render-vs-photo score hold up at scale, across real inventory.
 * Two tabs share ONE feed (properties -> child listings -> images, grouped, infinite
 * scroll) since fine_tag/logical_tag and render_score come from the SAME CLIP call —
 * only the overlay badge + filter row differ per tab. Reuses the exact anon read path
 * Browse/Listing-Detail/dedup already use (browse_list, property_sources_public,
 * images_public) and the exact Decision-history components (DedupBreakdown) so "which
 * dedup level this pair settled at" needs no new rendering code. */

type Mode = 'tagging' | 'render';

const TABS: ReadonlyArray<Tab<Mode>> = [
  { key: 'tagging', label: 'Tagging' },
  { key: 'render', label: 'Render diagnostics' },
];

// No "Vše" (all types) — browse_list's only covering index is (category_main,
// category_type, first_seen_at desc, …); an unfiltered recency scan measured ~3.5s
// cold on the full active cohort (over the anon 3s budget). See fetchClipAuditProperties.
const TYPE_TABS = CATEGORY_MAIN_TABS.filter((t) => t.id !== '');

const TAG_OPTIONS = Object.keys(IMAGE_TAG_LABELS).filter(
  // The fine-only sub-styles (situation_plan, cadastral_map, …) collapse into
  // site_plan for the engine; filter on the 15 canonical logical tags only, the
  // same set the render badge / dedup engine reason about.
  (k) => !['situation_plan', 'cadastral_map', 'aerial_plot', 'location_map',
           'energy_certificate', 'document_text'].includes(k),
);

interface PropertyPage {
  rows: ClipAuditPropertyRow[];
  nextCursor: KeysetCursor | null;
}

function chunk<T>(arr: readonly T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

export default function ClipAudit() {
  const [params, setParams] = useSearchParams();
  const mode: Mode = params.get('tab') === 'render' ? 'render' : 'tagging';
  const setMode = (m: Mode) => {
    const sp = new URLSearchParams(params);
    sp.set('tab', m);
    setParams(sp, { replace: true });
  };

  const [categoryMain, setCategoryMain] = useState<string>(TYPE_TABS[0]?.id ?? 'byt');
  const [tagFilter, setTagFilter] = useState('');
  const [renderMin, setRenderMin] = useState('');
  const [renderMax, setRenderMax] = useState('');
  // "already have all images tagged" — hides properties still mid-backfill (any
  // untagged image on any of their listings) so review only lands on finished work.
  // fine_tag completeness, not render_score (render_score is deliberately NULL for
  // drawings/documents by taxonomy design, not incompleteness) — so this applies the
  // same on both tabs.
  const [onlyFullyTagged, setOnlyFullyTagged] = useState(false);

  const properties = useInfiniteList<ClipAuditPropertyRow, PropertyPage>({
    queryKey: ['clip-audit', 'properties', categoryMain],
    queryFn: (cursor) =>
      fetchClipAuditProperties(categoryMain, cursor as KeysetCursor | null),
    pageSize: CLIP_AUDIT_PAGE_SIZE,
    getRowId: (r) => r.property_id,
  });

  const pages = useMemo(
    () => chunk(properties.rows, CLIP_AUDIT_PAGE_SIZE),
    [properties.rows],
  );

  // The Train label combobox's suggestions (Tagging tab only — Render is a continuous
  // score, not a pick-a-category label): the fixed CLIP taxonomy + anything the
  // operator already typed into the training set from either audit page.
  const trainingLabelsQ = useQuery({
    queryKey: ['clip-audit', 'training-labels'],
    queryFn: fetchDistinctTrainingLabels,
    staleTime: 30_000,
  });
  const labelOptions: LabelOption[] = useMemo(() => {
    // CLIP's 19 real fine_tag classes (not TAG_OPTIONS's 15 collapsed logical tags
    // used by the Tag filter above — see imageTags.ts), keyed by canonical value,
    // plus whatever open-vocabulary labels the operator has already typed in from
    // either audit page.
    const taxonomy: LabelOption[] = FINE_TAG_KEYS.map((key) => ({
      value: key,
      label: imageTagLabel(key) ?? key,
    }));
    const known = new Set(FINE_TAG_KEYS);
    const custom: LabelOption[] = (trainingLabelsQ.data ?? [])
      .filter((v) => !known.has(v))
      .map((v) => ({ value: v, label: v }));
    return [...taxonomy, ...custom].sort((a, b) => a.label.localeCompare(b.label, 'cs'));
  }, [trainingLabelsQ.data]);

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">CLIP Audit</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] max-w-3xl">
          Review the self-hosted CLIP tagger's calls across real inventory — room/plan
          classification on the Tagging tab, the orthogonal render-vs-photo score on
          Render diagnostics.
        </p>
      </header>

      <ModelExplainer />

      <div className="mt-6">
        <Tabs tabs={TABS} active={mode} onChange={setMode} />
      </div>

      <div className="mt-4 flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)] mr-1">
            Typ
          </span>
          {TYPE_TABS.map((t) => (
            <FilterChip
              key={t.id}
              on={categoryMain === t.id}
              label={t.label}
              onClick={() => setCategoryMain(t.id)}
            />
          ))}
          <span className="mx-1 h-4 w-px bg-[var(--color-rule)]" />
          <FilterChip
            on={onlyFullyTagged}
            label="Jen kompletně otagované"
            onClick={() => setOnlyFullyTagged((v) => !v)}
          />
        </div>
        {mode === 'tagging' ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)] mr-1">
              Tag
            </span>
            <FilterChip on={tagFilter === ''} label="Vše" onClick={() => setTagFilter('')} />
            {TAG_OPTIONS.map((tag) => (
              <FilterChip
                key={tag}
                on={tagFilter === tag}
                label={imageTagLabel(tag) ?? tag}
                onClick={() => setTagFilter(tag)}
              />
            ))}
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2 text-[0.78rem] text-[var(--color-ink-3)]">
            <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)]">
              Render skóre
            </span>
            <input
              value={renderMin}
              onChange={(e) => setRenderMin(e.target.value)}
              inputMode="decimal"
              placeholder="0,00"
              aria-label="Min render skóre"
              className="w-16 px-1.5 py-0.5 font-mono tabular-nums rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
            />
            <span>–</span>
            <input
              value={renderMax}
              onChange={(e) => setRenderMax(e.target.value)}
              inputMode="decimal"
              placeholder="1,00"
              aria-label="Max render skóre"
              className="w-16 px-1.5 py-0.5 font-mono tabular-nums rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
            />
          </div>
        )}
      </div>

      <div className="mt-6 flex flex-col gap-6">
        {properties.isLoading ? (
          <p className="text-sm text-[var(--color-ink-3)]">Načítám…</p>
        ) : pages.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-3)]">Žádné nemovitosti tohoto typu.</p>
        ) : (
          pages.map((page, i) => (
            <PropertyPageGroup
              key={i}
              properties={page}
              mode={mode}
              tagFilter={tagFilter}
              renderMin={renderMin ? Number(renderMin.replace(',', '.')) : null}
              renderMax={renderMax ? Number(renderMax.replace(',', '.')) : null}
              labelOptions={labelOptions}
              onlyFullyTagged={onlyFullyTagged}
            />
          ))
        )}
      </div>

      <InfiniteSentinel
        onReach={properties.fetchNextPage}
        hasNextPage={properties.hasNextPage}
        isFetchingNextPage={properties.isFetchingNextPage}
        loadedCount={properties.loadedCount}
        total={null}
      />
    </div>
  );
}

function ModelExplainer() {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-4 border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-2)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full px-4 py-2.5 flex items-center justify-between gap-2 text-left text-sm"
      >
        <span>Model &amp; parametry</span>
        <span className="text-[var(--color-ink-4)]" aria-hidden>{open ? '▴' : '▾'}</span>
      </button>
      {open && (
        <div className="px-4 pb-4 text-[0.82rem] text-[var(--color-ink-2)] leading-relaxed space-y-2">
          <p>
            Self-hosted <span className="font-mono">openai/clip-vit-base-patch32</span>{' '}
            (zero-shot), run against a fixed anchor taxonomy
            (<span className="font-mono">data/clip_taxonomy.json</span>). The room/plan tag
            is the anchor with the highest cosine similarity to the image; the render score
            is a SEPARATE softmax over a render-vs-photo anchor pair from the SAME
            embedding — one model call produces both signals shown on these two tabs.
          </p>
          <p>
            <strong className="text-[var(--color-ink)]">Live-tunable, no redeploy</strong>{' '}
            (<Link to="/settings#dedup-engine" className="text-[var(--color-copper)] hover:underline">Settings → Dedup engine</Link>):
            whether the engine prefers CLIP tags over the paid classifier, and the two
            cosine bars that route the forensic visual compare.
          </p>
          <p>
            <strong className="text-[var(--color-ink)]">Hardcoded in code</strong> (changing
            these needs a deploy, not a Settings edit): the anchor taxonomy itself
            (adding/rewording a prompt), and the render-exclusion cutoff
            (<span className="font-mono">RENDER_SCORE_EXCLUDE_MIN = 0.95</span>) that drops
            high-render images from the byt merge signal.
          </p>
        </div>
      )}
    </div>
  );
}

// A property qualifies once every image on every one of its listings carries a
// fine_tag — vacuously false for a property with zero images (nothing to review).
// Capped by fetchImagesByListingIds' own perId cap (200/listing); a listing with
// more images than that would under-check, same known limit as every other reader
// of this batched fetch.
function isFullyTagged(
  propertyId: number,
  sourcesMap: Map<number, PropertySource[]> | undefined,
  imagesMap: Map<number, ImagePublic[]> | undefined,
): boolean {
  const srcs = sourcesMap?.get(propertyId) ?? [];
  if (srcs.length === 0) return false;
  let sawImage = false;
  for (const src of srcs) {
    for (const img of imagesMap?.get(src.sreality_id) ?? []) {
      sawImage = true;
      if (img.clip_fine_tag == null) return false;
    }
  }
  return sawImage;
}

function PropertyPageGroup({
  properties,
  mode,
  tagFilter,
  renderMin,
  renderMax,
  labelOptions,
  onlyFullyTagged,
}: {
  properties: ClipAuditPropertyRow[];
  mode: Mode;
  tagFilter: string;
  renderMin: number | null;
  renderMax: number | null;
  labelOptions: LabelOption[];
  onlyFullyTagged: boolean;
}) {
  const propertyIds = useMemo(() => properties.map((p) => p.property_id), [properties]);

  const sourcesQ = useQuery({
    queryKey: ['clip-audit', 'sources', propertyIds],
    queryFn: () => fetchPropertySourcesByPropertyIds(propertyIds),
    enabled: propertyIds.length > 0,
  });
  const sourcesMap = sourcesQ.data;

  const srealityIds = useMemo(() => {
    const s = new Set<number>();
    for (const list of sourcesMap?.values() ?? []) {
      for (const src of list) s.add(src.sreality_id);
    }
    return [...s];
  }, [sourcesMap]);

  const imagesQ = useQuery({
    queryKey: ['clip-audit', 'images', srealityIds],
    queryFn: () => fetchImagesByListingIds(srealityIds, 200),
    enabled: srealityIds.length > 0,
  });
  const imagesMap = imagesQ.data;

  const imageIds = useMemo(() => {
    const s: number[] = [];
    for (const list of imagesMap?.values() ?? []) {
      for (const img of list) s.push(img.id);
    }
    return s;
  }, [imagesMap]);

  const annotationsQ = useQuery({
    queryKey: ['clip-audit', 'annotations', imageIds],
    queryFn: () => fetchImageAnnotationsByImageIds(imageIds),
    enabled: imageIds.length > 0,
  });

  const auditQ = useQuery({
    queryKey: ['clip-audit', 'audit', propertyIds],
    queryFn: () => getDedupAudit({ property_id_in: propertyIds, limit: 300 }),
    enabled: propertyIds.length > 0,
  });

  const trainingQ = useQuery({
    queryKey: ['clip-audit', 'training', imageIds],
    queryFn: () => fetchTrainingExamplesForImageIds(imageIds),
    enabled: imageIds.length > 0 && mode === 'tagging',
  });

  const borderCasesQ = useQuery({
    queryKey: ['clip-audit', 'border-cases', imageIds],
    queryFn: () => fetchBorderCasesByImageIds(imageIds),
    enabled: imageIds.length > 0 && mode === 'tagging',
  });

  // Until sources+images have loaded for this group, completeness can't be
  // determined yet — hold off rendering rather than flash an incomplete result.
  const dataReady = sourcesQ.isSuccess && (srealityIds.length === 0 || imagesQ.isSuccess);
  const visible = onlyFullyTagged
    ? (dataReady ? properties.filter((p) => isFullyTagged(p.property_id, sourcesMap, imagesMap)) : [])
    : properties;

  return (
    <>
      {visible.map((p) => (
        <PropertyCard
          key={p.property_id}
          property={p}
          sources={sourcesMap?.get(p.property_id) ?? []}
          imagesBySreality={imagesMap ?? new Map()}
          annotations={annotationsQ.data ?? new Map()}
          training={trainingQ.data ?? new Map()}
          borderCases={borderCasesQ.data ?? new Set()}
          labelOptions={labelOptions}
          auditRows={
            auditQ.data?.data.filter(
              (r) => r.left_property_id === p.property_id || r.right_property_id === p.property_id,
            ) ?? []
          }
          mode={mode}
          tagFilter={tagFilter}
          renderMin={renderMin}
          renderMax={renderMax}
        />
      ))}
    </>
  );
}

function PropertyCard({
  property,
  sources,
  imagesBySreality,
  annotations,
  training,
  borderCases,
  labelOptions,
  auditRows,
  mode,
  tagFilter,
  renderMin,
  renderMax,
}: {
  property: ClipAuditPropertyRow;
  sources: PropertySource[];
  imagesBySreality: Map<number, ImagePublic[]>;
  annotations: Map<number, ImageAnnotation>;
  training: Map<number, TrainingExample>;
  borderCases: Set<number>;
  labelOptions: LabelOption[];
  auditRows: DedupAuditRow[];
  mode: Mode;
  tagFilter: string;
  renderMin: number | null;
  renderMax: number | null;
}) {
  const listings = sources.length > 0
    ? sources
    : [{
        property_id: property.property_id, sreality_id: property.sreality_id,
        source: 'sreality', source_url: null, source_id_native: null,
        is_active: true, price_czk: null, first_seen_at: property.first_seen_at,
        last_seen_at: property.first_seen_at,
      }];

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)] p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
        <span className="font-mono tabular-nums text-[var(--color-ink-2)]">
          #{property.property_id}
        </span>
        <span className="text-[0.7rem] uppercase tracking-[0.08em] text-[var(--color-ink-4)]">
          {property.category_main}
        </span>
        <span className="text-[0.72rem] text-[var(--color-ink-4)]">
          poprvé {fmtRelative(property.first_seen_at)}
        </span>
        {listings.length > 1 && (
          <span className="text-[0.72rem] text-[var(--color-ink-4)]">
            {listings.length} inzeráty
          </span>
        )}
      </div>

      <div
        className="mt-3 grid gap-3"
        style={{ gridTemplateColumns: `repeat(${Math.max(listings.length, 1)}, minmax(0, 1fr))` }}
      >
        {listings.map((l) => (
          <ListingColumn
            key={l.sreality_id}
            listing={l}
            images={imagesBySreality.get(l.sreality_id) ?? []}
            annotations={annotations}
            training={training}
            borderCases={borderCases}
            labelOptions={labelOptions}
            mode={mode}
            tagFilter={tagFilter}
            renderMin={renderMin}
            renderMax={renderMax}
          />
        ))}
      </div>

      {auditRows.length > 0 && (
        <div className="mt-3 flex flex-col gap-2 border-t border-[var(--color-rule-soft)] pt-3">
          <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)]">
            Dedup rozhodnutí
          </span>
          {auditRows.map((r) => (
            <div key={r.audit_id} className="flex flex-col gap-1">
              <div className="flex items-center gap-2 text-[0.74rem]">
                <span
                  className={
                    r.outcome === 'merged'
                      ? 'text-[var(--color-copper)]'
                      : 'text-[var(--color-brick)]'
                  }
                >
                  {r.outcome === 'merged' ? 'sloučeno' : 'zamítnuto'}
                </span>
                <span className="text-[var(--color-ink-4)] font-mono">{r.stage}</span>
                <span className="text-[var(--color-ink-3)] font-mono tabular-nums">
                  {r.left_sreality_id} ↔ {r.right_sreality_id}
                </span>
              </div>
              <DedupBreakdown rungs={r.audit_breakdown} />
            </div>
          ))}
          <Link
            to={`/dedup?audit_property=${property.property_id}#history`}
            className="self-start text-[0.72rem] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] underline decoration-dotted underline-offset-2"
          >
            zobrazit v Decision history →
          </Link>
        </div>
      )}
    </div>
  );
}

function ListingColumn({
  listing,
  images,
  annotations,
  training,
  borderCases,
  labelOptions,
  mode,
  tagFilter,
  renderMin,
  renderMax,
}: {
  listing: PropertySource;
  images: ImagePublic[];
  annotations: Map<number, ImageAnnotation>;
  training: Map<number, TrainingExample>;
  borderCases: Set<number>;
  labelOptions: LabelOption[];
  mode: Mode;
  tagFilter: string;
  renderMin: number | null;
  renderMax: number | null;
}) {
  const filtered = images.filter((img) => {
    if (mode === 'tagging' && tagFilter) return img.clip_logical_tag === tagFilter;
    if (mode === 'render') {
      if (img.clip_render_score == null) return false;
      if (renderMin != null && img.clip_render_score < renderMin) return false;
      if (renderMax != null && img.clip_render_score > renderMax) return false;
    }
    return true;
  });
  const [lightboxAt, setLightboxAt] = useState<number | null>(null);

  return (
    <div className="min-w-0 flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5 text-[0.72rem]">
        <Link
          to={listingPath(listing.sreality_id)}
          className="font-mono tabular-nums text-[var(--color-ink-2)] hover:text-[var(--color-copper)]"
        >
          {listing.sreality_id}
        </Link>
        <span className="text-[var(--color-ink-4)] uppercase tracking-[0.06em]">
          {portalLabel(listing.source) ?? listing.source}
        </span>
        {!listing.is_active && (
          <span className="text-[var(--color-brick)] text-[0.66rem]">neaktivní</span>
        )}
      </div>
      {filtered.length === 0 ? (
        <p className="text-[0.7rem] text-[var(--color-ink-4)]">bez odpovídajících fotek</p>
      ) : (
        <div className="grid grid-cols-3 gap-1">
          {filtered.map((img, i) => (
            <ImageCell
              key={img.id}
              image={img}
              mode={mode}
              annotation={annotations.get(img.id)}
              example={training.get(img.id)}
              borderCase={borderCases.has(img.id)}
              labelOptions={labelOptions}
              onOpen={() => setLightboxAt(i)}
            />
          ))}
        </div>
      )}
      {lightboxAt != null && (
        <ImageLightbox
          images={filtered}
          startIndex={lightboxAt}
          onClose={() => setLightboxAt(null)}
        />
      )}
    </div>
  );
}

function ImageCell({
  image,
  mode,
  annotation,
  example,
  borderCase,
  labelOptions,
  onOpen,
}: {
  image: ImagePublic;
  mode: Mode;
  annotation: ImageAnnotation | undefined;
  example: TrainingExample | undefined;
  borderCase: boolean;
  labelOptions: LabelOption[];
  onOpen: () => void;
}) {
  const qc = useQueryClient();
  const flagged = mode === 'tagging' ? !!annotation?.tag_flagged : !!annotation?.render_flagged;

  const save = useMutation({
    mutationFn: (input: { flagged: boolean; note: string | null }) =>
      setImageAnnotation({
        image_id: image.id,
        tag_flagged: mode === 'tagging' ? input.flagged : !!annotation?.tag_flagged,
        render_flagged: mode === 'render' ? input.flagged : !!annotation?.render_flagged,
        note: input.note,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['clip-audit', 'annotations'] }),
  });
  const remove = useMutation({
    mutationFn: () => deleteImageAnnotation(image.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['clip-audit', 'annotations'] }),
  });

  return (
    <div className="flex flex-col gap-1">
      <button
        type="button"
        onClick={onOpen}
        className="group relative block w-full aspect-square overflow-hidden rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)]"
      >
        <img
          src={imageSrc(image)}
          alt=""
          loading="lazy"
          className="w-full h-full object-cover"
        />
        {mode === 'tagging' ? (
          <ImageTagBadge
            tag={image.clip_fine_tag}
            confidence={image.clip_confidence}
            className="absolute bottom-1 left-1 max-w-[calc(100%-0.5rem)] truncate"
          />
        ) : (
          <ImageRenderBadge
            renderScore={image.clip_render_score}
            className="absolute bottom-1 left-1"
          />
        )}
      </button>
      <span className="text-[0.62rem] font-mono tabular-nums text-[var(--color-ink-4)] truncate">
        pHash {image.phash ?? '—'}
      </span>
      <NoteFlagControl
        flagged={flagged}
        note={annotation?.note}
        flagLabel={mode === 'tagging' ? 'Označit tag' : 'Označit skóre'}
        flaggedLabel={mode === 'tagging' ? 'Tag špatně' : 'Skóre špatně'}
        notePlaceholder="Co je špatně?"
        busy={save.isPending || remove.isPending}
        onSave={(input) => save.mutate(input)}
        onRemove={() => remove.mutate()}
      />
      {/* Linear-probe training-set data collection — Tagging tab only (Render is a
          continuous score, not a category to pick from a label list). */}
      {mode === 'tagging' && (
        <TrainControl
          image={image}
          example={example}
          borderCase={borderCase}
          labelOptions={labelOptions}
          queryKeyPrefix="clip-audit"
        />
      )}
    </div>
  );
}
