import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import { ROUTES, withQuery } from '@/lib/routes';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getNewDedupLabelingOverview,
  addNewDedupTag,
  renameNewDedupTag,
  removeNewDedupTag,
  setNewDedupTagFlags,
  growNewDedupSample,
  listNewDedupProposals,
  listNewDedupOriginalTags,
  setNewDedupProposalState,
  bulkSetNewDedupProposalState,
  listNewDedupTagImages,
  setNewDedupTagAnnotation,
  bulkSetNewDedupTagAnnotation,
  listNewDedupPositiveTagsForImages,
  listNewDedupSettings,
  getNewDedupTagCandidates,
  drawNewDedupTagCandidates,
  type TagState,
  type TagExcludedReason,
  type TagCandidateDraw,
  type NewDedupTag,
  type NewDedupLabelProposal,
  type NewDedupTagImage,
  type NewDedupCandidateBucket,
  type NewDedupCandidateSummary,
  type NewDedupCandidateDrawResult,
} from '@/lib/api';
import { fetchImagesByImageIds } from '@/lib/queries';
import { imageSrc } from '@/lib/imageUrl';
import { pushToast } from '@/lib/toast';
import Tabs from '@/components/Tabs';
import ImageTagBadge from '@/components/ImageTagBadge';
import ImageLightbox from '@/components/ImageLightbox';
import BorderCaseButton from '@/components/BorderCaseButton';
import ImageSizeToggle from '@/components/ImageSizeToggle';
import Spinner from '@/components/Spinner';
import TaxonomyManageModal from '@/components/TaxonomyManageModal';
import LabelCombobox, { type LabelOption } from '@/components/LabelCombobox';
import { Chevron, useCollapsed } from '@/components/settings/SectionChrome';
import { CATEGORY_MAIN_TABS } from '@/lib/categoryMainTabs';
import { fmtRelative } from '@/lib/format';
import { usePersistedFlag } from '@/lib/persistedFlag';
import { useBorderCases, type BorderCaseStore } from '@/lib/useBorderCases';
import ErrorBanner from '@/components/ErrorBanner';
import TriStateControl, {
  BATCH_ACTIONS,
  EXCLUDED_REASON_META,
  STATE_META,
} from '@/components/tag-annotations/TriStateControl';
import ImageTagDetailPanel from '@/components/tag-annotations/ImageTagDetailPanel';
import {
  NEW_DEDUP_CANDIDATES_KEY as CANDIDATES_KEY,
  NEW_DEDUP_OVERVIEW_KEY as OVERVIEW_KEY,
  NEW_DEDUP_PROPOSALS_KEY as PROPOSALS_KEY,
  NEW_DEDUP_TAG_IMAGES_KEY as TAG_IMAGES_KEY,
} from '@/lib/newDedupKeys';
import type { ImagePublic } from '@/lib/types';

const SETTINGS_KEY = ['new-dedup', 'settings'];

/* 'all' is the union of the other three — the tab to work in when you want the
 * grid to hold still: reviewing a tile there greys it in place instead of
 * moving it out from under the cursor. */
type TabKey = 'all' | 'pending' | 'confirmed' | 'dismissed';
const STATUS_TABS: ReadonlyArray<{ key: TabKey; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'pending', label: 'Pending' },
  { key: 'confirmed', label: 'Confirmed' },
  { key: 'dismissed', label: 'Dismissed' },
];

/* The two review workflows this page supports (decided 2026-08-26): tag-centric
 * batch review of the secondary-CLIP's SUGGESTIONS (the default — fast, because
 * each screen only asks about one tag), and a Candidates browse over ONE tag's
 * own review queue (migration 450) plus everything already decided for it —
 * including images the model never proposed the tag for, which is the only way
 * to answer "show me every image where kitchen = excluded".
 *
 * The queue used to be `dedup_sim.labeling_sample`: 1,200 untargeted images
 * every tag shared. Rare tags are a fraction of a percent of the corpus, so a
 * random pool can never build their sets — candidates are FOUND now, by ranking
 * a bounded pool against the tag's own centroid. */
type Mode = 'proposals' | 'candidates';
type CandidateStateFilter = TagState | 'untouched' | 'all';
const CANDIDATE_STATE_OPTIONS: ReadonlyArray<{ key: CandidateStateFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'untouched', label: 'Untouched' },
  { key: 'positive', label: 'Positive' },
  { key: 'negative', label: 'Negative' },
  { key: 'excluded', label: 'Excluded' },
];

/* The rank bands a candidate can come from. The short label is what fits on a
 * tile; the title is the WHY, because a band is only meaningful as a deliberate
 * mix — a pure top-N produces a prototypical training set that fails on the odd
 * cases, which is the failure the whole mix exists to avoid. */
const DRAW_META: Record<TagCandidateDraw, { label: string; title: string }> = {
  centroid_head: {
    label: 'head',
    title:
      "Nearest the tag's centroid — the highest-yield band, and the only one that can build a rare tag's positive set at all. On its own it would produce a prototypical set that fails on odd cases.",
  },
  centroid_mid: {
    label: 'mid',
    title:
      'Just below the head, where the confusion clusters live (bathrooms, circulation, living spaces) — the hard cases the head cannot surface.',
  },
  random: {
    label: 'random',
    title:
      "An unranked sample of the pool: the only honest base rate for this tag, and the only band that can surface a positive the centroid is blind to. Sustained positives here mean the centroid is missing a mode.",
  },
};
/* Falls back to the raw key — the band vocabulary lives in the database's CHECK
 * constraint, and a band added there must not render as blank here. */
const drawLabel = (draw: string): string => DRAW_META[draw as TagCandidateDraw]?.label ?? draw;
const drawTitle = (draw: string): string => DRAW_META[draw as TagCandidateDraw]?.title ?? '';

/* A bucket's YIELD, appended to its tooltip. Silent until something has been
 * decided: "0 positive" on an untouched bucket would read as a verdict on the
 * band rather than as the absence of one. */
const yieldPhrase = (b: NewDedupCandidateBucket): string =>
  b.positive + b.negative === 0
    ? ''
    : `, ${b.positive} positive of ${b.positive + b.negative} decided`;

/* Mirrors toolkit/tag_candidates.DEFAULT_DRAW_COUNT. Only the INITIAL value of
 * an input the operator overtypes — the count that actually binds is the
 * server's, and the 1..DRAW_COUNT_MAX range is enforced there (a 422 carries
 * its own message) rather than copied into a second validator here. */
const DEFAULT_DRAW_COUNT = '120';

type ProposalsPage = { data: NewDedupLabelProposal[] };

/* Photo size for the review grid, remembered per browser. Its OWN key, not
 * Browse's: the two grids show different things at different densities, and
 * resizing tiles here must never reshape the listing cards there. */
const IMAGE_LARGE_KEY = 'sreality.newDedupLabeling.imageLarge';

/* The two --tile-min values a review tile can be. "large" is exactly double
 * "small" — the same rule Browse's cards follow (CARD_IMAGE_MIN) — so the
 * shared small/large switch means the same thing on both pages. */
const TILE_MIN = { sm: '14rem', lg: '28rem' } as const;

/* One key shape for everything the page holds per TILE — the staged tag edit,
 * the in-flight action, the lightbox position. It is label_proposals' own PK:
 * one image can carry proposals from several models, and they must never share
 * a slot. Module scope so the hooks that key on it have a stable dependency. */
const rowKey = (imageId: number, model: string) => `${imageId}:${model}`;
const draftKey = (p: NewDedupLabelProposal) => rowKey(p.image_id, p.model);

/* The threshold arrives with the overview payload, so it is briefly undefined
 * on first paint — rendered the way this page already renders a not-yet-loaded
 * setting (`{secondaryModel ?? '…'}`) rather than by hardcoding a second copy
 * of the number. */
const pctLabel = (rate: number | undefined) =>
  rate == null ? '…' : `${Math.round(rate * 100)}%`;

/* Keyboard review for a flat list of `n` tiles: arrow keys / j-k move a
 * focused index, 1/2/3 (or p/x) set that tile's state and auto-advance —
 * "assign primary tag, next image" in one keystroke. 4 is excluded·pruned,
 * the one exclusion reason ⊘ does not give you; it sits beside the other
 * digits and auto-advances the same way, so the rare case costs one keystroke
 * rather than a click-and-hunt. Attached to the grid container (tabIndex=0),
 * not the window, so it never fights a text input elsewhere on the page (the
 * tag combobox, the sample-size field). */
const KEYBOARD_ACTIONS: ReadonlyArray<{
  keys: readonly string[];
  state: TagState;
  reason: TagExcludedReason | null;
}> = [
  { keys: ['1', 'p'], state: 'positive', reason: null },
  { keys: ['2'], state: 'negative', reason: null },
  { keys: ['3', 'x'], state: 'excluded', reason: 'ambiguous' },
  { keys: ['4'], state: 'excluded', reason: 'pruned' },
];

function useGridKeyboardReview(
  n: number,
  onSetState: (index: number, state: TagState, excludedReason: TagExcludedReason | null) => void,
) {
  const [focused, setFocused] = useState<number | null>(null);
  useEffect(() => {
    if (focused != null && focused >= n) setFocused(n > 0 ? n - 1 : null);
  }, [n, focused]);
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (n === 0) return;
    const key = e.key.toLowerCase();
    const cur = focused ?? 0;
    if (key === 'arrowright' || key === 'arrowdown' || key === 'j') {
      e.preventDefault();
      setFocused(Math.min(n - 1, cur + 1));
      return;
    }
    if (key === 'arrowleft' || key === 'arrowup' || key === 'k') {
      e.preventDefault();
      setFocused(Math.max(0, focused == null ? 0 : cur - 1));
      return;
    }
    const action = KEYBOARD_ACTIONS.find((a) => a.keys.includes(key));
    if (action && focused != null) {
      e.preventDefault();
      onSetState(focused, action.state, action.reason);
      setFocused(Math.min(n - 1, focused + 1));
    }
  };
  return { focused, setFocused, onKeyDown };
}

export default function NewDedupLabeling() {
  const qc = useQueryClient();
  const overviewQ = useQuery({ queryKey: OVERVIEW_KEY, queryFn: getNewDedupLabelingOverview });
  const settingsQ = useQuery({ queryKey: SETTINGS_KEY, queryFn: listNewDedupSettings });

  const [mode, setMode] = useState<Mode>('proposals');
  const [labelFilter, setLabelFilter] = useState<string | null>(null);
  // Coverage ceiling for the TAG list (not the images): with dozens of tags,
  // "which tags are still short of Gate 1" is the question that decides what to
  // label next, and the full chart buries it. '' = no ceiling.
  const [maxTrained, setMaxTrained] = useState('');
  const [manageOpen, setManageOpen] = useState(false);
  const [tab, setTab] = useState<TabKey>('pending');
  const [candidateState, setCandidateState] = useState<CandidateStateFilter>('untouched');
  const [showOriginal, setShowOriginal] = useState(false);
  // The "Original tag" view filters by the production CLIP tagger's own
  // fine_tag — a different, fixed vocabulary from Taxonomy v1 (`labelFilter`
  // above), so it gets its own independent filter state rather than
  // reinterpreting labelFilter. Both are remembered across toggles, so
  // switching back restores whichever tag was picked in that view.
  const [originalTagFilter, setOriginalTagFilter] = useState<string | null>(null);
  const usingOriginalTagFilter = mode === 'proposals' && showOriginal;
  const imageLarge = usePersistedFlag(IMAGE_LARGE_KEY, false);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const [pendingRowKeys, setPendingRowKeys] = useState<ReadonlySet<string>>(new Set());
  const [drafts, setDrafts] = useState<ReadonlyMap<string, string>>(new Map());
  const [detailImageId, setDetailImageId] = useState<number | null>(null);

  const allTags = useMemo(() => overviewQ.data?.data.tags ?? [], [overviewQ.data]);
  const tagByLabel = useMemo(() => new Map(allTags.map((t) => [t.label, t])), [allTags]);
  const activeTagId = labelFilter ? (tagByLabel.get(labelFilter)?.id ?? null) : null;

  const originalTagsQ = useQuery({
    queryKey: ['new-dedup', 'labeling', 'original-tags'],
    queryFn: listNewDedupOriginalTags,
    enabled: usingOriginalTagFilter,
    staleTime: Infinity, // a fixed, static vocabulary — never goes stale
  });
  const originalTagOptions = originalTagsQ.data?.data ?? [];

  // Keyed and fetched off the EFFECTIVE filter values, not off
  // usingOriginalTagFilter itself — flipping New/Original is a display-only
  // toggle (which badge shows) and must never blink the grid on its own; it
  // only changes the result set when it actually drops a filter that was set.
  const effectiveLabel = usingOriginalTagFilter ? null : labelFilter;
  const effectiveOriginalTag = usingOriginalTagFilter ? originalTagFilter : null;
  const proposalsKey = useMemo(
    () => [...PROPOSALS_KEY, tab, effectiveLabel, effectiveOriginalTag],
    [tab, effectiveLabel, effectiveOriginalTag],
  );
  const proposalsQ = useQuery({
    queryKey: proposalsKey,
    queryFn: () =>
      listNewDedupProposals({
        status: tab,
        label: effectiveLabel ?? undefined,
        original_tag: effectiveOriginalTag ?? undefined,
        limit: 200,
      }),
    enabled: mode === 'proposals',
  });
  const proposals = useMemo(() => proposalsQ.data?.data ?? [], [proposalsQ.data]);

  const tagImagesKey = useMemo(
    () => [...TAG_IMAGES_KEY, activeTagId, candidateState],
    [activeTagId, candidateState],
  );
  const tagImagesQ = useQuery({
    queryKey: tagImagesKey,
    queryFn: () =>
      listNewDedupTagImages(activeTagId as number, {
        state: candidateState === 'all' ? undefined : candidateState,
        limit: 200,
      }),
    enabled: mode === 'candidates' && activeTagId != null,
  });
  const tagImages = useMemo(() => tagImagesQ.data?.data ?? [], [tagImagesQ.data]);

  /* The queue READOUT, independent of the grid: it answers "is there work on
   * this tag, how was it drawn, and can I get more" and must stay right whether
   * or not the Candidates grid is the one on screen. */
  const candidatesKey = useMemo(() => [...CANDIDATES_KEY, activeTagId], [activeTagId]);
  const candidatesQ = useQuery({
    queryKey: candidatesKey,
    queryFn: () => getNewDedupTagCandidates(activeTagId as number),
    enabled: activeTagId != null,
  });
  const candidates: NewDedupCandidateSummary | undefined = candidatesQ.data?.data;

  const imageIds = useMemo(
    () => (mode === 'proposals' ? proposals.map((p) => p.image_id) : tagImages.map((r) => r.image_id)),
    [mode, proposals, tagImages],
  );

  // Photos ACCUMULATE across tabs/modes/actions rather than being re-fetched
  // per grid. Keying the image query on the current id list meant every
  // confirm — which changes that list — swapped in an empty cache entry, so
  // every tile in the grid lost its photo and re-rendered at once. Only
  // never-seen ids are ever requested; a tile that's already on screen never
  // blinks again.
  const [imageCache, setImageCache] = useState<ReadonlyMap<number, ImagePublic>>(new Map());
  const missingIds = useMemo(
    () => [...new Set(imageIds.filter((id) => !imageCache.has(id)))],
    [imageIds, imageCache],
  );
  const imagesQ = useQuery({
    queryKey: ['new-dedup', 'labeling', 'images', missingIds.join(',')],
    queryFn: () => fetchImagesByImageIds(missingIds),
    enabled: missingIds.length > 0,
  });
  useEffect(() => {
    const fetched = imagesQ.data;
    if (!fetched || fetched.size === 0) return;
    setImageCache((prev) => {
      let grew = false;
      const next = new Map(prev);
      fetched.forEach((img, id) => {
        if (!next.has(id)) {
          next.set(id, img);
          grew = true;
        }
      });
      return grew ? next : prev;
    });
  }, [imagesQ.data]);

  /* Assigned (positive) tags per image — batched the same way photos are
   * (accumulate for never-seen ids), then patched in place as tri-state
   * decisions land (see patchPositiveTags), never refetched. A tile only
   * shows the one tag it's reviewing; with multi-label images now possible
   * that's not the same as everything the image is already positive on. */
  const [positiveTagsCache, setPositiveTagsCache] = useState<ReadonlyMap<number, string[]>>(
    new Map(),
  );
  const missingTagIds = useMemo(
    () => [...new Set(imageIds.filter((id) => !positiveTagsCache.has(id)))],
    [imageIds, positiveTagsCache],
  );
  const positiveTagsQ = useQuery({
    queryKey: ['new-dedup', 'labeling', 'positive-tags', missingTagIds.join(',')],
    queryFn: async () => {
      const res = await listNewDedupPositiveTagsForImages(missingTagIds);
      return { requestedIds: missingTagIds, rows: res.data };
    },
    enabled: missingTagIds.length > 0,
  });
  useEffect(() => {
    const result = positiveTagsQ.data;
    if (!result) return;
    setPositiveTagsCache((prev) => {
      const next = new Map(prev);
      for (const id of result.requestedIds) next.set(id, next.get(id) ?? []);
      for (const row of result.rows) {
        const cur = next.get(row.image_id) ?? [];
        if (!cur.includes(row.label)) next.set(row.image_id, [...cur, row.label]);
      }
      return next;
    });
  }, [positiveTagsQ.data]);
  const patchPositiveTags = (imageId: number, label: string, state: TagState) => {
    setPositiveTagsCache((prev) => {
      const cur = prev.get(imageId) ?? [];
      const has = cur.includes(label);
      if (state === 'positive' && !has) {
        const next = new Map(prev);
        next.set(imageId, [...cur, label].sort((a, b) => a.localeCompare(b, 'cs')));
        return next;
      }
      if (state !== 'positive' && has) {
        const next = new Map(prev);
        next.set(imageId, cur.filter((l) => l !== label));
        return next;
      }
      return prev;
    });
  };

  /* "Border case" is image-grain and independent of every tag's tri-state, so
   * it lives outside both grids: toggling it patches the store rather than
   * either grid — no tile moves. */
  const borderCases = useBorderCases(imageIds);

  /* Both grids double as a gallery: clicking a tile enlarges it in the SHARED
   * ImageLightbox and the arrow keys walk the rest of the grid from there. */
  const gallery = useMemo(() => {
    if (mode === 'proposals') {
      return proposals.flatMap((p) => {
        const image = imageCache.get(p.image_id);
        return image ? [{ key: draftKey(p), image, tag: p.label as string | null, confidence: p.confidence }] : [];
      });
    }
    return tagImages.flatMap((r) => {
      const image = imageCache.get(r.image_id);
      return image ? [{ key: String(r.image_id), image, tag: null, confidence: null }] : [];
    });
  }, [mode, proposals, tagImages, imageCache]);
  const galleryImages = useMemo(() => gallery.map((g) => g.image), [gallery]);
  const galleryIndex = useMemo(() => new Map(gallery.map((g, i) => [g.key, i])), [gallery]);
  const [lightboxAt, setLightboxAt] = useState<number | null>(null);

  useEffect(() => {
    setSelected(new Set());
    setDrafts(new Map());
    setLightboxAt(null);
  }, [tab, labelFilter, originalTagFilter, mode, candidateState]);
  useEffect(() => {
    const ids = new Set(imageIds);
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => ids.has(id)));
      return next.size === prev.size ? prev : next;
    });
    setDrafts((prev) => {
      const next = new Map([...prev].filter(([key]) => ids.has(Number(key.split(':')[0]))));
      return next.size === prev.size ? prev : next;
    });
  }, [imageIds]);

  /* Both numbers come from the server so the threshold has exactly ONE
   * definition — the SPA renders it, never decides it, and never recomputes
   * `ambiguity_alert` from the rate. */
  const ambiguityThreshold = overviewQ.data?.data.ambiguity_threshold;
  const ambiguityMinDecisions = overviewQ.data?.data.ambiguity_min_decisions;

  const settingValue = (key: string) => settingsQ.data?.data.find((s) => s.key === key)?.value;
  const gate1Target = (settingValue('labeling_gate1_target_per_tag') as number) ?? 150;
  const proposalTarget = (settingValue('labeling_target_proposals_per_category') as number) ?? 300;
  const secondaryModel = settingValue('labeling_secondary_model') as string | undefined;

  const invalidateOverview = () => qc.invalidateQueries({ queryKey: OVERVIEW_KEY });
  const invalidateProposals = () => qc.invalidateQueries({ queryKey: PROPOSALS_KEY });
  const invalidateTagImages = () => qc.invalidateQueries({ queryKey: TAG_IMAGES_KEY });
  const invalidateCandidates = () => qc.invalidateQueries({ queryKey: candidatesKey });
  // Only the tabs the operator ISN'T looking at. Invalidating the visible one
  // would refetch it and re-render every tile — the churn this page is built
  // to avoid (see patchRows / patchTagImages). None of the others is
  // mounted, so nothing refetches now; they're just correct when opened.
  const invalidateOtherTabs = () =>
    qc.invalidateQueries({
      predicate: (q) => {
        const k = q.queryKey as unknown[];
        return k[0] === 'new-dedup' && k[1] === 'labeling' && k[2] === 'proposals' && k[3] !== tab;
      },
    });

  /* Both grids are patched IN PLACE, never invalidated, for the same reason:
   * a refetch re-renders every tile, which on a page worked through
   * image-by-image reads as "the whole grid jumped". */
  const patchRows = (
    ids: ReadonlyArray<number>,
    model: string,
    patch: 'drop' | ((p: NewDedupLabelProposal) => NewDedupLabelProposal),
  ) => {
    const set = new Set(ids);
    qc.setQueryData<ProposalsPage>(proposalsKey, (old) =>
      old
        ? {
            ...old,
            data: old.data.flatMap((p) =>
              set.has(p.image_id) && p.model === model
                ? patch === 'drop'
                  ? []
                  : [patch(p)]
                : [p],
            ),
          }
        : old,
    );
  };
  const patchTagImages = (
    ids: ReadonlyArray<number>,
    state: TagState,
    excludedReason: TagExcludedReason | null,
  ) => {
    const set = new Set(ids);
    // The reason is patched alongside the state, and CLEARED on any
    // non-excluded state — otherwise a cell moved from excluded to negative
    // would keep showing a reason chip for a decision it no longer carries.
    // `source` follows too: the operator just decided this cell, so whatever
    // it was before (a 442 backfill row, most likely) it is a human decision
    // now and must stop being drawn as manufactured.
    const patch = (r: NewDedupTagImage): NewDedupTagImage => ({
      ...r,
      state,
      source: 'human',
      excluded_reason: state === 'excluded' ? excludedReason : null,
    });
    qc.setQueryData<{ data: NewDedupTagImage[] }>(tagImagesKey, (old) =>
      old
        ? {
            ...old,
            data:
              candidateState === 'all' || candidateState === state
                ? old.data.map((r) => (set.has(r.image_id) ? patch(r) : r))
                : old.data.filter((r) => !set.has(r.image_id)),
          }
        : old,
    );
  };

  const applyReview = (
    ids: ReadonlyArray<number>,
    model: string,
    patch: (p: NewDedupLabelProposal) => NewDedupLabelProposal,
  ) => {
    patchRows(ids, model, tab === 'pending' ? 'drop' : patch);
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => !ids.includes(id)));
      return next.size === prev.size ? prev : next;
    });
    invalidateOtherTabs();
    invalidateOverview();
  };

  // The taxonomy IS the option list for correcting a wrong suggestion — free
  // text still creates a new tag. Deliberately the WHOLE vocabulary, never
  // narrowed by the coverage ceiling below: that filter picks what to work
  // on, it doesn't restrict what a tag can be corrected to.
  const labelOptions: LabelOption[] = useMemo(
    () =>
      allTags
        .map((t) => ({ value: t.label, label: t.label, count: t.gate_count }))
        .sort((a, b) => a.label.localeCompare(b.label, 'cs')),
    [allTags],
  );

  const maxTrainedNum =
    maxTrained.trim() === '' || !Number.isFinite(Number(maxTrained)) || Number(maxTrained) < 0
      ? null
      : Number(maxTrained);
  const visibleTags = useMemo(
    () => (maxTrainedNum == null ? allTags : allTags.filter((t) => t.gate_count <= maxTrainedNum)),
    [allTags, maxTrainedNum],
  );
  const filterOptions = useMemo(
    () => [...visibleTags].sort((a, b) => a.label.localeCompare(b.label, 'cs')),
    [visibleTags],
  );

  // --- taxonomy ---------------------------------------------------------

  const [newLabelText, setNewLabelText] = useState('');
  const addLabelMut = useMutation({
    mutationFn: () => addNewDedupTag(newLabelText.trim()),
    onSuccess: () => {
      setNewLabelText('');
      pushToast('ok', 'Tag added.');
      invalidateOverview();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  const renameLabelMut = useMutation({
    mutationFn: ({ id, label }: { id: number; oldLabel: string; label: string }) =>
      renameNewDedupTag(id, label),
    onSuccess: (_res, vars) => {
      pushToast('ok', 'Renamed.');
      setLabelFilter((cur) => (cur === vars.oldLabel ? vars.label : cur));
      invalidateOverview();
      invalidateProposals();
      invalidateTagImages();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  const removeLabelMut = useMutation({
    mutationFn: ({ id }: { id: number; oldLabel: string }) => removeNewDedupTag(id),
    onSuccess: (_res, vars) => {
      pushToast('ok', 'Removed.');
      setLabelFilter((cur) => (cur === vars.oldLabel ? null : cur));
      invalidateOverview();
      invalidateProposals();
      invalidateTagImages();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  const setFlagsMut = useMutation({
    mutationFn: ({ id, flags }: { id: number; flags: { priority?: boolean; ready_for_training?: boolean } }) =>
      setNewDedupTagFlags(id, flags),
    onSuccess: () => invalidateOverview(),
    onError: (err: Error) => pushToast('err', err.message),
  });

  // --- candidate retrieval --------------------------------------------------

  const [drawCount, setDrawCount] = useState(DEFAULT_DRAW_COUNT);
  const [drawCategory, setDrawCategory] = useState('');
  const drawCountValid = Number.isInteger(Number(drawCount)) && Number(drawCount) > 0;
  /* The last draw's own report, kept on screen rather than only in a toast: the
   * loss counters and any per-category timeout are the ONLY quality signal this
   * mechanism ships, and a shortfall that scrolls away in six seconds reads as
   * "it just gave me fewer". */
  const [lastDraw, setLastDraw] = useState<NewDedupCandidateDrawResult | null>(null);
  const drawMut = useMutation({
    mutationFn: () =>
      drawNewDedupTagCandidates(activeTagId as number, Number(drawCount), drawCategory || null),
    onMutate: () => setLastDraw(null),
    onSuccess: (res) => {
      const d = res.data;
      setLastDraw(d);
      if (d.status === 'insufficient_positives') {
        pushToast(
          'info',
          `No draw — this tag has ${d.verified_positive_count} verified positive images, and a centroid needs ${d.min_verified_positives}.`,
        );
      } else {
        pushToast(
          'ok',
          `Drew ${d.inserted} candidates (${d.dropped_near_dup} near-duplicates, ${d.dropped_property_cap} over the per-property cap).`,
        );
      }
      invalidateOverview();
      invalidateCandidates();
      invalidateTagImages();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });
  // A new tag is a different queue: the previous tag's draw report would read
  // as this one's.
  useEffect(() => setLastDraw(null), [activeTagId]);

  // --- proposal pool --------------------------------------------------------

  const [growCount, setGrowCount] = useState('200');
  const [growCategory, setGrowCategory] = useState('');
  const growCountValid = Number.isInteger(Number(growCount)) && Number(growCount) > 0;
  const [lastGrow, setLastGrow] = useState<{ requested: number; added: number } | null>(null);
  const growMut = useMutation({
    mutationFn: () => growNewDedupSample(Number(growCount), growCategory || null),
    onMutate: () => setLastGrow(null),
    onSuccess: (res) => {
      pushToast('ok', `Sample grew by ${res.data.added}.`);
      setLastGrow({ requested: Number(growCount), added: res.data.added });
      invalidateOverview();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  // --- proposal review ------------------------------------------------------

  const beginAction = (imageId: number, model: string) =>
    setPendingRowKeys((prev) => new Set(prev).add(rowKey(imageId, model)));
  const endAction = (imageId: number, model: string) =>
    setPendingRowKeys((prev) => {
      const key = rowKey(imageId, model);
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  const clearDraft = (imageId: number, model: string) =>
    setDrafts((prev) => {
      const key = rowKey(imageId, model);
      if (!prev.has(key)) return prev;
      const next = new Map(prev);
      next.delete(key);
      return next;
    });

  const setStateMut = useMutation({
    mutationFn: ({
      imageId,
      model,
      state,
      label,
      excludedReason,
    }: {
      imageId: number;
      model: string;
      state: TagState;
      label?: string;
      excludedReason: TagExcludedReason | null;
    }) => setNewDedupProposalState(imageId, model, state, label, excludedReason),
    onSuccess: (res, vars) => {
      if (res.data.corrected) pushToast('ok', `Set “${res.data.label}” to ${res.data.state}.`);
      clearDraft(vars.imageId, vars.model);
      applyReview([vars.imageId], vars.model, (p) => ({
        ...p,
        status: res.data.status,
        label: res.data.label,
        current_state: res.data.state,
        current_excluded_reason: res.data.excluded_reason,
        reviewed_by: 'operator',
      }));
      patchPositiveTags(vars.imageId, res.data.label, res.data.state);
    },
    onError: (err: Error) => pushToast('err', err.message),
    onSettled: (_data, _err, vars) => endAction(vars.imageId, vars.model),
  });
  const bulkStateMut = useMutation({
    mutationFn: ({
      model,
      state,
      excludedReason,
    }: {
      model: string;
      state: TagState;
      excludedReason: TagExcludedReason | null;
    }) => bulkSetNewDedupProposalState(model, [...selected], state, excludedReason),
    onSuccess: (res) => {
      pushToast('ok', `Set ${res.data.updated} to ${res.data.state}.`);
      setSelected(new Set());
      applyReview(res.data.image_ids, res.data.model, (p) => ({
        ...p,
        status: res.data.state === 'positive' ? 'confirmed' : 'dismissed',
        current_state: res.data.state,
        current_excluded_reason: res.data.excluded_reason,
        reviewed_by: 'operator',
      }));
      // Each row keeps its own proposed label — look it up from the batch
      // that was on screen before this response arrived.
      for (const imageId of res.data.image_ids) {
        const p = proposals.find((row) => row.image_id === imageId && row.model === res.data.model);
        if (p) patchPositiveTags(imageId, p.label, res.data.state);
      }
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  const setTagAnnotationMut = useMutation({
    mutationFn: ({
      imageId,
      state,
      excludedReason,
    }: {
      imageId: number;
      state: TagState;
      excludedReason: TagExcludedReason | null;
    }) => setNewDedupTagAnnotation(activeTagId as number, imageId, state, excludedReason),
    onSuccess: (res, vars) => {
      patchTagImages([vars.imageId], res.data.state, res.data.excluded_reason);
      if (labelFilter) patchPositiveTags(vars.imageId, labelFilter, res.data.state);
      invalidateOverview();
      // The readout's open count just moved by one — the grid itself is still
      // patched in place, never invalidated.
      invalidateCandidates();
    },
    onError: (err: Error) => pushToast('err', err.message),
    onSettled: (_d, _e, vars) => endAction(vars.imageId, 'sample'),
  });
  const bulkTagAnnotationMut = useMutation({
    mutationFn: ({
      state,
      excludedReason,
    }: {
      state: TagState;
      excludedReason: TagExcludedReason | null;
    }) => bulkSetNewDedupTagAnnotation(activeTagId as number, [...selected], state, excludedReason),
    onSuccess: (res) => {
      pushToast('ok', `Set ${res.data.updated} to ${res.data.state}.`);
      setSelected(new Set());
      patchTagImages(res.data.image_ids, res.data.state, res.data.excluded_reason);
      if (labelFilter) {
        for (const imageId of res.data.image_ids) patchPositiveTags(imageId, labelFilter, res.data.state);
      }
      invalidateOverview();
      invalidateCandidates();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const draftFor = (p: NewDedupLabelProposal) => drafts.get(draftKey(p)) ?? p.label;
  const isCorrected = (p: NewDedupLabelProposal) => {
    const d = draftFor(p);
    return d.trim() !== '' && d !== p.label;
  };
  const setDraft = (p: NewDedupLabelProposal, label: string) => {
    setDrafts((prev) => {
      const next = new Map(prev);
      next.set(draftKey(p), label);
      return next;
    });
    if (label.trim() !== '' && label !== p.label) {
      setSelected((prev) => {
        if (!prev.has(p.image_id)) return prev;
        const next = new Set(prev);
        next.delete(p.image_id);
        return next;
      });
    }
  };

  // Only PENDING rows under the CURRENT secondary model batch together — an
  // older model's leftover pending rows still review one at a time via the
  // per-tile control. Corrected tiles are excluded from the batch for the
  // reason above.
  const selectableIds = useMemo(
    () =>
      mode === 'proposals'
        ? secondaryModel
          ? proposals
              .filter((p) => p.status === 'pending' && p.model === secondaryModel && !isCorrected(p))
              .map((p) => p.image_id)
          : []
        : tagImages.map((r) => r.image_id),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mode, proposals, tagImages, secondaryModel, drafts],
  );
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));

  const proposalKeyboard = useGridKeyboardReview(proposals.length, (i, state, excludedReason) => {
    const p = proposals[i];
    if (!p || p.status !== 'pending') return;
    beginAction(p.image_id, p.model);
    setStateMut.mutate({
      imageId: p.image_id,
      model: p.model,
      state,
      label: isCorrected(p) ? draftFor(p) : undefined,
      excludedReason,
    });
  });
  const candidateKeyboard = useGridKeyboardReview(tagImages.length, (i, state, excludedReason) => {
    const r = tagImages[i];
    if (!r || activeTagId == null) return;
    beginAction(r.image_id, 'sample');
    setTagAnnotationMut.mutate({ imageId: r.image_id, state, excludedReason });
  });
  const keyboard = mode === 'proposals' ? proposalKeyboard : candidateKeyboard;

  return (
    <div className="px-6 py-12 max-w-5xl mx-auto">
      <h1
        className="text-[1.6rem] leading-tight"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        NEW DEDUP · Labeling
      </h1>
      <p className="mt-3 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-2xl">
        Build the tag annotation matrix every per-tag classifier head trains from: each image ×
        tag cell is positive, negative, or excluded (dropped from that head's training set
        entirely). An untouched cell is untouched — nobody reviewed it, and it is never
        trained as a negative; being queued as a candidate is not a label either. "Border
        case" is a separate,
        whole-image flag — a photo unclear even to a human — independent of any tag's
        state. Gate 1 needs {gate1Target} positive images per active tag, counting only the ones
        that are NOT parked as a border case. Excluding an image asks why: ambiguous (nobody
        could decide) or pruned (deliberately out of the training set). Only ambiguous counts
        toward a tag's ambiguity rate — above {pctLabel(ambiguityThreshold)}, go fix the tag's
        definition instead of labeling more.
      </p>

      {overviewQ.error && <ErrorBanner message={(overviewQ.error as Error).message} />}

      <TaxonomyBarChart
        tags={visibleTags}
        totalTags={allTags.length}
        candidateImageCount={overviewQ.data?.data.candidate_image_count}
        loading={!overviewQ.data && !overviewQ.error}
        gate1Target={gate1Target}
        proposalTarget={proposalTarget}
        ambiguityThreshold={ambiguityThreshold}
        ambiguityMinDecisions={ambiguityMinDecisions}
        activeLabel={labelFilter}
        maxTrained={maxTrained}
        onMaxTrainedChange={setMaxTrained}
        onFilter={(label) => setLabelFilter((cur) => (cur === label ? null : label))}
        onOpenManage={() => setManageOpen(true)}
      />

      {manageOpen && (
        <TaxonomyManageModal
          labels={allTags}
          onClose={() => setManageOpen(false)}
          newLabelText={newLabelText}
          onNewLabelTextChange={setNewLabelText}
          onAdd={() => addLabelMut.mutate()}
          addPending={addLabelMut.isPending}
          onRename={(id, oldLabel, label) => renameLabelMut.mutate({ id, oldLabel, label })}
          renamePending={renameLabelMut.isPending}
          onRemove={(id, oldLabel) => removeLabelMut.mutate({ id, oldLabel })}
          removePending={removeLabelMut.isPending}
          onSetFlags={(id, flags) => setFlagsMut.mutate({ id, flags })}
          flagsPending={setFlagsMut.isPending}
        />
      )}

      <CandidateQueuePanel
        tagSelected={activeTagId != null}
        summary={activeTagId == null ? undefined : candidates}
        loading={activeTagId != null && candidatesQ.isLoading}
        error={activeTagId == null ? null : (candidatesQ.error as Error | null)}
        count={drawCount}
        onCountChange={setDrawCount}
        countValid={drawCountValid}
        category={drawCategory}
        onCategoryChange={setDrawCategory}
        onDraw={() => drawMut.mutate()}
        drawing={drawMut.isPending}
        lastDraw={lastDraw}
      />

      <section className="mt-8">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-1">
            <ToggleButton active={mode === 'proposals'} onClick={() => setMode('proposals')}>
              Proposals
            </ToggleButton>
            <ToggleButton active={mode === 'candidates'} onClick={() => setMode('candidates')}>
              Candidates
            </ToggleButton>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {mode === 'proposals' && (
              <div className="flex items-center gap-1">
                <ToggleButton active={!showOriginal} onClick={() => setShowOriginal(false)}>
                  New tag
                </ToggleButton>
                <ToggleButton active={showOriginal} onClick={() => setShowOriginal(true)}>
                  Original tag
                </ToggleButton>
              </div>
            )}
            <ImageSizeToggle
              large={imageLarge.value}
              onChange={imageLarge.set}
              label="Review grid image size"
              smallTitle="Smaller photos, more per screen"
              largeTitle="Bigger photos — judge a tag without opening it"
            />
          </div>
        </div>

        <div className="mt-3 flex items-center gap-2 flex-wrap text-xs">
          <label htmlFor="labeling-tag-filter" className="text-[var(--color-ink-3)]">
            Tag
          </label>
          {usingOriginalTagFilter ? (
            // The production CLIP tagger's own fixed vocabulary — a
            // completely different set from Taxonomy v1 below, so it gets
            // its own option list rather than reusing filterOptions.
            <select
              id="labeling-tag-filter"
              value={originalTagFilter ?? ''}
              onChange={(e) => setOriginalTagFilter(e.target.value || null)}
              className="px-2 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)] max-w-[18rem]"
            >
              <option value="">All original tags</option>
              {originalTagOptions.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          ) : (
            <select
              id="labeling-tag-filter"
              value={labelFilter ?? ''}
              onChange={(e) => setLabelFilter(e.target.value || null)}
              className="px-2 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)] max-w-[18rem]"
            >
              <option value="">{mode === 'candidates' ? 'Choose a tag…' : 'All tags'}</option>
              {labelFilter && !filterOptions.some((t) => t.label === labelFilter) && (
                <option value={labelFilter}>{labelFilter}</option>
              )}
              {filterOptions.map((t) => (
                <option key={t.id} value={t.label}>
                  {t.label} ({t.gate_count})
                </option>
              ))}
            </select>
          )}
          {usingOriginalTagFilter
            ? originalTagFilter && (
                <button
                  type="button"
                  onClick={() => setOriginalTagFilter(null)}
                  className="underline decoration-dotted underline-offset-2 text-[var(--color-ink-3)] hover:text-[var(--color-copper-2)]"
                >
                  clear
                </button>
              )
            : labelFilter && (
                <button
                  type="button"
                  onClick={() => setLabelFilter(null)}
                  className="underline decoration-dotted underline-offset-2 text-[var(--color-ink-3)] hover:text-[var(--color-copper-2)]"
                >
                  clear
                </button>
              )}
          {!usingOriginalTagFilter && maxTrainedNum != null && (
            <span className="text-[var(--color-ink-4)]">
              {filterOptions.length} of {allTags.length} tags (≤ {maxTrainedNum} training images)
            </span>
          )}
          {mode === 'candidates' && (
            <>
              <span className="h-4 w-px bg-[var(--color-rule)]" aria-hidden />
              <label htmlFor="labeling-candidate-state" className="text-[var(--color-ink-3)]">
                State
              </label>
              <select
                id="labeling-candidate-state"
                value={candidateState}
                onChange={(e) => setCandidateState(e.target.value as CandidateStateFilter)}
                className="px-2 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)]"
              >
                {CANDIDATE_STATE_OPTIONS.map((o) => (
                  <option key={o.key} value={o.key}>
                    {o.label}
                  </option>
                ))}
              </select>
            </>
          )}
        </div>

        {mode === 'proposals' && (
          <div className="mt-3">
            <Tabs tabs={STATUS_TABS} active={tab} onChange={setTab} />
          </div>
        )}

        {selectableIds.length > 0 && (
          <div className="mt-3 flex items-center gap-3 flex-wrap">
            <button
              type="button"
              onClick={() => setSelected(allSelected ? new Set() : new Set(selectableIds))}
              className="px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]"
            >
              {allSelected ? 'Deselect all' : 'Select all'}
            </button>
            <span className="text-xs text-[var(--color-ink-3)]">{selected.size} selected</span>
            {BATCH_ACTIONS.map((a) => (
              <button
                key={a.key}
                type="button"
                disabled={
                  selected.size === 0 ||
                  bulkStateMut.isPending ||
                  bulkTagAnnotationMut.isPending ||
                  (mode === 'proposals' && !secondaryModel)
                }
                onClick={() =>
                  mode === 'proposals'
                    ? secondaryModel &&
                      bulkStateMut.mutate({
                        model: secondaryModel,
                        state: a.state,
                        excludedReason: a.reason,
                      })
                    : bulkTagAnnotationMut.mutate({ state: a.state, excludedReason: a.reason })
                }
                title={a.reason ? EXCLUDED_REASON_META[a.reason].title : undefined}
                className={[
                  'px-2.5 py-1 text-xs rounded-[var(--radius-xs)] disabled:opacity-40',
                  STATE_META[a.state].activeClass,
                  // The two exclusion buttons share a colour because they share
                  // a state; the lighter one is the one that carries no
                  // diagnostic weight.
                  a.reason === 'pruned' ? 'opacity-80' : '',
                ].join(' ')}
              >
                Set selected: {a.label}
              </button>
            ))}
          </div>
        )}

        <p className="mt-2 text-[0.68rem] text-[var(--color-ink-4)]">
          Keyboard: click a tile to focus it, then ← → (or j/k) to move, 1/2/3 to set
          positive/negative/excluded (ambiguous), 4 for excluded (pruned), and advance to the
          next.
        </p>

        {mode === 'proposals' ? (
          <>
            {/* The pool the secondary-CLIP scores to make the suggestions below
              * — a different pool from the per-tag candidate queues above, and
              * it lives here because this is the only mode it feeds. */}
            <section className="mt-4 border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-4">
              <span className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] mb-3">
                Proposal pool
              </span>
              <div className="flex items-center gap-3 flex-wrap text-sm">
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={growCount}
                  onChange={(e) => setGrowCount(e.target.value)}
                  disabled={growMut.isPending}
                  aria-label="Images to add to the proposal pool"
                  className="w-20 px-2 py-1 font-mono text-sm text-right rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)] disabled:opacity-50"
                />
                <select
                  value={growCategory}
                  onChange={(e) => setGrowCategory(e.target.value)}
                  disabled={growMut.isPending}
                  aria-label="Property type for the proposal pool"
                  className="px-2 py-1 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)] disabled:opacity-50"
                >
                  {CATEGORY_MAIN_TABS.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.label}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => growMut.mutate()}
                  disabled={growMut.isPending || !growCountValid}
                  className="flex items-center gap-1.5 px-3 py-1 text-xs rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-50"
                >
                  {growMut.isPending && <Spinner size={10} />}
                  {growMut.isPending ? 'Growing…' : 'Grow sample'}
                </button>
              </div>

              {lastGrow && (
                <p className="mt-2.5 text-xs text-[var(--color-sage)]">
                  {lastGrow.added > 0
                    ? `Added ${lastGrow.added} image${lastGrow.added === 1 ? '' : 's'} — dispatch the relabel workflow below to generate proposals for them.`
                    : `No new images matched (requested ${lastGrow.requested}) — everything eligible is already in the sample.`}
                </p>
              )}

              <p className="mt-2 text-xs text-[var(--color-ink-4)] leading-relaxed">
                Adds newest not-yet-sampled images to the pool. Scoring runs separately via the
                "NEW DEDUP — Labeling secondary-CLIP proposals" GitHub Actions workflow (model:{' '}
                {secondaryModel ?? '…'}).
              </p>
            </section>

            {proposalsQ.error && <ErrorBanner message={(proposalsQ.error as Error).message} />}
            {!proposalsQ.data && !proposalsQ.error && (
              <p className="mt-6 text-sm text-[var(--color-ink-3)]">Loading proposals…</p>
            )}
            {proposalsQ.data && proposals.length === 0 && (
              <p className="mt-6 text-sm text-[var(--color-ink-3)]">
                {`No ${tab === 'all' ? '' : `${tab} `}proposals.`}
              </p>
            )}

            <div
              className="mt-4 grid gap-3 grid-cols-[repeat(auto-fill,minmax(min(var(--tile-min),100%),1fr))]"
              style={{ '--tile-min': imageLarge.value ? TILE_MIN.lg : TILE_MIN.sm } as CSSProperties}
              onKeyDown={keyboard.onKeyDown}
              tabIndex={0}
            >
              {proposals.map((p, i) => (
                <ProposalTile
                  key={`${p.image_id}:${p.model}`}
                  proposal={p}
                  image={imageCache.get(p.image_id)}
                  showOriginal={showOriginal}
                  selectable={p.status === 'pending' && p.model === secondaryModel && !isCorrected(p)}
                  selected={selected.has(p.image_id)}
                  onToggleSelect={() => toggle(p.image_id)}
                  labelOptions={labelOptions}
                  borderCases={borderCases}
                  assignedTags={positiveTagsCache.get(p.image_id) ?? []}
                  draft={draftFor(p)}
                  onDraftChange={(label) => setDraft(p, label)}
                  corrected={isCorrected(p)}
                  dimmed={tab === 'all' && p.status !== 'pending'}
                  focused={keyboard.focused === i}
                  onFocusTile={() => keyboard.setFocused(i)}
                  onOpen={() => {
                    keyboard.setFocused(i);
                    setLightboxAt(galleryIndex.get(draftKey(p)) ?? null);
                  }}
                  onOpenDetail={() => setDetailImageId(p.image_id)}
                  onSetState={(state, excludedReason) => {
                    beginAction(p.image_id, p.model);
                    setStateMut.mutate({
                      imageId: p.image_id,
                      model: p.model,
                      state,
                      label: isCorrected(p) ? draftFor(p) : undefined,
                      excludedReason: excludedReason ?? null,
                    });
                  }}
                  actionPending={pendingRowKeys.has(rowKey(p.image_id, p.model))}
                />
              ))}
            </div>
          </>
        ) : (
          <>
            {activeTagId == null && (
              <p className="mt-6 text-sm text-[var(--color-ink-3)]">
                Choose a tag above to browse its candidates.
              </p>
            )}
            {activeTagId != null && tagImagesQ.error && (
              <ErrorBanner message={(tagImagesQ.error as Error).message} />
            )}
            {activeTagId != null && !tagImagesQ.data && !tagImagesQ.error && (
              <p className="mt-6 text-sm text-[var(--color-ink-3)]">Loading candidates…</p>
            )}
            {activeTagId != null && tagImagesQ.data && tagImages.length === 0 && (
              <p className="mt-6 text-sm text-[var(--color-ink-3)]">
                {/* "No candidates yet" is only true when nothing is being
                  * filtered OUT — under a positive/negative/excluded filter the
                  * queue can be full and still show nothing here. */}
                {candidateState === 'all' || candidateState === 'untouched'
                  ? 'No candidates yet — draw some above.'
                  : 'No candidates in that state.'}
              </p>
            )}
            {activeTagId != null && (
              <div
                className="mt-4 grid gap-3 grid-cols-[repeat(auto-fill,minmax(min(var(--tile-min),100%),1fr))]"
                style={{ '--tile-min': imageLarge.value ? TILE_MIN.lg : TILE_MIN.sm } as CSSProperties}
                onKeyDown={keyboard.onKeyDown}
                tabIndex={0}
              >
                {tagImages.map((r, i) => (
                  <TagImageTile
                    key={r.image_id}
                    row={r}
                    image={imageCache.get(r.image_id)}
                    selected={selected.has(r.image_id)}
                    onToggleSelect={() => toggle(r.image_id)}
                    borderCases={borderCases}
                    assignedTags={positiveTagsCache.get(r.image_id) ?? []}
                    focused={keyboard.focused === i}
                    onOpen={() => {
                      keyboard.setFocused(i);
                      setLightboxAt(galleryIndex.get(String(r.image_id)) ?? null);
                    }}
                    onOpenDetail={() => setDetailImageId(r.image_id)}
                    onSetState={(state, excludedReason) => {
                      beginAction(r.image_id, 'sample');
                      setTagAnnotationMut.mutate({
                        imageId: r.image_id,
                        state,
                        excludedReason: excludedReason ?? null,
                      });
                    }}
                    actionPending={pendingRowKeys.has(rowKey(r.image_id, 'sample'))}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </section>

      {lightboxAt != null && (
        <ImageLightbox
          images={galleryImages}
          startIndex={lightboxAt}
          onClose={() => setLightboxAt(null)}
          tagAt={
            showOriginal || mode === 'candidates'
              ? undefined
              : (i) => ({ tag: gallery[i]?.tag ?? null, confidence: gallery[i]?.confidence ?? null })
          }
        />
      )}

      {detailImageId != null && (
        <ImageTagDetailPanel
          imageId={detailImageId}
          onClose={() => setDetailImageId(null)}
          onTagStateChange={(c) => patchPositiveTags(detailImageId, c.label, c.state)}
        />
      )}
    </div>
  );
}

function ToggleButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={[
        'px-2.5 py-1 rounded-[var(--radius-sm)] border text-[0.78rem] transition-colors',
        active
          ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
          : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
      ].join(' ')}
    >
      {children}
    </button>
  );
}

/* A run of quiet metadata chips, `a 12 · b 7 · c 3`, each carrying its own
 * explanation. The same `·`-separated idiom the taxonomy strip already uses for
 * a tag's counts, so the composition readout needs no new vocabulary. */
function ChipRun({
  items,
  label,
}: {
  items: ReadonlyArray<{ key: string; text: string; title: string }>;
  label: string;
}) {
  return (
    <span role="list" aria-label={label} className="flex items-baseline gap-1.5 flex-wrap">
      {items.map((it, i) => (
        <span key={it.key} role="listitem" title={it.title} className="whitespace-nowrap">
          {i > 0 ? '· ' : ''}
          {it.text}
        </span>
      ))}
    </span>
  );
}

/* ONE tag's review queue: how much work is on it, how it was drawn, and the way
 * to get more. A work-queue readout, not a retrieval console — nothing here
 * tunes the retrieval, and nothing here is a judgement about an image.
 *
 * The load-bearing constraint is what this panel must NOT imply. A candidate is
 * an image somebody should LOOK at for this tag: not a positive, not a
 * negative, not a default. So every number is a count of WORK (open / drawn),
 * never of evidence; the band and category chips describe the DRAW, never the
 * images; and the helper line says so outright, because this is the screen
 * where the opposite belief would be born.
 *
 * The composition chips are also the only place the corpus skew is visible: the
 * labeled set is 83.8% byt against a 43.9% corpus, and the draw is stratified
 * to dilute that. Printing the buckets is what makes the correction auditable
 * instead of a claim. */
function CandidateQueuePanel({
  tagSelected,
  summary,
  loading,
  error,
  count,
  onCountChange,
  countValid,
  category,
  onCategoryChange,
  onDraw,
  drawing,
  lastDraw,
}: {
  tagSelected: boolean;
  summary: NewDedupCandidateSummary | undefined;
  loading: boolean;
  error: Error | null;
  count: string;
  onCountChange: (next: string) => void;
  countValid: boolean;
  category: string;
  onCategoryChange: (next: string) => void;
  onDraw: () => void;
  drawing: boolean;
  lastDraw: NewDedupCandidateDrawResult | null;
}) {
  /* A tag with too few human-verified positives has no meaningful centroid, and
   * the floor is the SERVER's — rendered, never recomputed here, the same rule
   * the ambiguity threshold follows. */
  const canDraw = summary?.can_draw ?? false;
  const shortfall = lastDraw != null && lastDraw.status === 'drawn' && lastDraw.inserted < lastDraw.requested;
  const degraded = lastDraw?.categories.filter((c) => c.status !== 'drawn') ?? [];
  const randomBand = summary?.by_draw.find((b) => b.key === 'random');

  return (
    <section className="mt-8 border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-4">
      <span className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] mb-3">
        Candidates
      </span>

      {!tagSelected && (
        <p className="text-sm text-[var(--color-ink-3)]">
          Choose a tag to see its candidate queue.
        </p>
      )}

      {tagSelected && error && <ErrorBanner message={error.message} />}
      {tagSelected && !error && loading && (
        <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>
      )}

      {tagSelected && summary && (
        <>
          {/* One text node on purpose: the hierarchy here is line-to-line (this
            * line against the 0.68rem chips below), not word-to-word, and a
            * status line broken into styled fragments is a status line nothing
            * can read as a whole. */}
          <p className="text-sm text-[var(--color-ink)]">
            {`${summary.open} open · ${summary.total} drawn · ${
              summary.last_drawn_at ? `last drawn ${fmtRelative(summary.last_drawn_at)}` : 'never drawn'
            }`}
          </p>

          {summary.routing_categories.length > 0 && (
            // A scoped draw is NARROWER on purpose, and silence here is what made
            // the first live koupelna draw read as a broken retrieval rather than a
            // deliberate one. Neutral, not alarm: this is the tag working correctly.
            <p className="mt-1 text-xs text-[var(--color-ink-3)]">
              {`Draws only from ${summary.routing_categories.join(', ')} — the property types this tag serves.`}
            </p>
          )}

          {(summary.by_draw.length > 0 || summary.by_category.length > 0) && (
            <div className="mt-2 flex items-center gap-2 flex-wrap text-[0.68rem] text-[var(--color-ink-4)]">
              <ChipRun
                label="Candidates by rank band"
                items={summary.by_draw.map((b) => ({
                  key: b.key,
                  text: `${drawLabel(b.key)} ${b.total}`,
                  title: `${drawTitle(b.key)} ${b.open} of ${b.total} still undecided${yieldPhrase(b)}.`,
                }))}
              />
              {summary.by_draw.length > 0 && summary.by_category.length > 0 && (
                <span className="h-3 w-px bg-[var(--color-rule)]" aria-hidden />
              )}
              <ChipRun
                label="Candidates by property type"
                items={summary.by_category.map((b) => ({
                  key: b.key,
                  text: `${b.key} ${b.total}`,
                  title: `${b.total} candidates drawn under the ${b.key} quota, ${b.open} still undecided${yieldPhrase(b)}. Draws are stratified by property type so the labeled set's byt skew is diluted rather than inherited.`,
                }))}
              />
            </div>
          )}

          {randomBand != null && randomBand.positive > 0 && (
            // The one self-check the retrieval has, and the only place it can be
            // read. Neutral, not alarm: a centroid missing a mode is information
            // the random band exists to produce, not a fault to flag.
            <p className="mt-2 text-xs text-[var(--color-ink-3)]">
              {`Random band: ${randomBand.positive} positive of ${randomBand.positive + randomBand.negative} decided. Sustained positives from an unranked sample mean the centroid is missing a mode of this tag.`}
            </p>
          )}

          <div className="mt-3 flex items-center gap-3 flex-wrap text-sm">
            <input
              type="number"
              min={1}
              step={1}
              value={count}
              onChange={(e) => onCountChange(e.target.value)}
              disabled={drawing}
              aria-label="Candidates to draw"
              className="w-20 px-2 py-1 font-mono text-sm text-right rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)] disabled:opacity-50"
            />
            <select
              value={category}
              onChange={(e) => onCategoryChange(e.target.value)}
              disabled={drawing}
              aria-label="Property type for this draw"
              className="px-2 py-1 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)] disabled:opacity-50"
            >
              {CATEGORY_MAIN_TABS.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={onDraw}
              disabled={drawing || !countValid || !canDraw}
              className="flex items-center gap-1.5 px-3 py-1 text-xs rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-50"
            >
              {drawing && <Spinner size={10} />}
              {drawing ? 'Drawing…' : 'Draw candidates'}
            </button>
          </div>

          {!canDraw && (
            // Brick, the page's alarm hue: this is a blocked state with a named
            // way out, not a failure. A centroid over fewer positives than were
            // ever measured is one operator's idiosyncrasies, and a garbage
            // pool costs a whole review sitting.
            <p className="mt-2.5 text-xs text-[var(--color-brick)]">
              Needs {summary.min_verified_positives} verified positive images to build a centroid
              — this tag has {summary.verified_positive_count}. Label more positives first.
            </p>
          )}

          {lastDraw && lastDraw.status === 'drawn' && (
            <p className="mt-2.5 text-xs text-[var(--color-sage)]">
              {`Drew ${lastDraw.inserted} candidate${lastDraw.inserted === 1 ? '' : 's'} — ${lastDraw.dropped_near_dup} near-duplicate${lastDraw.dropped_near_dup === 1 ? '' : 's'} and ${lastDraw.dropped_property_cap} over the per-property cap dropped.`}
              {shortfall && (
                // Neutral, not alarm: a short draw is the honest outcome of a
                // thin pool, and the numbers say which loss caused it.
                <span className="text-[var(--color-ink-3)]">
                  {` Asked for ${lastDraw.requested}, short by ${lastDraw.requested - lastDraw.inserted} — bands are never back-filled from each other, so a thin band shows up here instead of being quietly topped up.`}
                </span>
              )}
              {degraded.length > 0 && (
                // This one IS alarm: a timed-out or skipped category means a
                // quota nobody filled, which a smaller total would otherwise
                // hide.
                <span className="text-[var(--color-brick)]">
                  {` Did not complete: ${degraded.map((c) => `${c.category_main} (${c.status})`).join(', ')}.`}
                </span>
              )}
            </p>
          )}

          {lastDraw && lastDraw.status === 'insufficient_positives' && (
            <p className="mt-2.5 text-xs text-[var(--color-brick)]">
              Nothing drawn — no pool is built at all below the floor, rather than a garbage one.
            </p>
          )}
        </>
      )}

      <p className="mt-2 text-xs text-[var(--color-ink-4)] leading-relaxed">
        Candidates are images to LOOK at for this tag, found by ranking a bounded,
        category-stratified pool against a centroid of this tag's human-verified positives.
        Membership is not a label — an image nobody has reviewed is never trained as a negative.
      </p>
    </section>
  );
}

/* Who decided this tag's set. Only the non-zero parts render — the same
 * conditional-chip idiom the counts beside it already use, so a tag whose set
 * is entirely operator work stays a single quiet number.
 *
 * The positive/negative/excluded counts on this row deliberately still INCLUDE
 * migration 442's manufactured rows; this chip is what makes that legible
 * rather than silently wrong. Backfill is drawn in brick — the same alarm
 * colour "parked" uses on this strip for a number that is not what it looks
 * like. */
function ProvenanceChip({ tag }: { tag: NewDedupTag }) {
  return (
    <>
      {tag.human_count > 0 && (
        <span
          className="shrink-0 text-[0.68rem] text-[var(--color-ink-4)]"
          title={`${tag.human_count} of this tag's annotations were decided by a person, or proposed by a machine and confirmed by one`}
        >
          · {tag.human_count} human
        </span>
      )}
      {tag.machine_count > 0 && (
        <span
          className="shrink-0 text-[0.68rem] text-[var(--color-ink-4)]"
          title={`${tag.machine_count} were decided by a machine that NOBODY has checked — not ground truth`}
        >
          · {tag.machine_count} machine
        </span>
      )}
      {tag.backfill_count > 0 && (
        <span
          className="shrink-0 text-[0.68rem] text-[var(--color-brick)]"
          title="manufactured by migration 442's one-hot backfill — not a decision anybody made; awaiting deletion"
        >
          · {tag.backfill_count} backfill
        </span>
      )}
    </>
  );
}

/* The "go fix the DEFINITION" signal. Encoded in FORM, not only in a number:
 * over threshold it becomes a LINK — underlined, weighted, with a trailing
 * arrow into that tag's definition workbench — while under threshold it is
 * plain muted text with no affordance at all. Strip the colour and the two are
 * still distinguishable.
 *
 * `ambiguity_alert` is computed server-side against the same threshold this
 * renders, and is never recomputed here: one definition, one place.
 *
 * A null rate renders NOTHING rather than 0% — a tag with no decisions is
 * unknown, not healthy, and its empty bar directly below already says so. */
function AmbiguityChip({
  tag,
  threshold,
  minDecisions,
}: {
  tag: NewDedupTag;
  threshold: number | undefined;
  minDecisions: number | undefined;
}) {
  if (tag.ambiguity_rate == null) return null;
  const pct = Math.round(tag.ambiguity_rate * 100);
  const belowFloor = minDecisions != null && tag.decided_count < minDecisions;
  /* Both halves come from the SAME population the rate was computed from —
   * ambiguous_count is the whole inventory (machine and backfill rows included)
   * and pairing it with decided_count would state a fraction nobody computed. */
  const head = `${tag.ambiguous_decided_count} of ${tag.decided_count} human decision${tag.decided_count === 1 ? '' : 's'} on this tag were left ambiguous.`;
  const tail = ' (Pruned exclusions are not counted, in the rate or its denominator.)';
  const title = tag.ambiguity_alert
    ? `${head} Above ${pctLabel(threshold)} the tag's DEFINITION is the problem, not the labeling — click to open it.${tail}`
    : belowFloor
      ? `${head} Too few decisions to call yet — the ${pctLabel(threshold)} alert needs at least ${minDecisions}.${tail}`
      : `${head} Under the ${pctLabel(threshold)} mark.${tail}`;
  const text = `· ${pct}% ambiguous`;

  if (!tag.ambiguity_alert) {
    return (
      <span className="shrink-0 text-[0.68rem] text-[var(--color-ink-4)]" title={title}>
        {text}
      </span>
    );
  }
  return (
    <Link
      to={withQuery(ROUTES.newDedupTaxonomy.build(), { tag: tag.id })}
      title={title}
      aria-label={`Fix the definition for ${tag.label} — ${pct}% of its decisions are ambiguous`}
      className="shrink-0 text-[0.68rem] font-medium text-[var(--color-brick)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-copper-2)]"
    >
      {text} →
    </Link>
  );
}

/* Ranked horizontal bar chart, most positive images first. Collapsible
 * (persisted) because with a full taxonomy the chart pushes the review grid
 * off screen. */
function TaxonomyBarChart({
  tags,
  totalTags,
  candidateImageCount,
  loading,
  gate1Target,
  proposalTarget,
  ambiguityThreshold,
  ambiguityMinDecisions,
  activeLabel,
  maxTrained,
  onMaxTrainedChange,
  onFilter,
  onOpenManage,
}: {
  tags: NewDedupTag[];
  totalTags: number;
  /* Distinct images queued for at least ONE tag. A different quantity from the
   * old shared-pool size, over a different denominator — the header says
   * "queued", never "sampled". */
  candidateImageCount: number | undefined;
  loading: boolean;
  gate1Target: number;
  proposalTarget: number;
  /* Server-owned, so they arrive with the payload and are undefined on the
   * first paint — never defaulted to a literal here. */
  ambiguityThreshold: number | undefined;
  ambiguityMinDecisions: number | undefined;
  activeLabel: string | null;
  maxTrained: string;
  onMaxTrainedChange: (next: string) => void;
  onFilter: (label: string) => void;
  onOpenManage: () => void;
}) {
  const [open, toggle] = useCollapsed('new-dedup-labeling-taxonomy', true);
  const sorted = useMemo(() => [...tags].sort((a, b) => b.gate_count - a.gate_count), [tags]);
  const domainMax = Math.max(gate1Target, ...sorted.map((t) => t.gate_count), 1);
  const gatePct = Math.min(100, (gate1Target / domainMax) * 100);
  const filtered = tags.length !== totalTags;

  return (
    <section className="mt-8">
      <div className="flex items-center justify-between mb-3 gap-3">
        <button
          type="button"
          onClick={toggle}
          aria-expanded={open}
          className="group flex min-w-0 items-center gap-2 text-left"
        >
          <Chevron open={open} />
          <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] group-hover:text-[var(--color-ink-2)] transition-colors">
            Taxonomy v1 ({filtered ? `${tags.length} of ${totalTags}` : totalTags} tags
            {candidateImageCount != null ? `, ${candidateImageCount} images queued` : ''})
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-2">
          <label
            className="flex items-center gap-1.5 text-xs text-[var(--color-ink-3)]"
            title="Show only tags that have at most this many Gate-1 images (border cases excluded) — the ones still short. Also narrows the tag filter below."
          >
            ≤
            <input
              type="number"
              min={0}
              step={1}
              value={maxTrained}
              onChange={(e) => onMaxTrainedChange(e.target.value)}
              placeholder="any"
              aria-label="Max training images per tag"
              className="w-16 px-1.5 py-1 font-mono text-xs text-right rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)]"
            />
            imgs
          </label>
          <button
            type="button"
            onClick={onOpenManage}
            className="px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] transition-colors"
          >
            Modify labels
          </button>
        </div>
      </div>

      {open && (
        <>
          {loading && <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>}
          {!loading && sorted.length === 0 && (
            <p className="text-sm text-[var(--color-ink-3)]">
              {filtered
                ? 'No tag is under that many training images.'
                : 'No tags yet — add the first one via "Modify labels".'}
            </p>
          )}

          {sorted.length > 0 && (
            <>
              <p className="text-[0.7rem] text-[var(--color-ink-4)]">
                Bars are positive images that count toward Gate 1 — border cases excluded —
                scaled to its target of {gate1Target} (marked ▏below).
              </p>
              <div className="mt-3 space-y-2">
                {sorted.map((t) => {
                  const pct = Math.min(100, (t.gate_count / domainMax) * 100);
                  const active = activeLabel === t.label;
                  return (
                    <div key={t.id}>
                      <div className="flex items-baseline gap-1.5 min-w-0 flex-wrap">
                        <button
                          type="button"
                          onClick={() => onFilter(t.label)}
                          title="Filter to this tag"
                          className={[
                            'min-w-0 truncate font-mono text-[0.76rem] hover:text-[var(--color-copper-2)]',
                            active
                              ? 'text-[var(--color-copper)]'
                              : t.priority
                                ? 'text-[var(--color-brick)]'
                                : 'text-[var(--color-ink-2)]',
                          ].join(' ')}
                        >
                          {t.label}
                        </button>
                        {t.pending_count > 0 && (
                          <span className="shrink-0 text-[0.68rem] text-[var(--color-ink-4)]">
                            · {t.pending_count}/{proposalTarget} pending
                          </span>
                        )}
                        {(t.negative_count > 0 || t.excluded_count > 0) && (
                          <span className="shrink-0 text-[0.68rem] text-[var(--color-ink-4)]">
                            · {t.negative_count} neg · {t.excluded_count} excl
                          </span>
                        )}
                        {t.border_case_count > 0 && (
                          <span
                            className="shrink-0 text-[0.68rem] text-[var(--color-brick)]"
                            title={`${t.border_case_count} more positive image${t.border_case_count === 1 ? ' is' : 's are'} parked as border cases — not counted toward Gate 1 until the flag is cleared (${t.positive_count} positive in total)`}
                          >
                            · {t.border_case_count} parked
                          </span>
                        )}
                        <ProvenanceChip tag={t} />
                        <AmbiguityChip
                          tag={t}
                          threshold={ambiguityThreshold}
                          minDecisions={ambiguityMinDecisions}
                        />
                      </div>
                      <div className="mt-1 flex items-center gap-2">
                        <div className="relative h-3.5 flex-1 rounded-[var(--radius-xs)] bg-[var(--color-rule-soft)] overflow-hidden">
                          <div
                            className={[
                              'h-full rounded-r-[var(--radius-sm)] transition-[width]',
                              active
                                ? 'bg-[var(--color-copper)]'
                                : t.priority
                                  ? 'bg-[var(--color-brick)]'
                                  : 'bg-[var(--color-sage)]',
                            ].join(' ')}
                            style={{ width: `${pct}%` }}
                            aria-hidden
                          />
                          <div
                            className="absolute top-0 bottom-0 w-px bg-[var(--color-ink)]/40"
                            style={{ left: `calc(${gatePct}% - 1px)` }}
                            aria-hidden
                          />
                        </div>
                        <span className="w-8 shrink-0 text-right font-mono text-[0.7rem] tabular-nums text-[var(--color-ink-3)]">
                          {t.gate_count}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}

/* The tags currently assigned (positive) to one image — "below the image"
 * per the operator's ask, since a tile only shows the ONE tag it's
 * reviewing and that's not the same as everything the image is already
 * positive on now that multi-label images are possible. Renders nothing
 * when empty, to keep an untouched tile clean. */
function AssignedTagsRow({ tags }: { tags: string[] }) {
  if (tags.length === 0) return null;
  return (
    <div
      role="list"
      aria-label="Assigned tags"
      className="px-2 pt-1.5 flex flex-wrap gap-1"
    >
      {tags.map((label) => (
        <span
          key={label}
          role="listitem"
          className="px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-sage-soft)] text-[var(--color-sage)] text-[0.65rem] font-mono truncate max-w-full"
          title={label}
        >
          {label}
        </span>
      ))}
    </div>
  );
}

function ProposalTile({
  proposal,
  image,
  showOriginal,
  selectable,
  selected,
  onToggleSelect,
  labelOptions,
  borderCases,
  assignedTags,
  draft,
  onDraftChange,
  corrected,
  dimmed,
  focused,
  onFocusTile,
  onOpen,
  onOpenDetail,
  onSetState,
  actionPending,
}: {
  proposal: NewDedupLabelProposal;
  image: ImagePublic | undefined;
  showOriginal: boolean;
  selectable: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  labelOptions: LabelOption[];
  borderCases: BorderCaseStore;
  assignedTags: string[];
  draft: string;
  onDraftChange: (label: string) => void;
  corrected: boolean;
  dimmed: boolean;
  focused: boolean;
  onFocusTile: () => void;
  onOpen: () => void;
  onOpenDetail: () => void;
  onSetState: (state: TagState, excludedReason?: TagExcludedReason | null) => void;
  actionPending: boolean;
}) {
  const badgeTag = showOriginal ? (image?.clip_fine_tag ?? null) : proposal.label;
  const badgeConfidence = showOriginal ? (image?.clip_confidence ?? null) : proposal.confidence;

  return (
    <div
      className={[
        'border rounded-[var(--radius-sm)] bg-[var(--color-paper)] transition-opacity',
        focused ? 'border-[var(--color-copper)]' : 'border-[var(--color-rule)]',
        dimmed ? 'opacity-45 hover:opacity-100 focus-within:opacity-100' : '',
      ].join(' ')}
      data-dimmed={dimmed || undefined}
      onMouseEnter={onFocusTile}
    >
      <div className="relative aspect-[4/3] overflow-hidden rounded-t-[var(--radius-sm)] bg-[var(--color-inset)]">
        {selectable && (
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggleSelect}
            className="absolute top-1.5 left-1.5 z-10 h-4 w-4"
            aria-label="Select for batch action"
          />
        )}
        {image && (
          <button
            type="button"
            onClick={onOpen}
            aria-label={`Open photo ${image.id}`}
            className="absolute inset-0 block h-full w-full cursor-zoom-in focus:outline-none focus-visible:border focus-visible:border-[var(--color-copper)]"
          >
            <img src={imageSrc(image)} alt="" loading="lazy" className="h-full w-full object-cover" />
          </button>
        )}
        <ImageTagBadge tag={badgeTag} confidence={badgeConfidence} className="absolute bottom-1.5 left-1.5" />
      </div>

      <AssignedTagsRow tags={assignedTags} />

      {/* The tri-state control only ever decides THIS ONE tag (the proposal's
        * own label, or the correction typed into the picker below) — never
        * every tag on the image. Naming it right above the buttons makes that
        * unambiguous without hunting for the picker underneath. */}
      <p
        className="px-2 pt-1.5 truncate text-[0.65rem] text-[var(--color-ink-3)] font-mono"
        title={`Setting the state of "${draft.trim() || proposal.label}"`}
      >
        {draft.trim() || proposal.label}
      </p>
      <div className="px-2 py-1.5 flex items-center justify-between gap-1.5">
        <TriStateControl
          state={proposal.current_state ?? 'untouched'}
          onChange={onSetState}
          disabled={actionPending}
          focused={focused}
          excludedReason={proposal.current_excluded_reason}
          onChangeReason={(reason) => onSetState('excluded', reason)}
        />
        <button
          type="button"
          onClick={onOpenDetail}
          title="Every tag on this image"
          className="text-[0.65rem] text-[var(--color-ink-3)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-copper-2)]"
        >
          all tags
        </button>
      </div>

      <div className="px-2 pb-2 flex items-center gap-1.5">
        <div className="min-w-0 flex-1">
          <LabelCombobox label={`Tag for image ${proposal.image_id}`} value={draft} onChange={onDraftChange} options={labelOptions} placeholder="tag…" />
        </div>
        <BorderCaseButton imageId={proposal.image_id} store={borderCases} />
      </div>
      {corrected && (
        <p className="px-2 pb-1.5 text-[0.65rem] text-[var(--color-copper)]">
          will decide against “{draft}”, not “{proposal.label}”
        </p>
      )}
    </div>
  );
}

/* Candidates-mode tile: no proposal, no label picker (the tag is already fixed
 * by the page's tag filter) — just the photo, the tri-state control for THAT
 * tag, border case, and the band that put it in front of you. */
function TagImageTile({
  row,
  image,
  selected,
  onToggleSelect,
  borderCases,
  assignedTags,
  focused,
  onOpen,
  onOpenDetail,
  onSetState,
  actionPending,
}: {
  row: NewDedupTagImage;
  image: ImagePublic | undefined;
  selected: boolean;
  onToggleSelect: () => void;
  borderCases: BorderCaseStore;
  assignedTags: string[];
  focused: boolean;
  onOpen: () => void;
  onOpenDetail: () => void;
  onSetState: (state: TagState, excludedReason?: TagExcludedReason | null) => void;
  actionPending: boolean;
}) {
  return (
    <div
      className={[
        'border rounded-[var(--radius-sm)] bg-[var(--color-paper)]',
        focused ? 'border-[var(--color-copper)]' : 'border-[var(--color-rule)]',
      ].join(' ')}
    >
      <div className="relative aspect-[4/3] overflow-hidden rounded-t-[var(--radius-sm)] bg-[var(--color-inset)]">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelect}
          className="absolute top-1.5 left-1.5 z-10 h-4 w-4"
          aria-label="Select for batch action"
        />
        {image && (
          <button
            type="button"
            onClick={onOpen}
            aria-label={`Open photo ${image.id}`}
            className="absolute inset-0 block h-full w-full cursor-zoom-in focus:outline-none focus-visible:border focus-visible:border-[var(--color-copper)]"
          >
            <img src={imageSrc(image)} alt="" loading="lazy" className="h-full w-full object-cover" />
          </button>
        )}
        {image && (
          <ImageTagBadge
            tag={image.clip_fine_tag ?? null}
            confidence={image.clip_confidence ?? null}
            className="absolute bottom-1.5 left-1.5"
          />
        )}
      </div>
      <AssignedTagsRow tags={assignedTags} />
      <div className="px-2 py-1.5 flex items-center justify-between gap-1.5">
        <TriStateControl
          state={row.state}
          onChange={onSetState}
          disabled={actionPending}
          focused={focused}
          excludedReason={row.excluded_reason}
          onChangeReason={(reason) => onSetState('excluded', reason)}
          source={row.source}
        />
        <button
          type="button"
          onClick={onOpenDetail}
          title="Every tag on this image"
          className="text-[0.65rem] text-[var(--color-ink-3)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-copper-2)]"
        >
          all tags
        </button>
      </div>
      <div className="px-2 pb-2 flex items-center justify-between gap-1.5">
        <BorderCaseButton imageId={row.image_id} store={borderCases} />
        {/* WHY this tile is in front of you. Deliberately colourless and quiet:
          * the band is a fact about the retrieval, never a hint about the
          * answer — an operator who starts reading "head" as "probably yes"
          * is exactly the bias the mixed bands exist to prevent. Absent
          * entirely on an image decided before candidates existed. */}
        {row.draw != null && (
          <span
            className="shrink-0 font-mono text-[0.62rem] text-[var(--color-ink-4)]"
            title={`${drawTitle(row.draw)}${row.category_main ? ` Drawn under the ${row.category_main} quota.` : ''} This says why the image was queued, not what it is.`}
          >
            {drawLabel(row.draw)}
            {row.pool_rank != null ? ` #${row.pool_rank}` : ''}
          </span>
        )}
      </div>
    </div>
  );
}
