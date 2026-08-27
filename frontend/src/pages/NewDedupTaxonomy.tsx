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
  saveTagDefinition,
  type NewDedupTag,
  type SaveTagDefinitionIn,
  type TagDefinition,
  type TagDefinitionStatus,
} from '@/lib/api';
import { fetchImagesByImageIds } from '@/lib/queries';
import { pushToast } from '@/lib/toast';
import Spinner from '@/components/Spinner';
import DefinitionEditor, {
  EMPTY_DRAFT,
  type Draft,
} from '@/components/tag-definitions/DefinitionEditor';
import DefinitionReadOnly from '@/components/tag-definitions/DefinitionReadOnly';
import OverlapEvidence from '@/components/tag-definitions/OverlapEvidence';
import TagContentsGallery from '@/components/tag-definitions/TagContentsGallery';
import TagDefinitionList from '@/components/tag-definitions/TagDefinitionList';
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
 * There are no drafts server-side: one Save = one version. So every edit —
 * text, picker, gallery click, "add to confusable" — stages into local state
 * and the page makes exactly ONE network write. */

const OVERVIEW_KEY = ['new-dedup', 'labeling', 'overview'];
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
const positiveImagesKey = (tagId: number) => ['new-dedup', 'labeling', 'positive-images', tagId];
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

  const overviewQ = useQuery({ queryKey: OVERVIEW_KEY, queryFn: getNewDedupLabelingOverview });
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
    queryKey: positiveImagesKey(selectedTagId ?? 0),
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
