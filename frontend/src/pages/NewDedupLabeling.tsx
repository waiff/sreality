import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getNewDedupLabelingOverview,
  addNewDedupTag,
  renameNewDedupTag,
  removeNewDedupTag,
  setNewDedupTagFlags,
  growNewDedupSample,
  listNewDedupProposals,
  setNewDedupProposalState,
  bulkSetNewDedupProposalState,
  listNewDedupTagImages,
  setNewDedupTagAnnotation,
  bulkSetNewDedupTagAnnotation,
  listNewDedupImageTags,
  bulkSetNewDedupImageTags,
  listNewDedupPositiveTagsForImages,
  listNewDedupSettings,
  TAG_STATES,
  type TagState,
  type NewDedupTag,
  type NewDedupLabelProposal,
  type NewDedupTagImage,
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
import { usePersistedFlag } from '@/lib/persistedFlag';
import { useBorderCases, type BorderCaseStore } from '@/lib/useBorderCases';
import type { ImagePublic } from '@/lib/types';

const OVERVIEW_KEY = ['new-dedup', 'labeling', 'overview'];
const PROPOSALS_KEY = ['new-dedup', 'labeling', 'proposals'];
const TAG_IMAGES_KEY = ['new-dedup', 'labeling', 'tag-images'];
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
 * each screen only asks about one tag), and a Sample browse that reaches every
 * image in the labeling pool for one tag, including ones the model never
 * proposed it for — the only way to answer "show me every image where kitchen =
 * excluded" for images outside the suggestion queue. */
type Mode = 'proposals' | 'sample';
type SampleStateFilter = TagState | 'untouched' | 'all';
const SAMPLE_STATE_OPTIONS: ReadonlyArray<{ key: SampleStateFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'untouched', label: 'Untouched' },
  { key: 'positive', label: 'Positive' },
  { key: 'negative', label: 'Negative' },
  { key: 'excluded', label: 'Excluded' },
];

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

const STATE_META: Record<
  TagState,
  { label: string; icon: string; activeClass: string; hotkey: string }
> = {
  positive: {
    label: 'Positive — this tag applies',
    icon: '✓',
    activeClass: 'bg-[var(--color-sage)] text-[var(--color-paper)] border-[var(--color-sage)]',
    hotkey: '1',
  },
  negative: {
    label: 'Negative — this tag does not apply',
    icon: '–',
    activeClass: 'bg-[var(--color-ink-3)] text-[var(--color-paper)] border-[var(--color-ink-3)]',
    hotkey: '2',
  },
  excluded: {
    label: 'Excluded — ambiguous, do not train this tag on this image',
    icon: '⊘',
    activeClass: 'bg-[var(--color-copper)] text-[var(--color-paper)] border-[var(--color-copper)]',
    hotkey: '3',
  },
};

/* Three visually distinct states at a glance (colour + icon), plus a fourth
 * "untouched" rendering of the same three buttons — a dashed outline on the
 * negative slot, since untouched defaults to negative — so an explicit
 * decision is never confused with the unreviewed default. One control, never
 * two widgets for "is it positive" and "is it excluded". */
function TriStateControl({
  state,
  onChange,
  disabled,
  focused,
}: {
  state: TagState | 'untouched';
  onChange: (state: TagState) => void;
  disabled?: boolean;
  focused?: boolean;
}) {
  return (
    <div role="group" aria-label="Tag state" className="flex items-center gap-0.5 shrink-0">
      {TAG_STATES.map((s) => {
        const meta = STATE_META[s];
        const active = state === s;
        const implied = state === 'untouched' && s === 'negative';
        return (
          <button
            key={s}
            type="button"
            aria-pressed={active}
            disabled={disabled}
            onClick={() => onChange(s)}
            title={`${meta.label}${implied ? ' (defaulted — not yet reviewed)' : ''} [${meta.hotkey}]`}
            className={[
              'flex h-6 w-6 items-center justify-center rounded-[var(--radius-xs)] border text-[0.8rem] leading-none transition-colors disabled:opacity-40',
              focused ? 'ring-2 ring-[var(--color-copper)] ring-offset-1' : '',
              active
                ? meta.activeClass
                : implied
                  ? 'border-dashed border-[var(--color-ink-3)] text-[var(--color-ink-3)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]',
            ].join(' ')}
          >
            {meta.icon}
          </button>
        );
      })}
    </div>
  );
}

/* Keyboard review for a flat list of `n` tiles: arrow keys / j-k move a
 * focused index, 1/2/3 (or p/n/x) set that tile's state and auto-advance —
 * "assign primary tag, next image" in one keystroke. Attached to the grid
 * container (tabIndex=0), not the window, so it never fights a text input
 * elsewhere on the page (the tag combobox, the sample-size field). */
function useGridKeyboardReview(n: number, onSetState: (index: number, state: TagState) => void) {
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
    const byHotkey = (Object.entries(STATE_META) as Array<[TagState, (typeof STATE_META)[TagState]]>).find(
      ([, meta]) => meta.hotkey === key,
    );
    const byLetter: TagState | null = key === 'p' ? 'positive' : key === 'x' ? 'excluded' : null;
    const state = byHotkey?.[0] ?? byLetter;
    if (state && focused != null) {
      e.preventDefault();
      onSetState(focused, state);
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
  const [sampleState, setSampleState] = useState<SampleStateFilter>('untouched');
  const [showOriginal, setShowOriginal] = useState(false);
  const imageLarge = usePersistedFlag(IMAGE_LARGE_KEY, false);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const [pendingRowKeys, setPendingRowKeys] = useState<ReadonlySet<string>>(new Set());
  const [drafts, setDrafts] = useState<ReadonlyMap<string, string>>(new Map());
  const [detailImageId, setDetailImageId] = useState<number | null>(null);

  const allTags = useMemo(() => overviewQ.data?.data.tags ?? [], [overviewQ.data]);
  const tagByLabel = useMemo(() => new Map(allTags.map((t) => [t.label, t])), [allTags]);
  const activeTagId = labelFilter ? (tagByLabel.get(labelFilter)?.id ?? null) : null;

  const proposalsKey = useMemo(() => [...PROPOSALS_KEY, tab, labelFilter], [tab, labelFilter]);
  const proposalsQ = useQuery({
    queryKey: proposalsKey,
    queryFn: () =>
      listNewDedupProposals({ status: tab, label: labelFilter ?? undefined, limit: 200 }),
    enabled: mode === 'proposals',
  });
  const proposals = useMemo(() => proposalsQ.data?.data ?? [], [proposalsQ.data]);

  const tagImagesKey = useMemo(
    () => [...TAG_IMAGES_KEY, activeTagId, sampleState],
    [activeTagId, sampleState],
  );
  const tagImagesQ = useQuery({
    queryKey: tagImagesKey,
    queryFn: () =>
      listNewDedupTagImages(activeTagId as number, {
        state: sampleState === 'all' ? undefined : sampleState,
        limit: 200,
      }),
    enabled: mode === 'sample' && activeTagId != null,
  });
  const tagImages = useMemo(() => tagImagesQ.data?.data ?? [], [tagImagesQ.data]);

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
  }, [tab, labelFilter, mode, sampleState]);
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

  const settingValue = (key: string) => settingsQ.data?.data.find((s) => s.key === key)?.value;
  const gate1Target = (settingValue('labeling_gate1_target_per_tag') as number) ?? 150;
  const proposalTarget = (settingValue('labeling_target_proposals_per_category') as number) ?? 300;
  const secondaryModel = settingValue('labeling_secondary_model') as string | undefined;

  const invalidateOverview = () => qc.invalidateQueries({ queryKey: OVERVIEW_KEY });
  const invalidateProposals = () => qc.invalidateQueries({ queryKey: PROPOSALS_KEY });
  const invalidateTagImages = () => qc.invalidateQueries({ queryKey: TAG_IMAGES_KEY });
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
  const patchTagImages = (ids: ReadonlyArray<number>, state: TagState) => {
    const set = new Set(ids);
    qc.setQueryData<{ data: NewDedupTagImage[] }>(tagImagesKey, (old) =>
      old
        ? {
            ...old,
            data:
              sampleState === 'all' || sampleState === state
                ? old.data.map((r) => (set.has(r.image_id) ? { ...r, state } : r))
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

  // --- sample -------------------------------------------------------------

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
    }: {
      imageId: number;
      model: string;
      state: TagState;
      label?: string;
    }) => setNewDedupProposalState(imageId, model, state, label),
    onSuccess: (res, vars) => {
      if (res.data.corrected) pushToast('ok', `Set “${res.data.label}” to ${res.data.state}.`);
      clearDraft(vars.imageId, vars.model);
      applyReview([vars.imageId], vars.model, (p) => ({
        ...p,
        status: res.data.status,
        label: res.data.label,
        current_state: res.data.state,
        reviewed_by: 'operator',
      }));
      patchPositiveTags(vars.imageId, res.data.label, res.data.state);
    },
    onError: (err: Error) => pushToast('err', err.message),
    onSettled: (_data, _err, vars) => endAction(vars.imageId, vars.model),
  });
  const bulkStateMut = useMutation({
    mutationFn: ({ model, state }: { model: string; state: TagState }) =>
      bulkSetNewDedupProposalState(model, [...selected], state),
    onSuccess: (res) => {
      pushToast('ok', `Set ${res.data.updated} to ${res.data.state}.`);
      setSelected(new Set());
      applyReview(res.data.image_ids, res.data.model, (p) => ({
        ...p,
        status: res.data.state === 'positive' ? 'confirmed' : 'dismissed',
        current_state: res.data.state,
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
    mutationFn: ({ imageId, state }: { imageId: number; state: TagState }) =>
      setNewDedupTagAnnotation(activeTagId as number, imageId, state),
    onSuccess: (res, vars) => {
      patchTagImages([vars.imageId], res.data.state);
      if (labelFilter) patchPositiveTags(vars.imageId, labelFilter, res.data.state);
      invalidateOverview();
    },
    onError: (err: Error) => pushToast('err', err.message),
    onSettled: (_d, _e, vars) => endAction(vars.imageId, 'sample'),
  });
  const bulkTagAnnotationMut = useMutation({
    mutationFn: (state: TagState) =>
      bulkSetNewDedupTagAnnotation(activeTagId as number, [...selected], state),
    onSuccess: (res) => {
      pushToast('ok', `Set ${res.data.updated} to ${res.data.state}.`);
      setSelected(new Set());
      patchTagImages(res.data.image_ids, res.data.state);
      if (labelFilter) {
        for (const imageId of res.data.image_ids) patchPositiveTags(imageId, labelFilter, res.data.state);
      }
      invalidateOverview();
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

  const proposalKeyboard = useGridKeyboardReview(proposals.length, (i, state) => {
    const p = proposals[i];
    if (!p || p.status !== 'pending') return;
    beginAction(p.image_id, p.model);
    setStateMut.mutate({
      imageId: p.image_id,
      model: p.model,
      state,
      label: isCorrected(p) ? draftFor(p) : undefined,
    });
  });
  const sampleKeyboard = useGridKeyboardReview(tagImages.length, (i, state) => {
    const r = tagImages[i];
    if (!r || activeTagId == null) return;
    beginAction(r.image_id, 'sample');
    setTagAnnotationMut.mutate({ imageId: r.image_id, state });
  });
  const keyboard = mode === 'proposals' ? proposalKeyboard : sampleKeyboard;

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
        tag cell is positive, negative, or excluded (ambiguous — dropped from that head's
        training set entirely). An untouched cell defaults to negative. "Border case" is a
        separate, whole-image flag — a photo unclear even to a human — independent of any tag's
        state. Gate 1 needs {gate1Target} positive images per active tag, counting only the ones
        that are NOT parked as a border case.
      </p>

      {overviewQ.error && <ErrorBanner message={(overviewQ.error as Error).message} />}

      <TaxonomyBarChart
        tags={visibleTags}
        totalTags={allTags.length}
        sampleSize={overviewQ.data?.data.sample_size}
        loading={!overviewQ.data && !overviewQ.error}
        gate1Target={gate1Target}
        proposalTarget={proposalTarget}
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

      <section className="mt-8 border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-4">
        <span className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] mb-3">
          Sample
        </span>
        <div className="flex items-center gap-3 flex-wrap text-sm">
          <span className="text-[var(--color-ink-2)]">
            {overviewQ.data ? overviewQ.data.data.sample_size : '—'} images in sample
          </span>
          <span className="h-4 w-px bg-[var(--color-rule)]" aria-hidden />
          <input
            type="number"
            min={1}
            step={1}
            value={growCount}
            onChange={(e) => setGrowCount(e.target.value)}
            disabled={growMut.isPending}
            className="w-20 px-2 py-1 font-mono text-sm text-right rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)] disabled:opacity-50"
          />
          <select
            value={growCategory}
            onChange={(e) => setGrowCategory(e.target.value)}
            disabled={growMut.isPending}
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
          Adds newest not-yet-sampled images to the pool. Scoring runs separately via the "NEW
          DEDUP — Labeling secondary-CLIP proposals" GitHub Actions workflow (model:{' '}
          {secondaryModel ?? '…'}).
        </p>
      </section>

      <section className="mt-8">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-1">
            <ToggleButton active={mode === 'proposals'} onClick={() => setMode('proposals')}>
              Proposals
            </ToggleButton>
            <ToggleButton active={mode === 'sample'} onClick={() => setMode('sample')}>
              Sample
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
          <select
            id="labeling-tag-filter"
            value={labelFilter ?? ''}
            onChange={(e) => setLabelFilter(e.target.value || null)}
            className="px-2 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)] max-w-[18rem]"
          >
            <option value="">{mode === 'sample' ? 'Choose a tag…' : 'All tags'}</option>
            {labelFilter && !filterOptions.some((t) => t.label === labelFilter) && (
              <option value={labelFilter}>{labelFilter}</option>
            )}
            {filterOptions.map((t) => (
              <option key={t.id} value={t.label}>
                {t.label} ({t.gate_count})
              </option>
            ))}
          </select>
          {labelFilter && (
            <button
              type="button"
              onClick={() => setLabelFilter(null)}
              className="underline decoration-dotted underline-offset-2 text-[var(--color-ink-3)] hover:text-[var(--color-copper-2)]"
            >
              clear
            </button>
          )}
          {maxTrainedNum != null && (
            <span className="text-[var(--color-ink-4)]">
              {filterOptions.length} of {allTags.length} tags (≤ {maxTrainedNum} training images)
            </span>
          )}
          {mode === 'sample' && (
            <>
              <span className="h-4 w-px bg-[var(--color-rule)]" aria-hidden />
              <label htmlFor="labeling-sample-state" className="text-[var(--color-ink-3)]">
                State
              </label>
              <select
                id="labeling-sample-state"
                value={sampleState}
                onChange={(e) => setSampleState(e.target.value as SampleStateFilter)}
                className="px-2 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)]"
              >
                {SAMPLE_STATE_OPTIONS.map((o) => (
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
            {TAG_STATES.map((s) => (
              <button
                key={s}
                type="button"
                disabled={
                  selected.size === 0 ||
                  bulkStateMut.isPending ||
                  bulkTagAnnotationMut.isPending ||
                  (mode === 'proposals' && !secondaryModel)
                }
                onClick={() =>
                  mode === 'proposals'
                    ? secondaryModel && bulkStateMut.mutate({ model: secondaryModel, state: s })
                    : bulkTagAnnotationMut.mutate(s)
                }
                className={[
                  'px-2.5 py-1 text-xs rounded-[var(--radius-xs)] disabled:opacity-40',
                  STATE_META[s].activeClass,
                ].join(' ')}
              >
                Set selected: {s}
              </button>
            ))}
          </div>
        )}

        <p className="mt-2 text-[0.68rem] text-[var(--color-ink-4)]">
          Keyboard: click a tile to focus it, then ← → (or j/k) to move, 1/2/3 to set
          positive/negative/excluded and advance to the next.
        </p>

        {mode === 'proposals' ? (
          <>
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
                  onSetState={(state) => {
                    beginAction(p.image_id, p.model);
                    setStateMut.mutate({
                      imageId: p.image_id,
                      model: p.model,
                      state,
                      label: isCorrected(p) ? draftFor(p) : undefined,
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
                Choose a tag above to browse its sample.
              </p>
            )}
            {activeTagId != null && tagImagesQ.error && (
              <ErrorBanner message={(tagImagesQ.error as Error).message} />
            )}
            {activeTagId != null && !tagImagesQ.data && !tagImagesQ.error && (
              <p className="mt-6 text-sm text-[var(--color-ink-3)]">Loading sample…</p>
            )}
            {activeTagId != null && tagImagesQ.data && tagImages.length === 0 && (
              <p className="mt-6 text-sm text-[var(--color-ink-3)]">No images match.</p>
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
                    onSetState={(state) => {
                      beginAction(r.image_id, 'sample');
                      setTagAnnotationMut.mutate({ imageId: r.image_id, state });
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
            showOriginal || mode === 'sample'
              ? undefined
              : (i) => ({ tag: gallery[i]?.tag ?? null, confidence: gallery[i]?.confidence ?? null })
          }
        />
      )}

      {detailImageId != null && (
        <ImageTagDetailPanel
          imageId={detailImageId}
          onClose={() => setDetailImageId(null)}
          onTagStateChange={(label, state) => patchPositiveTags(detailImageId, label, state)}
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

/* Ranked horizontal bar chart, most positive images first. Collapsible
 * (persisted) because with a full taxonomy the chart pushes the review grid
 * off screen. */
function TaxonomyBarChart({
  tags,
  totalTags,
  sampleSize,
  loading,
  gate1Target,
  proposalTarget,
  activeLabel,
  maxTrained,
  onMaxTrainedChange,
  onFilter,
  onOpenManage,
}: {
  tags: NewDedupTag[];
  totalTags: number;
  sampleSize: number | undefined;
  loading: boolean;
  gate1Target: number;
  proposalTarget: number;
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
            {sampleSize != null ? `, ${sampleSize} sampled` : ''})
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
  onSetState: (state: TagState) => void;
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
          <LabelCombobox value={draft} onChange={onDraftChange} options={labelOptions} placeholder="tag…" />
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

/* Sample-mode tile: no proposal, no label picker (the tag is already fixed by
 * the page's tag filter) — just the photo, the tri-state control for THAT
 * tag, and border case. */
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
  onSetState: (state: TagState) => void;
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
      </div>
    </div>
  );
}

/* Image-centric detail: every active tag on ONE image, grouped by family,
 * each with the same tri-state control — the "open kitchen-living room"
 * case (kitchen positive, living_room excluded, everything else negative)
 * needs to be set in one sitting without hunting through per-tag screens. */
function ImageTagDetailPanel({
  imageId,
  onClose,
  onTagStateChange,
}: {
  imageId: number;
  onClose: () => void;
  onTagStateChange: (label: string, state: TagState) => void;
}) {
  const qc = useQueryClient();
  const key = ['new-dedup', 'labeling', 'image-tags', imageId];
  const q = useQuery({ queryKey: key, queryFn: () => listNewDedupImageTags(imageId) });
  const rows = useMemo(() => q.data?.data ?? [], [q.data]);
  const grouped = useMemo(() => {
    const groups = new Map<string, typeof rows>();
    for (const r of rows) {
      const family = r.family ?? '—';
      groups.set(family, [...(groups.get(family) ?? []), r]);
    }
    return [...groups.entries()];
  }, [rows]);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const toggleSelect = (tagId: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(tagId)) next.delete(tagId);
      else next.add(tagId);
      return next;
    });
  // "Select all" targets untouched tags — the actual use case (close out
  // everything an operator hasn't looked at yet on this image) — without
  // silently overwriting tags already decided one at a time. Any row can
  // still be checked or unchecked by hand regardless.
  const untouchedIds = useMemo(
    () => rows.filter((r) => r.state === 'untouched').map((r) => r.id),
    [rows],
  );
  const allUntouchedSelected =
    untouchedIds.length > 0 && untouchedIds.every((id) => selected.has(id));

  const setMut = useMutation({
    mutationFn: (vars: { tagId: number; label: string; state: TagState }) =>
      setNewDedupTagAnnotation(vars.tagId, imageId, vars.state),
    onSuccess: (res, vars) => {
      qc.setQueryData<{ data: typeof rows }>(key, (old) =>
        old
          ? { ...old, data: old.data.map((r) => (r.id === vars.tagId ? { ...r, state: res.data.state } : r)) }
          : old,
      );
      onTagStateChange(vars.label, res.data.state);
      // No longer untouched — drop it from the batch selection so a later
      // "Set selected" can't silently re-decide a tile already handled
      // one at a time.
      setSelected((prev) => {
        if (!prev.has(vars.tagId)) return prev;
        const next = new Set(prev);
        next.delete(vars.tagId);
        return next;
      });
      qc.invalidateQueries({ queryKey: OVERVIEW_KEY });
      qc.invalidateQueries({ queryKey: TAG_IMAGES_KEY });
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  const bulkSetMut = useMutation({
    mutationFn: (state: TagState) => bulkSetNewDedupImageTags(imageId, [...selected], state),
    onSuccess: (res) => {
      const changedIds = new Set(res.data.tag_ids);
      qc.setQueryData<{ data: typeof rows }>(key, (old) =>
        old
          ? {
              ...old,
              data: old.data.map((r) => (changedIds.has(r.id) ? { ...r, state: res.data.state } : r)),
            }
          : old,
      );
      for (const r of rows) {
        if (changedIds.has(r.id)) onTagStateChange(r.label, res.data.state);
      }
      setSelected(new Set());
      pushToast('ok', `Set ${res.data.updated} to ${res.data.state}.`);
      qc.invalidateQueries({ queryKey: OVERVIEW_KEY });
      qc.invalidateQueries({ queryKey: TAG_IMAGES_KEY });
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-[var(--color-ink)]/40 px-4 pt-[10vh]"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="flex max-h-[78vh] w-full max-w-lg flex-col rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] p-5 shadow-lg"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="All tags on this image"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-lg leading-tight" style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}>
            Image {imageId} — all tags
          </h2>
          <button type="button" onClick={onClose} aria-label="Close" className="text-[var(--color-ink-3)] hover:text-[var(--color-ink)]">
            ✕
          </button>
        </div>

        {q.isLoading && <p className="mt-4 text-sm text-[var(--color-ink-3)]">Loading…</p>}
        {q.error && <ErrorBanner message={(q.error as Error).message} />}

        {rows.length > 0 && (
          <div className="mt-3 flex items-center gap-3 flex-wrap">
            <button
              type="button"
              onClick={() =>
                setSelected(allUntouchedSelected ? new Set() : new Set(untouchedIds))
              }
              disabled={untouchedIds.length === 0}
              className="px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] disabled:opacity-40"
            >
              {allUntouchedSelected ? 'Deselect all' : 'Select all untouched'}
            </button>
            <span className="text-xs text-[var(--color-ink-3)]">{selected.size} selected</span>
            {TAG_STATES.map((s) => (
              <button
                key={s}
                type="button"
                disabled={selected.size === 0 || bulkSetMut.isPending}
                onClick={() => bulkSetMut.mutate(s)}
                className={[
                  'px-2.5 py-1 text-xs rounded-[var(--radius-xs)] disabled:opacity-40',
                  STATE_META[s].activeClass,
                ].join(' ')}
              >
                Set selected: {s}
              </button>
            ))}
          </div>
        )}

        <div className="mt-3 flex-1 space-y-4 overflow-y-auto">
          {grouped.map(([family, tags]) => (
            <div key={family}>
              <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mb-1.5">
                {family}
              </p>
              <div className="space-y-1">
                {tags.map((t) => (
                  <div key={t.id} className="flex items-center gap-2 py-0.5">
                    <input
                      type="checkbox"
                      checked={selected.has(t.id)}
                      onChange={() => toggleSelect(t.id)}
                      className="h-3.5 w-3.5 shrink-0"
                      aria-label={`Select ${t.label} for batch action`}
                    />
                    <span className="min-w-0 flex-1 truncate font-mono text-sm text-[var(--color-ink-2)]">
                      {t.label}
                    </span>
                    <TriStateControl
                      state={t.state}
                      onChange={(state) => setMut.mutate({ tagId: t.id, label: t.label, state })}
                      disabled={setMut.isPending && setMut.variables?.tagId === t.id}
                    />
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="mt-6 p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
      <strong className="font-medium">Failed:</strong> {message}
    </div>
  );
}
