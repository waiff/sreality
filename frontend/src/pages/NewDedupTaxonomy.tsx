import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  bulkSetNewDedupTagAnnotation,
  getNewDedupLabelingOverview,
  getTagDefinition,
  getTagDefinitionVersion,
  listTagDefinitionStatus,
  listTagDefinitionVersions,
  listTagNeighbours,
  listTagPositiveImages,
  previewTagDefinitionCard,
  listTagLabelNotes,
  absorbTagLabelNotes,
  removeNewDedupTag,
  renameNewDedupTag,
  saveTagDefinition,
  setNewDedupTagAnnotation,
  type NewDedupImageTag,
  type NewDedupLabelingOverview,
  type NewDedupTag,
  type SaveTagDefinitionIn,
  type TagDefinition,
  type TagDefinitionStatus,
  type TagExcludedReason,
  type TagPositiveImage,
  type TagState,
  type TagPositiveImagesResponse,
} from '@/lib/api';
import { fetchImagesByImageIds } from '@/lib/queries';
import { pushToast } from '@/lib/toast';
import Spinner from '@/components/Spinner';
import ErrorBanner from '@/components/ErrorBanner';
import DefinitionEditor, {
  EMPTY_DRAFT,
  type Draft,
} from '@/components/tag-definitions/DefinitionEditor';
import DefinitionReadOnly from '@/components/tag-definitions/DefinitionReadOnly';
import DefinitionCard from '@/components/tag-definitions/DefinitionCard';
import OverlapEvidence from '@/components/tag-definitions/OverlapEvidence';
import DefinitionNotes from '@/components/tag-definitions/DefinitionNotes';
import TagContentsGallery, {
  type BatchFileRequest,
  type BatchFileResult,
  type MovedOutImage,
} from '@/components/tag-definitions/TagContentsGallery';
import {
  orderPositives,
  type ContentsOrder,
} from '@/components/tag-definitions/ContentsOrder';
import TagDefinitionList from '@/components/tag-definitions/TagDefinitionList';
import TagDeleteConfirm from '@/components/tag-definitions/TagDeleteConfirm';
import ImageTagDetailPanel, {
  type ImageTagChange,
} from '@/components/tag-annotations/ImageTagDetailPanel';
import {
  NEW_DEDUP_CANDIDATES_KEY,
  NEW_DEDUP_OVERVIEW_KEY,
  NEW_DEDUP_PROPOSALS_KEY,
  NEW_DEDUP_TAG_IMAGES_KEY,
  newDedupImageTagsKey,
  newDedupPositiveImagesKey,
} from '@/lib/newDedupKeys';
import type { ImagePublic } from '@/lib/types';

/* NEW DEDUP · Taxonomy — the Phase-0 workbench where the ~51 tags stop being
 * bare Czech strings and get a written meaning (migration 445).
 *
 * Three surfaces, one job: the tag list says which definitions are still
 * missing, the editor is where the sentence gets written, and directly below it
 * the tag's own positives plus its nearest neighbours in CLIP space say whether
 * the sentence is true. The evidence sits UNDER the form, not behind a tab,
 * because the definition is the diagnostic — if you cannot write a
 * does-not-count line separating two tags, they are one tag, and you only see
 * that with both in view.
 *
 * There are no drafts server-side: one Save = one version. So every edit to the
 * DEFINITION — text, picker, gallery click, "add to confusable" — stages into
 * local state and the page makes exactly ONE network write.
 *
 * Two edits deliberately sit outside that rule, because neither is part of the
 * document: retagging an image from the gallery's "all tags" pill, and renaming
 * a tag from the list. A tri-state cell and a tag's name are ground truth with
 * no draft to batch into, so both write immediately — the gallery says so in as
 * many words, and neither touches the definition draft. */

const DEFINITIONS_KEY = ['new-dedup', 'labeling', 'definitions'];
const definitionKey = (tagId: number) => ['new-dedup', 'labeling', 'definition', tagId];
const versionsKey = (tagId: number) => ['new-dedup', 'labeling', 'definition-versions', tagId];
const versionKey = (tagId: number, v: number) => [
  'new-dedup',
  'labeling',
  'definition-version',
  tagId,
  v,
];
const neighboursKey = (tagId: number) => ['new-dedup', 'labeling', 'neighbours', tagId];
const notesKey = (tagId: number) => ['new-dedup', 'labeling', 'label-notes', tagId];
const photosKey = (ids: string) => ['new-dedup', 'taxonomy', 'photos', ids];
/* Prefixes, for the writes that dirty EVERY tag's copy of a label-carrying
 * read rather than one tag's. Local consts, not additions to newDedupKeys:
 * nothing outside this page needs them, and the second one is already used
 * inline by the rename path below. */
const NEW_DEDUP_NEIGHBOURS_PREFIX = ['new-dedup', 'labeling', 'neighbours'];
const NEW_DEDUP_IMAGE_TAGS_PREFIX = ['new-dedup', 'labeling', 'image-tags'];

/* The server caps this list at 300; asking for exactly the cap is what makes
 * the grid's "showing the N most recent" note both reachable and truthful. */
const POSITIVE_IMAGE_LIMIT = 300;
const NEIGHBOUR_LIMIT = 8;
/* Mirrors toolkit.tag_definitions.MIN_POSITIVES_FOR_CENTROID — the floor under
 * which a tag has no centroid and `nearest_tags` returns [] rather than a
 * confident-looking wrong answer. */
const MIN_POSITIVES_FOR_CENTROID = 5;
/* Mirrors toolkit.tag_definitions.EXAMPLE_IMAGES_MAX — the server rejects the
 * whole document past it, so the 25th click has to be refused here rather than
 * turning a finished sitting into a 422. */
const EXAMPLE_IMAGES_MAX = 24;
/* Mirrors toolkit.tag_annotations.BULK_STATE_MAX — the server refuses a larger
 * batch outright, and a select-all over a 300-row grid exceeds it. */
const BULK_ANNOTATION_MAX = 200;

const chunkIds = (ids: ReadonlyArray<number>, size: number): number[][] => {
  const out: number[][] = [];
  for (let i = 0; i < ids.length; i += size) out.push([...ids.slice(i, i + size)]);
  return out;
};

const draftFrom = (def: TagDefinition | null | undefined): Draft =>
  def
    ? {
        means: def.means,
        counts: [...def.counts],
        does_not_count: def.does_not_count.map((r) => ({ ...r })),
        confusable_with: def.confusable_with.map((r) => ({ ...r })),
        leave_out_when: def.leave_out_when ?? '',
        example_image_ids: [...def.example_image_ids],
      }
    : EMPTY_DRAFT;

/* Rows the operator left blank are dropped rather than saved — but only rows
 * that carry NOTHING. A row with a chosen "goes to" tag and no case text is
 * half-written, not empty, and blocks Save (doesNotCountIncomplete) instead of
 * vanishing on the way to the server. */
const toPayload = (d: Draft, baseVersion: number | null): SaveTagDefinitionIn => ({
  means: d.means.trim(),
  counts: d.counts.map((c) => c.trim()).filter((c) => c !== ''),
  does_not_count: d.does_not_count
    .map((r) => ({ case: r.case.trim(), goes_to_tag_id: r.goes_to_tag_id }))
    .filter((r) => r.case !== ''),
  confusable_with: d.confusable_with
    .map((r) => ({ tag_id: r.tag_id, tell: r.tell.trim() }))
    .filter((r) => r.tag_id !== 0 && r.tell !== ''),
  leave_out_when: d.leave_out_when.trim() || null,
  example_image_ids: [...d.example_image_ids],
  base_version: baseVersion,
});

const fmtDate = (iso: string): string => iso.slice(0, 10);

export default function NewDedupTaxonomy() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const selectedTagId = Number(params.get('tag')) || null;

  const overviewQ = useQuery({ queryKey: NEW_DEDUP_OVERVIEW_KEY, queryFn: getNewDedupLabelingOverview });
  const statusQ = useQuery({ queryKey: DEFINITIONS_KEY, queryFn: listTagDefinitionStatus });

  const tags = useMemo<NewDedupTag[]>(() => overviewQ.data?.data.tags ?? [], [overviewQ.data]);
  const tagById = useMemo(() => new Map(tags.map((t) => [t.id, t])), [tags]);
  const statusByTag = useMemo(
    () => new Map<number, TagDefinitionStatus>((statusQ.data?.data ?? []).map((s) => [s.tag_id, s])),
    [statusQ.data],
  );
  const selectedTag = selectedTagId != null ? (tagById.get(selectedTagId) ?? null) : null;

  const definitionQ = useQuery({
    queryKey: definitionKey(selectedTagId ?? 0),
    queryFn: () => getTagDefinition(selectedTagId as number),
    enabled: selectedTagId != null,
  });
  const versionsQ = useQuery({
    queryKey: versionsKey(selectedTagId ?? 0),
    queryFn: () => listTagDefinitionVersions(selectedTagId as number),
    enabled: selectedTagId != null,
  });
  const positiveImagesQ = useQuery({
    queryKey: newDedupPositiveImagesKey(selectedTagId ?? 0),
    queryFn: () =>
      listTagPositiveImages(selectedTagId as number, POSITIVE_IMAGE_LIMIT, 'outlier_first'),
    enabled: selectedTagId != null,
  });
  const neighboursQ = useQuery({
    queryKey: neighboursKey(selectedTagId ?? 0),
    queryFn: () => listTagNeighbours(selectedTagId as number, NEIGHBOUR_LIMIT),
    enabled: selectedTagId != null,
  });

  /* The operator's reasons for changing marks on this head (training-set
   * page) — read together, distilled into one rule, then marked absorbed. */
  const notesQ = useQuery({
    queryKey: notesKey(selectedTagId ?? 0),
    queryFn: () => listTagLabelNotes(selectedTagId as number),
    enabled: selectedTagId != null,
  });
  const absorbMut = useMutation({
    mutationFn: (vars: { tagId: number; definitionId: number; noteIds: number[] }) =>
      absorbTagLabelNotes(vars.tagId, { definition_id: vars.definitionId, note_ids: vars.noteIds }),
    onSuccess: (res, vars) => {
      qc.invalidateQueries({ queryKey: notesKey(vars.tagId) });
      const short = res.data.requested - res.data.absorbed.length;
      pushToast(short ? 'err' : 'ok',
        short
          ? `${res.data.absorbed.length} of ${res.data.requested} absorbed — the rest were another tag's or already absorbed`
          : `${res.data.absorbed.length} notes absorbed`);
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  /* ONE fetch per tag, always the distance order — "Newest first" is a
   * client-side re-sort of the same rows, so flipping the order never refetches
   * a visible grid and every cache patch below stays keyed on one entry. */
  const [contentsOrder, setContentsOrder] = useState<ContentsOrder>('outlier_first');
  const positivesRes = positiveImagesQ.data;
  /* The server's own verdict, not a re-derivation from the counts. */
  const outlierApplied = positivesRes?.order === 'outlier_first';
  const effectiveOrder: ContentsOrder = outlierApplied ? contentsOrder : 'recent';
  const positiveRows = useMemo(
    () => orderPositives(positivesRes?.data ?? [], effectiveOrder),
    [positivesRes, effectiveOrder],
  );

  // --- draft state ---------------------------------------------------------

  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [baseline, setBaseline] = useState<Draft>(EMPTY_DRAFT);
  /* The version the FORM was loaded from — not the newest the server has told us
   * about since. It is what every save asserts against (see toPayload), so a
   * definition another tab moved on is a 422 and never a silent revert. */
  const [loadedVersion, setLoadedVersion] = useState<number | null>(null);
  const [focusConfusableIndex, setFocusConfusableIndex] = useState<number | null>(null);
  const [viewingVersion, setViewingVersion] = useState<number | null>(null);

  const definition = definitionQ.data?.data ?? null;
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const patch = useCallback((p: Partial<Draft>) => setDraft((d) => ({ ...d, ...p })), []);

  /* The handbook card for whatever is currently in the form.
   *
   * Rendered SERVER-side, deliberately. A TypeScript copy of the renderer would
   * be a second implementation of what a tag means, free to drift from the one
   * the vision model is actually given — and that drift is the exact failure the
   * two-renderings design exists to prevent. Debounced, because the cost of the
   * round-trip is a keystroke's latency and the cost of drift is silent. */
  const [debouncedDraft, setDebouncedDraft] = useState<Draft>(EMPTY_DRAFT);
  useEffect(() => {
    const t = setTimeout(() => setDebouncedDraft(draft), 350);
    return () => clearTimeout(t);
  }, [draft]);

  const previewQ = useQuery({
    queryKey: [...definitionKey(selectedTagId ?? 0), 'card', debouncedDraft],
    queryFn: () =>
      previewTagDefinitionCard(selectedTagId as number, toPayload(debouncedDraft, loadedVersion)),
    enabled: selectedTagId != null && debouncedDraft.means.trim() !== '',
    /* A failed preview must never look like an empty definition, so the last
     * good card stays on screen while a new one is in flight. */
    placeholderData: (prev) => prev,
  });
  const previewCard = previewQ.data?.data.card ?? null;

  /* Load the form from a server document — the ONE place draft, baseline and
   * loadedVersion move together. A save reloads through it too, so the saved
   * document (already normalized server-side) becomes the new clean baseline
   * rather than leaving the page permanently "unsaved". */
  const loadForm = useCallback((def: TagDefinition | null) => {
    const next = draftFrom(def);
    setDraft(next);
    setBaseline(next);
    setLoadedVersion(def?.version ?? null);
  }, []);

  /* Photos ACCUMULATE across tag switches rather than being refetched per grid
   * (the idiom NewDedupLabeling landed on): only never-seen ids are ever
   * requested, so a tile that is already on screen never blinks again — and
   * flipping back to a tag you looked at a minute ago is instant. The grid
   * itself does NOT wait on this — it paints from the rows the API already
   * returned; this feeds the lightbox and the off-list example chips. */
  const [photos, setPhotos] = useState<ReadonlyMap<number, ImagePublic>>(new Map());
  const missingIds = useMemo(
    () => [
      ...new Set(
        [...positiveRows.map((r) => r.image_id), ...draft.example_image_ids].filter(
          (id) => !photos.has(id),
        ),
      ),
    ],
    [positiveRows, draft.example_image_ids, photos],
  );
  const photosQ = useQuery({
    queryKey: photosKey(missingIds.join(',')),
    queryFn: () => fetchImagesByImageIds(missingIds),
    enabled: missingIds.length > 0,
  });
  useEffect(() => {
    const fetched = photosQ.data;
    if (!fetched || fetched.size === 0) return;
    setPhotos((prev) => {
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
  }, [photosQ.data]);

  /* What the form is currently loaded FROM, as an identity rather than an
   * object reference. A background refetch (react-query refetches on window
   * focus) returns the same version and must never wipe half-written text; only
   * a different tag, or a genuinely different version, reloads the form. And if
   * the operator is mid-edit, even that is left alone — the save then carries
   * the base_version the form WAS loaded from, so a definition another tab
   * moved on lands as the backend's 422 rather than as a silent revert. */
  const loadStamp =
    selectedTagId == null
      ? 'none'
      : definitionQ.data === undefined
        ? `${selectedTagId}:loading`
        : `${selectedTagId}:${definition ? `${definition.id}v${definition.version}` : 'empty'}`;
  const loadedStamp = useRef<string | null>(null);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useEffect(() => {
    if (loadedStamp.current === loadStamp) return;
    // Only within ONE tag: switching tags has already passed the discard
    // confirm, so there the reload is what the operator asked for.
    const sameTag = loadedStamp.current?.split(':')[0] === String(selectedTagId);
    if (sameTag && dirtyRef.current) return;
    loadedStamp.current = loadStamp;
    loadForm(definitionQ.data?.data ?? null);
  }, [loadStamp, selectedTagId, definitionQ.data, loadForm]);

  useEffect(() => {
    setViewingVersion(null);
  }, [selectedTagId]);

  const versionQ = useQuery({
    queryKey: versionKey(selectedTagId ?? 0, viewingVersion ?? 0),
    queryFn: () => getTagDefinitionVersion(selectedTagId as number, viewingVersion as number),
    enabled: selectedTagId != null && viewingVersion != null,
  });

  const saveMut = useMutation({
    mutationFn: (vars: { tagId: number; body: SaveTagDefinitionIn }) =>
      saveTagDefinition(vars.tagId, vars.body),
    onSuccess: (res, vars) => {
      qc.setQueryData(definitionKey(vars.tagId), { data: res.data });
      qc.invalidateQueries({ queryKey: DEFINITIONS_KEY });
      qc.invalidateQueries({ queryKey: versionsKey(vars.tagId) });
      /* Adopt what the server actually stored as the new clean baseline —
       * otherwise the page stays dirty forever after its first save (Save stays
       * live and a second click writes a byte-identical version) and the
       * whitespace/dedupe normalisation the toolkit applied stays invisible. The
       * tag guard is for a save that resolves after a tag switch. */
      if (vars.tagId === selectedTagId) loadForm(res.data);
      pushToast('ok', `Saved v${res.data.version}.`);
    },
    // Own onError, so main.tsx's MutationCache doesn't also toast it. The draft
    // is deliberately left intact — a failed save must never eat the writing.
    onError: (err: Error) => pushToast('err', err.message),
  });

  const selectTag = (id: number) => {
    if (id === selectedTagId) return;
    if (dirty && !window.confirm('Discard unsaved changes to this definition?')) return;
    setParams({ tag: String(id) }, { replace: true });
  };

  // --- retagging from the gallery ------------------------------------------

  /* The one image whose all-tags panel is open. Distinct from the tile click,
   * which still means "stage as a canonical example". */
  const [detailImageId, setDetailImageId] = useState<number | null>(null);

  /* Images taken OUT of this tag during this sitting — a receipt that persists
   * and is actionable, which is why there is no toast on a single subject write.
   * Session-local, cleared on a tag switch. */
  const [movedOut, setMovedOut] = useState<ReadonlyArray<MovedOutImage>>([]);
  const movedOutRef = useRef<ReadonlyArray<MovedOutImage>>([]);
  movedOutRef.current = movedOut;

  /* Batch-file state. Selection is a MODE, not a modifier: while it is on a
   * tile click selects, while it is off a tile click stages a canonical
   * example, and exactly one of those is true at any moment. */
  const [selecting, setSelecting] = useState(false);
  const [selectedImageIds, setSelectedImageIds] = useState<ReadonlySet<number>>(new Set());
  const [batchResult, setBatchResult] = useState<BatchFileResult | null>(null);
  const [batchWritten, setBatchWritten] = useState(0);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => {
    setMovedOut([]);
    setDetailImageId(null);
    setSelecting(false);
    setSelectedImageIds(new Set());
    setBatchResult(null);
    setBatchWritten(0);
    setConfirmingDelete(false);
    setDeleteError(null);
  }, [selectedTagId]);

  /* The narrowing invariant: an image taken out through the all-tags panel
   * mid-selection drops out of the batch on its own, the readout can never
   * overcount, and no payload can name an id that is not currently positive on
   * the source. One derived value kills a whole class of bug. */
  const effectiveSelectedIds = useMemo(() => {
    if (selectedImageIds.size === 0) return new Set<number>();
    const next = new Set<number>();
    for (const r of positiveRows) if (selectedImageIds.has(r.image_id)) next.add(r.image_id);
    return next;
  }, [positiveRows, selectedImageIds]);

  const toggleSelectImage = useCallback(
    (imageId: number) =>
      setSelectedImageIds((prev) => {
        const next = new Set(prev);
        if (next.has(imageId)) next.delete(imageId);
        else next.add(imageId);
        return next;
      }),
    [],
  );

  /* Put the row back where it was, not at the top. The server orders by
   * `updated_at DESC` and a put-back genuinely moves the row, so it WILL float
   * to the front on the next natural refetch — converging then is honest;
   * reshuffling the grid under the operator's cursor mid-sitting is the churn
   * both pages are built to avoid. Under the DISTANCE order the same argument
   * holds even more quietly: a put-back moves the row only as far as the
   * centroid it re-joins shifts, which is a fraction of one image in N. */
  /* Returns whether the patch was possible at all: the receipt strip is
   * session-local and a tag switch empties it, so a put-back that resolves
   * after one has no row to splice back and the caller has to repair the cache
   * some other way. */
  const restorePositiveRow = useCallback(
    (tagId: number, imageId: number): boolean => {
      const held = movedOutRef.current.find((m) => m.row.image_id === imageId);
      setMovedOut((prev) => prev.filter((m) => m.row.image_id !== imageId));
      if (!held) return false;
      qc.setQueryData<TagPositiveImagesResponse>(
        newDedupPositiveImagesKey(tagId),
        (old) => {
          if (!old || old.data.some((r) => r.image_id === imageId)) return old;
          const next = [...old.data];
          next.splice(Math.min(held.index, next.length), 0, held.row);
          return { ...old, data: next };
        },
      );
      return true;
    },
    [qc],
  );

  /* Batch sibling of restorePositiveRow: ONE setQueryData, splicing ASCENDING
   * by held index — descending would land later rows at the wrong offsets.
   * Returns the ids it could not place, which is what the caller has to repair
   * some other way. */
  const restorePositiveRows = useCallback(
    (tagId: number, imageIds: ReadonlyArray<number>): number[] => {
      const wanted = new Set(imageIds);
      const held = movedOutRef.current
        .filter((m) => wanted.has(m.row.image_id))
        .sort((a, b) => a.index - b.index);
      setMovedOut((prev) => prev.filter((m) => !wanted.has(m.row.image_id)));
      const placed = new Set<number>();
      qc.setQueryData<{ data: TagPositiveImage[] }>(
        newDedupPositiveImagesKey(tagId),
        (old) => {
          if (!old) return old;
          const next = [...old.data];
          for (const h of held) {
            if (next.some((r) => r.image_id === h.row.image_id)) continue;
            next.splice(Math.min(h.index, next.length), 0, h.row);
            placed.add(h.row.image_id);
          }
          return { ...old, data: next };
        },
      );
      return [...imageIds].filter((id) => !placed.has(id));
    },
    [qc],
  );

  /* Patched, never invalidated: an invalidate refetches up to 300 rows,
   * re-renders every tile, AND reorders by updated_at. The patch is not a guess
   * — the server already agrees the row is no longer positive, so a background
   * focus-refetch converges on the same answer. */
  const onImageTagChange = useCallback(
    (c: ImageTagChange) => {
      /* A write to a tag OTHER than the one being read — the whole point of the
       * panel's second half. That tag's gallery is not mounted, so there is
       * nothing to blink and nothing to patch in place; but it IS cached, and
       * the overview refetch has already moved its count in the list on the
       * left. Mark it stale, or selecting it inside main.tsx's 60s staleTime
       * serves a gallery this very write contradicted. Inactive queries only
       * take the flag — the refetch happens when the operator gets there. */
      if (selectedTagId == null || c.tagId !== selectedTagId) {
        qc.invalidateQueries({ queryKey: newDedupPositiveImagesKey(c.tagId) });
        return;
      }
      if (c.state === 'positive') {
        // A `false` here is a re-affirmed positive on a tile that never left,
        // not a hole in the cache — invalidating would blink the visible grid.
        restorePositiveRow(selectedTagId, c.imageId);
        return;
      }
      const key = newDedupPositiveImagesKey(selectedTagId);
      const cached = qc.getQueryData<TagPositiveImagesResponse>(key);
      const index = cached?.data.findIndex((r) => r.image_id === c.imageId) ?? -1;
      const row = index >= 0 ? cached?.data[index] : undefined;
      if (row) {
        qc.setQueryData<TagPositiveImagesResponse>(key, (old) =>
          old ? { ...old, data: old.data.filter((r) => r.image_id !== c.imageId) } : old,
        );
      }
      setMovedOut((prev) => {
        const existing = prev.find((m) => m.row.image_id === c.imageId);
        // A second outcome on the same image only restates WHY it left.
        if (existing)
          return prev.map((m) =>
            m.row.image_id === c.imageId
              ? { ...m, state: c.state, excludedReason: c.excludedReason }
              : m,
          );
        if (!row) return prev;
        return [...prev, { row, index, state: c.state, excludedReason: c.excludedReason }];
      });
    },
    [qc, restorePositiveRow, selectedTagId],
  );

  const putBackMut = useMutation({
    mutationFn: (vars: { tagId: number; imageId: number }) =>
      setNewDedupTagAnnotation(vars.tagId, vars.imageId, 'positive', null),
    onSuccess: (res, vars) => {
      /* Normally a patch. But a tag switch empties the receipt strip, so a
       * put-back that resolves after one leaves the server holding a positive
       * the cached grid does not list — and nothing on screen to explain it.
       * The refetch is the fallback for exactly that path, never the norm, so
       * no visible list blinks on the ordinary one. */
      if (!restorePositiveRow(vars.tagId, vars.imageId))
        qc.invalidateQueries({ queryKey: newDedupPositiveImagesKey(vars.tagId) });
      /* Patched, not invalidated, for the same reason the panel patches its own
       * list: an open panel must not blink through a refetch. */
      qc.setQueryData<{ data: NewDedupImageTag[] }>(
        newDedupImageTagsKey(vars.imageId),
        (old) =>
          old
            ? {
                ...old,
                data: old.data.map((r) =>
                  r.id === vars.tagId
                    ? {
                        ...r,
                        state: res.data.state,
                        source: res.data.source,
                        excluded_reason: res.data.excluded_reason,
                      }
                    : r,
                ),
              }
            : old,
      );
      // Nine interdependent derived numbers moved, and ambiguity_rate has
      // exactly ONE definition — server-side. Never re-derived here.
      qc.invalidateQueries({ queryKey: NEW_DEDUP_OVERVIEW_KEY });
    },
    onError: (err: Error) => pushToast('err', err.message),
  });
  const putBackPending = useMemo(
    () =>
      new Set<number>(
        putBackMut.isPending && putBackMut.variables ? [putBackMut.variables.imageId] : [],
      ),
    [putBackMut.isPending, putBackMut.variables],
  );

  // --- filing a batch under another tag -------------------------------------

  /* Every annotation write moves the same nine derived numbers on both tags and
   * dirties the same label-carrying reads on the OTHER page. One helper, so a
   * batch and a single write can never disagree about what one decision dirties.
   * NEW_DEDUP_PROPOSALS_KEY is deliberately NOT here — the single/bulk writes in
   * ImageTagDetailPanel do not invalidate it either. */
  const invalidateAnnotationReads = useCallback(() => {
    qc.invalidateQueries({ queryKey: NEW_DEDUP_OVERVIEW_KEY });
    qc.invalidateQueries({ queryKey: NEW_DEDUP_IMAGE_TAGS_PREFIX });
    qc.invalidateQueries({ queryKey: NEW_DEDUP_TAG_IMAGES_KEY });
    qc.invalidateQueries({ queryKey: NEW_DEDUP_CANDIDATES_KEY });
  }, [qc]);

  const batchMut = useMutation({
    /* NEVER rejects. It resolves with a BatchFileResult for all four terminal
     * cases, so partial failure has exactly one handler and no toast path.
     *
     * Destination FIRST, always. If the source were written first and the
     * destination then failed, images would have left the source with nowhere
     * to go; destination-first means the worst case is a duplicate positive on
     * both tags — precisely the safe `keeps` state, and recoverable by pressing
     * Write again. Chunks are sequential, never Promise.all: a deterministic
     * failure point, and polite to a single-operator API. */
    mutationFn: async (
      vars: BatchFileRequest & { sourceTagId: number },
    ): Promise<BatchFileResult> => {
      const base = {
        destTagId: vars.destTagId,
        destLabel: tagById.get(vars.destTagId)?.label ?? `tag ${vars.destTagId}`,
        outcome: vars.outcome,
        requestedIds: [...vars.imageIds],
      };
      const chunks = chunkIds(vars.imageIds, BULK_ANNOTATION_MAX);
      const destWritten: number[] = [];
      setBatchWritten(0);

      for (const c of chunks) {
        try {
          await bulkSetNewDedupTagAnnotation(vars.destTagId, c, 'positive', null);
        } catch (err) {
          const landed = new Set(destWritten);
          return {
            ...base,
            status: 'dest-failed',
            destWrittenIds: destWritten,
            sourceWrittenIds: [],
            unresolvedIds: base.requestedIds.filter((id) => !landed.has(id)),
            message: (err as Error).message,
          };
        }
        destWritten.push(...c);
        setBatchWritten(destWritten.length);
      }

      if (vars.outcome === 'keeps')
        return {
          ...base,
          status: 'ok',
          destWrittenIds: destWritten,
          sourceWrittenIds: [],
          unresolvedIds: [],
          message: null,
        };

      const state: TagState = vars.outcome === 'not-this' ? 'negative' : 'excluded';
      const reason: TagExcludedReason | null = vars.outcome === 'not-this' ? null : 'pruned';
      const sourceWritten: number[] = [];
      for (const c of chunks) {
        try {
          await bulkSetNewDedupTagAnnotation(vars.sourceTagId, c, state, reason);
        } catch (err) {
          const done = new Set(sourceWritten);
          return {
            ...base,
            status: 'source-failed',
            destWrittenIds: destWritten,
            sourceWrittenIds: sourceWritten,
            unresolvedIds: destWritten.filter((id) => !done.has(id)),
            message: (err as Error).message,
          };
        }
        sourceWritten.push(...c);
      }
      return {
        ...base,
        status: 'ok',
        destWrittenIds: destWritten,
        sourceWrittenIds: sourceWritten,
        unresolvedIds: [],
        message: null,
      };
    },
    onSuccess: (res, vars) => {
      setBatchResult(res);
      setBatchWritten(0);
      /* The selection narrows to what "press Write again" must act on. On a
       * clean run of `keeps` the rows never left, so the selection stays intact
       * — filing the same block under a second tag is one picker change away. */
      if (res.status !== 'ok') setSelectedImageIds(new Set(res.unresolvedIds));

      /* Patched, never invalidated: an invalidate refetches up to 300 rows,
       * re-renders every tile AND reorders by updated_at. The patch is not a
       * guess — the server already agrees these rows are no longer positive. A
       * `keeps` batch touches nothing here, and must not blink the grid. */
      if (res.sourceWrittenIds.length > 0) {
        const state: TagState = res.outcome === 'not-this' ? 'negative' : 'excluded';
        const excludedReason: TagExcludedReason | null =
          res.outcome === 'not-this' ? null : 'pruned';
        const key = newDedupPositiveImagesKey(vars.sourceTagId);
        const written = new Set(res.sourceWrittenIds);
        const cached = qc.getQueryData<{ data: TagPositiveImage[] }>(key);
        const held: MovedOutImage[] = [];
        cached?.data.forEach((row, index) => {
          if (written.has(row.image_id)) held.push({ row, index, state, excludedReason });
        });
        qc.setQueryData<{ data: TagPositiveImage[] }>(key, (old) =>
          old ? { ...old, data: old.data.filter((r) => !written.has(r.image_id)) } : old,
        );
        /* The cache patch is right whatever is on screen — those rows really
         * did leave. The receipt strip is session-local and a tag switch
         * empties it, so a batch that resolves after one must not push chips
         * describing a tag the operator is no longer reading. */
        if (vars.sourceTagId === selectedTagId)
          setMovedOut((prev) => [
            ...prev,
            ...held.filter((h) => !prev.some((p) => p.row.image_id === h.row.image_id)),
          ]);
      }

      if (res.destWrittenIds.length === 0 && res.sourceWrittenIds.length === 0) return;
      /* The destination gallery is not mounted, so nothing blinks — but it IS
       * cached, and inside main.tsx's 60s staleTime selecting that tag would
       * serve a gallery this write contradicted. */
      if (res.destWrittenIds.length > 0)
        qc.invalidateQueries({ queryKey: newDedupPositiveImagesKey(res.destTagId) });
      invalidateAnnotationReads();
    },
  });

  const putBackAllMut = useMutation({
    mutationFn: async (vars: { tagId: number; imageIds: number[] }) => {
      for (const c of chunkIds(vars.imageIds, BULK_ANNOTATION_MAX))
        await bulkSetNewDedupTagAnnotation(vars.tagId, c, 'positive', null);
      return vars;
    },
    onSuccess: (vars) => {
      // One setQueryData for the whole strip; the refetch is the fallback for
      // rows a tag switch already dropped, never the norm.
      if (restorePositiveRows(vars.tagId, vars.imageIds).length > 0)
        qc.invalidateQueries({ queryKey: newDedupPositiveImagesKey(vars.tagId) });
      invalidateAnnotationReads();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  // --- deleting a tag -------------------------------------------------------

  const deleteMut = useMutation({
    mutationFn: (tagId: number) => removeNewDedupTag(tagId),
    onSuccess: (res, tagId) => {
      setConfirmingDelete(false);
      setDeleteError(null);

      /* Patch, don't refetch: a 51-row VISIBLE list, and no OTHER tag's counts
       * moved. But candidate_image_count (distinct images queued for ≥1 tag)
       * can only be recomputed server-side, so the entry is flagged stale
       * WITHOUT a refetch and corrects on the next mount or focus. That
       * arithmetic is never invented here. */
      qc.setQueryData<{ data: NewDedupLabelingOverview }>(NEW_DEDUP_OVERVIEW_KEY, (old) =>
        old
          ? { ...old, data: { ...old.data, tags: old.data.tags.filter((t) => t.id !== tagId) } }
          : old,
      );
      qc.invalidateQueries({ queryKey: NEW_DEDUP_OVERVIEW_KEY, refetchType: 'none' });
      // Also a visible list (the v-chips): no chip may survive its tag.
      qc.setQueryData<{ data: TagDefinitionStatus[] }>(DEFINITIONS_KEY, (old) =>
        old ? { ...old, data: old.data.filter((s) => s.tag_id !== tagId) } : old,
      );

      /* removeQueries, not invalidate — invalidating would fire a refetch for
       * an entity that now 404s. This is what "the entity is gone" means. It
       * runs BEFORE the prefix invalidates below, so those cannot re-match the
       * dead tag's own entries. */
      qc.removeQueries({ queryKey: definitionKey(tagId) });
      qc.removeQueries({ queryKey: versionsKey(tagId) });
      qc.removeQueries({ queryKey: neighboursKey(tagId) });
      qc.removeQueries({ queryKey: newDedupPositiveImagesKey(tagId) });

      // Other tags' cached neighbour lists still name it, and would keep
      // offering a gone tag as confusable. Label-carrying reads on the other
      // page are flagged for the same reason the rename path flags them.
      qc.invalidateQueries({ queryKey: NEW_DEDUP_NEIGHBOURS_PREFIX });
      qc.invalidateQueries({ queryKey: NEW_DEDUP_IMAGE_TAGS_PREFIX });
      qc.invalidateQueries({ queryKey: NEW_DEDUP_TAG_IMAGES_KEY });
      qc.invalidateQueries({ queryKey: NEW_DEDUP_PROPOSALS_KEY });
      qc.invalidateQueries({ queryKey: NEW_DEDUP_CANDIDATES_KEY });

      /* Reset the draft through the ONE loader, and drop the selection rather
       * than auto-advancing to a neighbour — silently loading a different tag's
       * document after a destructive act is how the next edit lands on the
       * wrong one. selectTag's dirty-confirm deliberately does not apply: the
       * modal already said the unsaved changes go too. */
      loadedStamp.current = null;
      loadForm(null);
      setParams({}, { replace: true });
      pushToast(
        'ok',
        `Deleted ${res.data.label} — ${res.data.deleted_annotations} annotations went with it.`,
      );
    },
    /* Own onError, which suppresses main.tsx's global toast: the modal is still
     * open and on screen, and a toast six seconds away would be the same
     * message said twice. */
    onError: (err: Error) => setDeleteError(err.message),
  });

  // --- renaming a tag in place ---------------------------------------------

  const [renameError, setRenameError] = useState<string | null>(null);
  /* Stable by contract: the list clears the pending rename whenever selection
   * moves, and an inline arrow here would make that effect fire every render. */
  const clearRenameError = useCallback(() => setRenameError(null), []);
  const renameMut = useMutation({
    mutationFn: (vars: { tagId: number; label: string }) =>
      renameNewDedupTag(vars.tagId, vars.label),
    onSuccess: (res, vars) => {
      setRenameError(null);
      /* Patch, don't invalidate: the tag list is a 51-row VISIBLE list. And
       * merge only label+family — the rename route echoes tag_annotations'
       * _tag_dict (identity and flags only), so spreading the response would
       * wipe every count to undefined and render NaN. */
      qc.setQueryData<{ data: NewDedupLabelingOverview }>(NEW_DEDUP_OVERVIEW_KEY, (old) =>
        old
          ? {
              ...old,
              data: {
                ...old.data,
                tags: old.data.tags.map((t) =>
                  t.id === vars.tagId
                    ? { ...t, label: res.data.label, family: res.data.family }
                    : t,
                ),
              },
            }
          : old,
      );
      /* Label-carrying reads that are NOT mounted here. Marked stale so a later
       * panel open, or the Labeling page, is correct — no refetch happens now. */
      qc.invalidateQueries({ queryKey: ['new-dedup', 'labeling', 'image-tags'] });
      qc.invalidateQueries({ queryKey: NEW_DEDUP_PROPOSALS_KEY });
      qc.invalidateQueries({ queryKey: NEW_DEDUP_TAG_IMAGES_KEY });
      pushToast('ok', 'Renamed.');
    },
    /* Own onError, which suppresses main.tsx's global toast on purpose: the
     * error is field-scoped and the field is on screen and focused. A toast six
     * seconds away from the input is worse feedback, and a toast PLUS an inline
     * message is one message said twice. */
    onError: (err: Error) => setRenameError(err.message),
  });

  const addConfusable = (tagId: number) => {
    if (draft.confusable_with.some((r) => r.tag_id === tagId)) return;
    const index = draft.confusable_with.length;
    setDraft((d) => ({
      ...d,
      confusable_with: [...d.confusable_with, { tag_id: tagId, tell: '' }],
    }));
    // The one field the click can't fill in — land the cursor in it.
    setFocusConfusableIndex(index);
  };

  const toggleExample = (imageId: number) => {
    if (draft.example_image_ids.includes(imageId)) {
      setDraft((d) => ({
        ...d,
        example_image_ids: d.example_image_ids.filter((id) => id !== imageId),
      }));
      return;
    }
    if (draft.example_image_ids.length >= EXAMPLE_IMAGES_MAX) {
      pushToast('err', `At most ${EXAMPLE_IMAGES_MAX} example images per definition.`);
      return;
    }
    setDraft((d) => ({ ...d, example_image_ids: [...d.example_image_ids, imageId] }));
  };

  const versions = versionsQ.data?.data ?? [];
  const confusableIncomplete = draft.confusable_with.some(
    (r) => r.tag_id === 0 || r.tell.trim() === '',
  );
  /* A "does not count" row that names the tag a case belongs to but never says
   * WHAT the case is would be dropped on the way to the server — block the save
   * instead of losing the half of it the operator did write. A wholly blank row
   * is just an unused "+ Add" and is dropped silently. */
  const doesNotCountIncomplete = draft.does_not_count.some(
    (r) => r.case.trim() === '' && r.goes_to_tag_id != null,
  );
  const canSave =
    dirty &&
    !saveMut.isPending &&
    draft.means.trim() !== '' &&
    !confusableIncomplete &&
    !doesNotCountIncomplete;

  /* The gallery renders unconditionally and `isLoading` is false once a query
   * has ERRORED, so without this a failed /positive-images read left the page
   * saying the tag has no centroid — a data-shaped diagnosis of a transport
   * failure, on the one surface whose whole claim is showing a tag's real
   * contents. Last in the chain: an overview or definition failure is the more
   * fundamental one and still wins the banner. */
  const loadError =
    overviewQ.error ?? statusQ.error ?? definitionQ.error ?? positiveImagesQ.error;

  /* Filing images onto a retired tag would put them where list_tags_for_image
   * can no longer show them. */
  const destinationTags = useMemo(() => tags.filter((t) => t.active), [tags]);
  /* Exactly the strip the gallery renders: a row a background refetch has
   * re-listed is not "moved out" any more and must not be re-written. */
  const putBackAllIds = useMemo(
    () =>
      movedOut
        .filter((m) => !positiveRows.some((r) => r.image_id === m.row.image_id))
        .map((m) => m.row.image_id),
    [movedOut, positiveRows],
  );
  /* The back button, and any stale link: without this the page leans on
   * definitionQ's 404 banner to explain a tag that is simply gone. */
  const selectedTagGone =
    selectedTagId != null && overviewQ.data != null && tagById.get(selectedTagId) == null;

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-2xl mx-auto">
      <h1 className="text-2xl leading-tight">NEW DEDUP · Taxonomy</h1>
      <p className="mt-1 text-sm text-[var(--color-ink-2)]">
        Write what each tag means before anything is labeled against it — and use the tag's own
        images to check that the name still matches what it holds.
      </p>

      {loadError instanceof Error && <ErrorBanner message={loadError.message} />}

      <div className="mt-5 grid gap-6 lg:grid-cols-[22rem_1fr]">
        <TagDefinitionList
          tags={tags}
          status={statusByTag}
          selectedId={selectedTagId}
          onSelect={selectTag}
          loading={overviewQ.isLoading}
          onRename={(tagId, label) =>
            renameMut.mutateAsync({ tagId, label }).then(
              () => true,
              () => false,
            )
          }
          renamePending={renameMut.isPending}
          renameError={renameError}
          onRenameErrorClear={clearRenameError}
          onRequestDelete={() => {
            setDeleteError(null);
            setConfirmingDelete(true);
          }}
        />

        <div>
          {selectedTagId == null ? (
            <p className="text-sm text-[var(--color-ink-3)]">
              Pick a tag on the left to write its definition.
            </p>
          ) : selectedTagGone ? (
            <p className="text-sm text-[var(--color-ink-3)]">
              That tag no longer exists. Pick another on the left.
            </p>
          ) : (
            <>
              <section
                className={[
                  'rounded-[var(--radius-sm)] border p-4',
                  dirty ? 'border-[var(--color-copper)]' : 'border-[var(--color-rule)]',
                ].join(' ')}
              >
                <div className="flex items-baseline gap-2 flex-wrap">
                  <h2
                    className="min-w-0 font-mono text-[0.9rem] text-[var(--color-ink)]"
                    title={selectedTag?.label ?? ''}
                  >
                    {selectedTag?.label ?? `tag ${selectedTagId}`}
                  </h2>

                  {definition ? (
                    <span className="px-1.5 py-px font-mono text-[0.7rem] tabular-nums rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-3)]">
                      v{definition.version}
                    </span>
                  ) : (
                    <span className="text-[0.72rem] text-[var(--color-ink-4)]">
                      no definition yet
                    </span>
                  )}

                  {dirty && (
                    <span className="text-[0.72rem] text-[var(--color-brick)]">• unsaved</span>
                  )}

                  <div className="ml-auto flex items-center gap-1.5">
                    {versions.length > 0 && (
                      <select
                        aria-label="Definition history"
                        value={viewingVersion == null ? '' : String(viewingVersion)}
                        onChange={(e) =>
                          setViewingVersion(e.target.value === '' ? null : Number(e.target.value))
                        }
                        className="px-1.5 py-1 text-[0.72rem] font-mono rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)] text-[var(--color-ink-2)]"
                      >
                        <option value="">history…</option>
                        {versions.map((v) => (
                          <option key={v.id} value={String(v.version)}>
                            v{v.version} · {v.status} · {fmtDate(v.created_at)}
                          </option>
                        ))}
                      </select>
                    )}

                    {viewingVersion == null && (
                      <>
                        {dirty && (
                          <button
                            type="button"
                            onClick={() => setDraft(baseline)}
                            className="px-2 py-1 text-[0.72rem] rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]"
                          >
                            Discard changes
                          </button>
                        )}
                        <button
                          type="button"
                          disabled={!canSave}
                          onClick={() =>
                            saveMut.mutate({
                              tagId: selectedTagId,
                              body: toPayload(draft, loadedVersion),
                            })
                          }
                          className={[
                            'inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.72rem]',
                            'rounded-[var(--radius-xs)] border disabled:opacity-40',
                            dirty
                              ? 'border-[var(--color-copper)] bg-[var(--color-copper)] text-[var(--color-paper)]'
                              : 'border-[var(--color-rule)] text-[var(--color-ink-3)]',
                          ].join(' ')}
                        >
                          {saveMut.isPending && <Spinner />}
                          Save {loadedVersion == null ? 'v1' : `v${loadedVersion + 1}`}
                        </button>
                      </>
                    )}
                  </div>
                </div>

                {viewingVersion != null ? (
                  <div className="mt-4">
                    <div className="flex items-center gap-2 flex-wrap p-2 rounded-[var(--radius-xs)] bg-[var(--color-inset)]">
                      <span className="text-[0.72rem] text-[var(--color-ink-2)]">
                        Viewing v{viewingVersion} (
                        {versions.find((v) => v.version === viewingVersion)?.status ??
                          'superseded'}
                        ) — read only
                      </span>
                      <button
                        type="button"
                        onClick={() => setViewingVersion(null)}
                        className="ml-auto px-2 py-0.5 text-[0.72rem] rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]"
                      >
                        Back to current
                      </button>
                    </div>
                    <div className="mt-3">
                      {versionQ.isLoading && (
                        <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>
                      )}
                      {versionQ.data?.data && (
                        <DefinitionReadOnly definition={versionQ.data.data} />
                      )}
                    </div>
                  </div>
                ) : (
                  <div className="mt-4">
                    {definitionQ.isLoading ? (
                      <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>
                    ) : (
                      <>
                        {!definition && (
                          <p className="mb-3 text-[0.72rem] text-[var(--color-ink-4)]">
                            No definition yet — saving writes v1.
                          </p>
                        )}
                        <DefinitionEditor
                          draft={draft}
                          onChange={patch}
                          tags={tags}
                          subjectTagId={selectedTagId}
                          focusConfusableIndex={focusConfusableIndex}
                          onConfusableFocused={() => setFocusConfusableIndex(null)}
                        />
                        {previewCard && (
                          // The same fields, rendered the way the operator and the
                          // vision model will actually read them. The four boxes
                          // above are the storage shape; this is the meaning.
                          <div className="mt-4">
                            <DefinitionCard card={previewCard} draft={dirty} />
                          </div>
                        )}
                        {dirty && draft.means.trim() === '' && (
                          <p className="mt-2 text-[0.7rem] text-[var(--color-brick)]">
                            A definition needs its one-sentence <code>means</code> before it can
                            be saved.
                          </p>
                        )}
                        {confusableIncomplete && (
                          <p className="mt-2 text-[0.7rem] text-[var(--color-brick)]">
                            Every confusable-with row needs a tag and a tell — remove the row if
                            you can't name the difference.
                          </p>
                        )}
                        {doesNotCountIncomplete && (
                          <p className="mt-2 text-[0.7rem] text-[var(--color-brick)]">
                            A does-not-count row that names a tag needs the case it applies to —
                            write it, or remove the row.
                          </p>
                        )}
                      </>
                    )}
                  </div>
                )}
              </section>

              {viewingVersion == null && (
                <>
                  <TagContentsGallery
                    rows={positiveRows}
                    photos={photos}
                    exampleIds={draft.example_image_ids}
                    onToggleExample={toggleExample}
                    loading={positiveImagesQ.isLoading}
                    limit={POSITIVE_IMAGE_LIMIT}
                    exampleLimit={EXAMPLE_IMAGES_MAX}
                    onOpenTags={setDetailImageId}
                    movedOut={movedOut}
                    onPutBack={(imageId) =>
                      putBackMut.mutate({ tagId: selectedTagId, imageId })
                    }
                    putBackPending={putBackPending}
                    subjectTagId={selectedTagId}
                    subjectLabel={selectedTag?.label ?? `tag ${selectedTagId}`}
                    destinationTags={destinationTags}
                    selecting={selecting}
                    onEnterSelection={() => setSelecting(true)}
                    onLeaveSelection={() => {
                      setSelecting(false);
                      setSelectedImageIds(new Set());
                      setBatchResult(null);
                    }}
                    selectedIds={effectiveSelectedIds}
                    onToggleSelect={toggleSelectImage}
                    onSelectAll={() =>
                      setSelectedImageIds(new Set(positiveRows.map((r) => r.image_id)))
                    }
                    onClearSelection={() => setSelectedImageIds(new Set())}
                    onBatchFile={(req) =>
                      batchMut.mutate({ ...req, sourceTagId: selectedTagId })
                    }
                    batchPending={batchMut.isPending}
                    batchWritten={batchWritten}
                    batchResult={batchResult}
                    onDismissBatchResult={() => setBatchResult(null)}
                    onPutBackAll={() =>
                      putBackAllMut.mutate({
                        tagId: selectedTagId,
                        imageIds: putBackAllIds,
                      })
                    }
                    putBackAllPending={putBackAllMut.isPending}
                    order={effectiveOrder}
                    onOrderChange={setContentsOrder}
                    outlierApplied={outlierApplied}
                    centroidPositives={positivesRes?.centroid_positives ?? null}
                    minPositives={positivesRes?.min_positives ?? MIN_POSITIVES_FOR_CENTROID}
                  />
                  <OverlapEvidence
                    neighbours={neighboursQ.data?.data ?? []}
                    tagById={tagById}
                    listedIds={draft.confusable_with.map((r) => r.tag_id)}
                    onAddConfusable={addConfusable}
                    loading={neighboursQ.isLoading}
                    minPositives={MIN_POSITIVES_FOR_CENTROID}
                  />
                  <DefinitionNotes
                    notes={notesQ.data?.data ?? []}
                    loading={notesQ.isLoading}
                    activeDefinitionId={definition?.id ?? null}
                    activeVersion={definition?.version ?? null}
                    absorbing={absorbMut.isPending}
                    onAbsorb={(noteIds) => {
                      if (selectedTagId == null || definition == null) return;
                      absorbMut.mutate({
                        tagId: selectedTagId, definitionId: definition.id, noteIds,
                      });
                    }}
                  />
                </>
              )}
            </>
          )}
        </div>
      </div>

      {confirmingDelete && selectedTag != null && (
        <TagDeleteConfirm
          tag={selectedTag}
          definitionVersion={statusByTag.get(selectedTag.id)?.version ?? null}
          savedVersionCount={versionsQ.data ? versions.length : null}
          hasUnsavedDraft={dirty}
          onCancel={() => {
            setConfirmingDelete(false);
            setDeleteError(null);
          }}
          onConfirm={() => deleteMut.mutate(selectedTag.id)}
          pending={deleteMut.isPending}
          error={deleteError}
        />
      )}

      {detailImageId != null && (
        <ImageTagDetailPanel
          imageId={detailImageId}
          onClose={() => setDetailImageId(null)}
          onTagStateChange={onImageTagChange}
          subjectTagId={selectedTagId}
        />
      )}
    </div>
  );
}
