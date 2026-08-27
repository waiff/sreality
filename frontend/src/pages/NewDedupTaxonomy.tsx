import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  getNewDedupLabelingOverview,
  getTagDefinition,
  getTagDefinitionVersion,
  listTagDefinitionStatus,
  listTagDefinitionVersions,
  listTagNeighbours,
  listTagPositiveImages,
  renameNewDedupTag,
  saveTagDefinition,
  setNewDedupTagAnnotation,
  type NewDedupImageTag,
  type NewDedupLabelingOverview,
  type NewDedupTag,
  type SaveTagDefinitionIn,
  type TagDefinition,
  type TagDefinitionStatus,
  type TagPositiveImage,
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
import OverlapEvidence from '@/components/tag-definitions/OverlapEvidence';
import TagContentsGallery, {
  type MovedOutImage,
} from '@/components/tag-definitions/TagContentsGallery';
import TagDefinitionList from '@/components/tag-definitions/TagDefinitionList';
import ImageTagDetailPanel, {
  type ImageTagChange,
} from '@/components/tag-annotations/ImageTagDetailPanel';
import {
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
const photosKey = (ids: string) => ['new-dedup', 'taxonomy', 'photos', ids];

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
    queryFn: () => listTagPositiveImages(selectedTagId as number, POSITIVE_IMAGE_LIMIT),
    enabled: selectedTagId != null,
  });
  const neighboursQ = useQuery({
    queryKey: neighboursKey(selectedTagId ?? 0),
    queryFn: () => listTagNeighbours(selectedTagId as number, NEIGHBOUR_LIMIT),
    enabled: selectedTagId != null,
  });

  const positiveRows = useMemo(() => positiveImagesQ.data?.data ?? [], [positiveImagesQ.data]);

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
  useEffect(() => {
    setMovedOut([]);
    setDetailImageId(null);
  }, [selectedTagId]);

  /* Put the row back where it was, not at the top. The server orders by
   * `updated_at DESC` and a put-back genuinely moves the row, so it WILL float
   * to the front on the next natural refetch — converging then is honest;
   * reshuffling the grid under the operator's cursor mid-sitting is the churn
   * both pages are built to avoid. */
  /* Returns whether the patch was possible at all: the receipt strip is
   * session-local and a tag switch empties it, so a put-back that resolves
   * after one has no row to splice back and the caller has to repair the cache
   * some other way. */
  const restorePositiveRow = useCallback(
    (tagId: number, imageId: number): boolean => {
      const held = movedOutRef.current.find((m) => m.row.image_id === imageId);
      setMovedOut((prev) => prev.filter((m) => m.row.image_id !== imageId));
      if (!held) return false;
      qc.setQueryData<{ data: TagPositiveImage[] }>(
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
      const cached = qc.getQueryData<{ data: TagPositiveImage[] }>(key);
      const index = cached?.data.findIndex((r) => r.image_id === c.imageId) ?? -1;
      const row = index >= 0 ? cached?.data[index] : undefined;
      if (row) {
        qc.setQueryData<{ data: TagPositiveImage[] }>(key, (old) =>
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

  const loadError = overviewQ.error ?? statusQ.error ?? definitionQ.error;

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
        />

        <div>
          {selectedTagId == null ? (
            <p className="text-sm text-[var(--color-ink-3)]">
              Pick a tag on the left to write its definition.
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
                  />
                  <OverlapEvidence
                    neighbours={neighboursQ.data?.data ?? []}
                    tagById={tagById}
                    listedIds={draft.confusable_with.map((r) => r.tag_id)}
                    onAddConfusable={addConfusable}
                    loading={neighboursQ.isLoading}
                    minPositives={MIN_POSITIVES_FOR_CENTROID}
                  />
                </>
              )}
            </>
          )}
        </div>
      </div>

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
