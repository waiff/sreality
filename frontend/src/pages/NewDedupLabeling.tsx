import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getNewDedupLabelingOverview,
  addNewDedupTaxonomyLabel,
  renameNewDedupTaxonomyLabel,
  removeNewDedupTaxonomyLabel,
  growNewDedupSample,
  listNewDedupProposals,
  confirmNewDedupProposal,
  dismissNewDedupProposal,
  bulkConfirmNewDedupProposals,
  bulkDismissNewDedupProposals,
  listNewDedupSettings,
  setTrainingExample,
  type NewDedupTaxonomyLabel,
  type NewDedupLabelProposal,
} from '@/lib/api';
import { fetchImagesByImageIds } from '@/lib/queries';
import { imageSrc } from '@/lib/imageUrl';
import { pushToast } from '@/lib/toast';
import Tabs from '@/components/Tabs';
import ImageTagBadge from '@/components/ImageTagBadge';
import Spinner from '@/components/Spinner';
import TaxonomyManageModal from '@/components/TaxonomyManageModal';
import LabelCombobox, { type LabelOption } from '@/components/LabelCombobox';
import { Chevron, useCollapsed } from '@/components/settings/SectionChrome';
import { CATEGORY_MAIN_TABS } from '@/lib/categoryMainTabs';
import type { ImagePublic } from '@/lib/types';

const OVERVIEW_KEY = ['new-dedup', 'labeling', 'overview'];
const PROPOSALS_KEY = ['new-dedup', 'labeling', 'proposals'];
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

type ProposalsPage = { data: NewDedupLabelProposal[] };

export default function NewDedupLabeling() {
  const qc = useQueryClient();
  const overviewQ = useQuery({ queryKey: OVERVIEW_KEY, queryFn: getNewDedupLabelingOverview });
  const settingsQ = useQuery({ queryKey: SETTINGS_KEY, queryFn: listNewDedupSettings });

  const [labelFilter, setLabelFilter] = useState<string | null>(null);
  // Coverage ceiling for the TAG list (not the images): with dozens of labels,
  // "which tags are still short of Gate 1" is the question that decides what to
  // label next, and the full chart buries it. '' = no ceiling.
  const [maxTrained, setMaxTrained] = useState('');
  const [manageOpen, setManageOpen] = useState(false);
  const [tab, setTab] = useState<TabKey>('pending');
  const [showOriginal, setShowOriginal] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  // confirmMut/dismissMut below are ONE shared mutation instance for the
  // whole grid — TanStack Query's observer only ever reflects the MOST
  // RECENT .mutate() call's isPending/variables, so a second tile's click
  // would make an earlier tile's still-in-flight action look finished.
  // Track per-tile pending state locally instead, keyed by row (image, model)
  // so one model's in-flight action can't disable another model's tile for
  // the same image on the All tab.
  const [pendingRowKeys, setPendingRowKeys] = useState<ReadonlySet<string>>(new Set());
  // Staged tag edits, held HERE rather than inside the tile: the batch
  // actions below send image ids only (the bulk endpoint takes no per-image
  // labels), so the page has to know which tiles carry an unsaved correction
  // in order to keep them out of a batch — otherwise "Confirm selected" would
  // silently write the model's label over the operator's fix. Page-level also
  // survives a background refetch replacing the proposals array mid-edit.
  // Keyed by (image_id, model), matching label_proposals' own PK: one image
  // can carry proposals from several models, and they must not share a slot.
  const [drafts, setDrafts] = useState<ReadonlyMap<string, string>>(new Map());
  const draftKey = (p: NewDedupLabelProposal) => `${p.image_id}:${p.model}`;

  const proposalsKey = useMemo(() => [...PROPOSALS_KEY, tab, labelFilter], [tab, labelFilter]);
  const proposalsQ = useQuery({
    queryKey: proposalsKey,
    queryFn: () =>
      listNewDedupProposals({ status: tab, label: labelFilter ?? undefined, limit: 200 }),
  });
  const proposals = useMemo(() => proposalsQ.data?.data ?? [], [proposalsQ.data]);
  const imageIds = useMemo(() => proposals.map((p) => p.image_id), [proposals]);

  // Photos ACCUMULATE across tabs and actions rather than being re-fetched per
  // grid. Keying the image query on the current id list meant every confirm —
  // which changes that list — swapped in an empty cache entry, so every tile in
  // the grid lost its photo and re-rendered at once. Only never-seen ids are
  // ever requested; a tile that's already on screen never blinks again.
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

  useEffect(() => {
    setSelected(new Set());
    setDrafts(new Map());
  }, [tab, labelFilter]);
  useEffect(() => {
    const ids = new Set(imageIds);
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => ids.has(id)));
      return next.size === prev.size ? prev : next;
    });
    setDrafts((prev) => {
      const next = new Map(
        [...prev].filter(([key]) => ids.has(Number(key.split(':')[0]))),
      );
      return next.size === prev.size ? prev : next;
    });
  }, [imageIds]);

  const settingValue = (key: string) => settingsQ.data?.data.find((s) => s.key === key)?.value;
  const gate1Target = (settingValue('labeling_gate1_target_per_tag') as number) ?? 150;
  const proposalTarget = (settingValue('labeling_target_proposals_per_category') as number) ?? 300;
  const secondaryModel = settingValue('labeling_secondary_model') as string | undefined;

  const invalidateOverview = () => qc.invalidateQueries({ queryKey: OVERVIEW_KEY });
  // A taxonomy rename/remove rewrites labels across EVERY tab (it cascades in
  // one transaction server-side), so that one really does have to refetch the
  // visible grid — unlike reviewing a single proposal, which patches in place.
  const invalidateProposals = () => qc.invalidateQueries({ queryKey: PROPOSALS_KEY });
  // Only the tabs the operator ISN'T looking at. Invalidating the visible one
  // would refetch it and re-render every tile — the churn this page is built to
  // avoid (see patchRows). The others are marked stale so they're correct the
  // moment they're opened; none of them is mounted, so nothing refetches now.
  const invalidateOtherTabs = () =>
    qc.invalidateQueries({
      predicate: (q) => {
        const k = q.queryKey as unknown[];
        return (
          k[0] === 'new-dedup' && k[1] === 'labeling' && k[2] === 'proposals' && k[3] !== tab
        );
      },
    });

  /* The review grid is patched IN PLACE, never invalidated. A refetch re-runs
   * the query and re-renders every tile, which on a page you work through
   * image-by-image reads as "the whole grid jumped". `patch` is either 'drop'
   * (the row belongs to another tab now) or a mapper over the row. */
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

  // On Pending a reviewed row leaves for its new tab (the others keep their
  // order); anywhere else it stays exactly where it is and greys out.
  const applyReview = (
    ids: ReadonlyArray<number>,
    model: string,
    patch: (p: NewDedupLabelProposal) => NewDedupLabelProposal,
  ) => {
    patchRows(ids, model, tab === 'pending' ? 'drop' : patch);
    // A tile reviewed through its OWN button while checkbox-selected has to
    // leave the selection too. On Pending the row is gone and the imageIds
    // effect prunes it, but on All it stays on screen — and a later "Confirm
    // selected" would then re-send an id that's no longer pending.
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => !ids.includes(id)));
      return next.size === prev.size ? prev : next;
    });
    invalidateOtherTabs();
    invalidateOverview();
  };

  // The taxonomy IS the option list for correcting a wrong suggestion —
  // same shape the /clip-audit combobox uses (label + its current training
  // count), so a mis-tagged proposal is fixed by picking the right tag
  // rather than dismissing and re-labelling elsewhere. Free text still
  // creates a new label, matching that page. Deliberately the WHOLE
  // vocabulary, never narrowed by the coverage ceiling below: that filter
  // picks what to work on, it doesn't restrict what a tag can be corrected to.
  const allLabels = useMemo(() => overviewQ.data?.data.labels ?? [], [overviewQ.data]);
  const labelOptions: LabelOption[] = useMemo(
    () =>
      allLabels
        .map((l) => ({ value: l.label, label: l.label, count: l.confirmed_count }))
        .sort((a, b) => a.label.localeCompare(b.label, 'cs')),
    [allLabels],
  );

  const maxTrainedNum =
    maxTrained.trim() === '' || !Number.isFinite(Number(maxTrained)) || Number(maxTrained) < 0
      ? null
      : Number(maxTrained);
  const visibleLabels = useMemo(
    () =>
      maxTrainedNum == null
        ? allLabels
        : allLabels.filter((l) => l.confirmed_count <= maxTrainedNum),
    [allLabels, maxTrainedNum],
  );
  const filterOptions = useMemo(
    () => [...visibleLabels].sort((a, b) => a.label.localeCompare(b.label, 'cs')),
    [visibleLabels],
  );

  // --- taxonomy ---------------------------------------------------------

  const [newLabelText, setNewLabelText] = useState('');
  const addLabelMut = useMutation({
    mutationFn: () => addNewDedupTaxonomyLabel(newLabelText.trim()),
    onSuccess: () => {
      setNewLabelText('');
      pushToast('ok', 'Label added.');
      invalidateOverview();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  const renameLabelMut = useMutation({
    mutationFn: ({ id, label }: { id: number; oldLabel: string; label: string }) =>
      renameNewDedupTaxonomyLabel(id, label),
    onSuccess: (_res, vars) => {
      pushToast('ok', 'Renamed.');
      // Only follow the filter if it was pointed at THIS label — renaming an
      // unrelated row must never hijack whichever label the operator has
      // the proposals grid currently filtered to.
      setLabelFilter((cur) => (cur === vars.oldLabel ? vars.label : cur));
      invalidateOverview();
      invalidateProposals();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  const removeLabelMut = useMutation({
    mutationFn: ({ id }: { id: number; oldLabel: string }) => removeNewDedupTaxonomyLabel(id),
    onSuccess: (_res, vars) => {
      pushToast('ok', 'Removed.');
      // Clear the filter if it was pointed at the label just deleted —
      // otherwise the banner keeps naming a label that no longer exists.
      setLabelFilter((cur) => (cur === vars.oldLabel ? null : cur));
      invalidateOverview();
      invalidateProposals();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  // --- sample -------------------------------------------------------------

  const [growCount, setGrowCount] = useState('200');
  const [growCategory, setGrowCategory] = useState('');
  // The API's count field is an int (Pydantic) — a fractional value like
  // "2.5" would 422 server-side with an unreadable toast, so validate
  // integer-ness client-side too, not just positivity.
  const growCountValid = Number.isInteger(Number(growCount)) && Number(growCount) > 0;
  // A toast alone is easy to miss on a slow connection or a big count (1000+
  // candidate images still take a real network round-trip) — the operator
  // reported clicking "Grow sample" and seeing nothing happen. Surface the
  // result inline, next to the button itself, not just in a corner toast.
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

  const rowKey = (imageId: number, model: string) => `${imageId}:${model}`;
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

  const confirmMut = useMutation({
    mutationFn: ({
      imageId,
      model,
      label,
    }: {
      imageId: number;
      model: string;
      label?: string;
    }) => confirmNewDedupProposal(imageId, model, label),
    onSuccess: (res, vars) => {
      if (res.data.corrected) {
        pushToast('ok', `Corrected to “${res.data.label}”.`);
      }
      clearDraft(vars.imageId, vars.model);
      applyReview([vars.imageId], vars.model, (p) => ({
        ...p,
        status: 'confirmed',
        label: res.data.label,
        trained_label: res.data.label,
        reviewed_by: 'operator',
      }));
    },
    onError: (err: Error) => pushToast('err', err.message),
    onSettled: (_data, _err, vars) => endAction(vars.imageId, vars.model),
  });

  // Relabelling an image that's ALREADY in the training set is a plain
  // image_training_examples upsert — the same endpoint /clip-audit's Train
  // CTA uses. It deliberately does NOT touch label_proposals: the proposal
  // row records what the model predicted, and the Confirmed tab reads the
  // label live from image_training_examples, so this shows up immediately.
  const relabelMut = useMutation({
    mutationFn: ({ imageId, label }: { imageId: number; model: string; label: string }) =>
      setTrainingExample({ image_id: imageId, label }),
    onSuccess: (res, vars) => {
      pushToast('ok', `Relabelled to “${res.data.label}”.`);
      clearDraft(vars.imageId, vars.model);
      // Stays on whichever tab it's on — it was already confirmed, only its
      // text changed — so this patches in place even on Pending.
      patchRows([vars.imageId], vars.model, (p) => ({
        ...p,
        label: res.data.label,
        trained_label: res.data.label,
      }));
      invalidateOtherTabs();
      invalidateOverview();
    },
    onError: (err: Error) => pushToast('err', err.message),
    onSettled: (_data, _err, vars) => endAction(vars.imageId, vars.model),
  });
  const dismissMut = useMutation({
    mutationFn: ({ imageId, model }: { imageId: number; model: string }) =>
      dismissNewDedupProposal(imageId, model),
    onSuccess: (_res, vars) => {
      applyReview([vars.imageId], vars.model, (p) => ({
        ...p,
        status: 'dismissed',
        reviewed_by: 'operator',
      }));
    },
    onError: (err: Error) => pushToast('err', err.message),
    onSettled: (_data, _err, vars) => endAction(vars.imageId, vars.model),
  });
  const bulkConfirmMut = useMutation({
    mutationFn: (model: string) => bulkConfirmNewDedupProposals(model, [...selected]),
    onSuccess: (res, model) => {
      pushToast('ok', `Confirmed ${res.data.confirmed}.`);
      setSelected(new Set());
      // Each row keeps its OWN label (the batch writes per-proposal labels,
      // it isn't a relabel-everything-to-one-value action).
      applyReview(res.data.image_ids, model, (p) => ({
        ...p,
        status: 'confirmed',
        trained_label: p.label,
        reviewed_by: 'operator',
      }));
    },
    onError: (err: Error) => pushToast('err', err.message),
  });
  const bulkDismissMut = useMutation({
    mutationFn: (model: string) => bulkDismissNewDedupProposals(model, [...selected]),
    onSuccess: (res, model) => {
      pushToast('ok', `Dismissed ${res.data.dismissed}.`);
      setSelected(new Set());
      applyReview(res.data.image_ids, model, (p) => ({
        ...p,
        status: 'dismissed',
        reviewed_by: 'operator',
      }));
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
  // Editing a tag takes that tile out of the batch: the batch endpoint writes
  // each proposal's OWN label, so leaving a corrected tile selected would
  // discard the correction without telling anyone. It gets confirmed through
  // its own button instead.
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
  // older model's leftover pending rows (from before a model config change)
  // still review one at a time via the per-tile buttons. Row-level, not
  // tab-level, so the batch bar works on All too. Corrected tiles are excluded
  // for the reason above.
  const selectableIds = useMemo(
    () =>
      secondaryModel
        ? proposals
            .filter(
              (p) => p.status === 'pending' && p.model === secondaryModel && !isCorrected(p),
            )
            .map((p) => p.image_id)
        : [],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [proposals, secondaryModel, drafts],
  );
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));

  return (
    <div className="px-6 py-12 max-w-5xl mx-auto">
      <h1
        className="text-[1.6rem] leading-tight"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        NEW DEDUP · Labeling
      </h1>
      <p className="mt-3 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-2xl">
        Build the Taxonomy v1 training set: a secondary, stronger CLIP encoder proposes tags
        for images in the sample below; confirming a proposal writes it into the real training
        set, dismissing it doesn't. Gate 1 needs {gate1Target} confirmed images per active tag.
      </p>

      {overviewQ.error && <ErrorBanner message={(overviewQ.error as Error).message} />}

      <TaxonomyBarChart
        labels={visibleLabels}
        totalLabels={allLabels.length}
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
          labels={allLabels}
          onClose={() => setManageOpen(false)}
          newLabelText={newLabelText}
          onNewLabelTextChange={setNewLabelText}
          onAdd={() => addLabelMut.mutate()}
          addPending={addLabelMut.isPending}
          onRename={(id, oldLabel, label) => renameLabelMut.mutate({ id, oldLabel, label })}
          renamePending={renameLabelMut.isPending}
          onRemove={(id, oldLabel) => removeLabelMut.mutate({ id, oldLabel })}
          removePending={removeLabelMut.isPending}
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
          Adds newest not-yet-sampled images to the pool the relabel job scores — this only grows
          membership, it does NOT run scoring itself and no new pending proposals appear here yet.
          Scoring runs separately via the "NEW DEDUP — Labeling secondary-CLIP proposals" GitHub
          Actions workflow (model: {secondaryModel ?? '…'}).
        </p>
      </section>

      <section className="mt-8">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <Tabs tabs={STATUS_TABS} active={tab} onChange={setTab} />
          <div className="flex items-center gap-1">
            <ToggleButton active={!showOriginal} onClick={() => setShowOriginal(false)}>
              New tag
            </ToggleButton>
            <ToggleButton active={showOriginal} onClick={() => setShowOriginal(true)}>
              Original tag
            </ToggleButton>
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
            <option value="">All tags</option>
            {/* The current filter always stays selectable, even when the
              * coverage ceiling would hide it — otherwise the select would
              * read "All tags" while the grid stayed filtered. */}
            {labelFilter && !filterOptions.some((l) => l.label === labelFilter) && (
              <option value={labelFilter}>{labelFilter}</option>
            )}
            {filterOptions.map((l) => (
              <option key={l.id} value={l.label}>
                {l.label} ({l.confirmed_count})
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
              {filterOptions.length} of {allLabels.length} tags (≤ {maxTrainedNum} training
              images)
            </span>
          )}
        </div>

        {selectableIds.length > 0 && (
          <div className="mt-3 flex items-center gap-3 flex-wrap">
            <button
              type="button"
              onClick={() =>
                setSelected(allSelected ? new Set() : new Set(selectableIds))
              }
              className="px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]"
            >
              {allSelected ? 'Deselect all' : 'Select all'}
            </button>
            <span className="text-xs text-[var(--color-ink-3)]">{selected.size} selected</span>
            <button
              type="button"
              disabled={selected.size === 0 || bulkConfirmMut.isPending || !secondaryModel}
              onClick={() => secondaryModel && bulkConfirmMut.mutate(secondaryModel)}
              className="px-2.5 py-1 text-xs rounded-[var(--radius-xs)] bg-[var(--color-sage-soft)] text-[var(--color-sage)] disabled:opacity-40"
            >
              Confirm selected
            </button>
            <button
              type="button"
              disabled={selected.size === 0 || bulkDismissMut.isPending || !secondaryModel}
              onClick={() => secondaryModel && bulkDismissMut.mutate(secondaryModel)}
              className="px-2.5 py-1 text-xs rounded-[var(--radius-xs)] bg-[var(--color-brick-soft)] text-[var(--color-brick)] disabled:opacity-40"
            >
              Dismiss selected
            </button>
          </div>
        )}

        {proposalsQ.error && <ErrorBanner message={(proposalsQ.error as Error).message} />}
        {!proposalsQ.data && !proposalsQ.error && (
          <p className="mt-6 text-sm text-[var(--color-ink-3)]">Loading proposals…</p>
        )}
        {proposalsQ.data && proposals.length === 0 && (
          <p className="mt-6 text-sm text-[var(--color-ink-3)]">
            {`No ${tab === 'all' ? '' : `${tab} `}proposals.`}
          </p>
        )}

        <div className="mt-4 grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
          {proposals.map((p) => (
            <ProposalTile
              key={`${p.image_id}:${p.model}`}
              proposal={p}
              image={imageCache.get(p.image_id)}
              showOriginal={showOriginal}
              selectable={
                p.status === 'pending' && p.model === secondaryModel && !isCorrected(p)
              }
              selected={selected.has(p.image_id)}
              onToggleSelect={() => toggle(p.image_id)}
              labelOptions={labelOptions}
              draft={draftFor(p)}
              onDraftChange={(label) => setDraft(p, label)}
              corrected={isCorrected(p)}
              // On the mixed tab, anything already dealt with — tagged into the
              // training set, or dismissed — recedes, so the bright tiles are
              // exactly what's still waiting on the operator.
              dimmed={tab === 'all' && (p.trained_label != null || p.status !== 'pending')}
              onConfirm={(label) => {
                beginAction(p.image_id, p.model);
                confirmMut.mutate({
                  imageId: p.image_id,
                  model: p.model,
                  // Only an actual correction travels: an untouched Confirm
                  // sends no label, so the server uses the proposal's CURRENT
                  // stored label rather than whatever this (possibly stale)
                  // page last rendered — a taxonomy rename in between must
                  // not be undone by echoing the old spelling back.
                  label: isCorrected(p) ? label : undefined,
                });
              }}
              onDismiss={() => {
                beginAction(p.image_id, p.model);
                dismissMut.mutate({ imageId: p.image_id, model: p.model });
              }}
              onRelabel={(label) => {
                beginAction(p.image_id, p.model);
                relabelMut.mutate({ imageId: p.image_id, model: p.model, label });
              }}
              actionPending={pendingRowKeys.has(rowKey(p.image_id, p.model))}
            />
          ))}
        </div>
      </section>
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

/* Ranked horizontal bar chart, most confirmed training images first — the
 * training set IS the point of this page, so its size per label is the
 * primary view; managing the vocabulary (add/rename/remove) moves to the
 * "Modify labels" modal, sorted alphabetically instead (see
 * TaxonomyManageModal). Single hue (sage = confirmed/good, matching the old
 * per-row progress fill); the currently-filtered label switches to the
 * app's accent (copper) — color follows the one selected entity, not rank.
 *
 * Collapsible (persisted, same localStorage scheme as the settings pages'
 * sections) because with a full taxonomy the chart pushes the review grid off
 * screen. Its header stays visible when folded, so the coverage ceiling — which
 * also drives the grid's tag filter — is always reachable. */
function TaxonomyBarChart({
  labels,
  totalLabels,
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
  labels: NewDedupTaxonomyLabel[];
  totalLabels: number;
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
  const sorted = useMemo(
    () => [...labels].sort((a, b) => b.confirmed_count - a.confirmed_count),
    [labels],
  );
  // Bars scale to whichever is bigger — the Gate 1 target or the current
  // leader — so the target tick is always on-chart, not off the right edge.
  const domainMax = Math.max(gate1Target, ...sorted.map((l) => l.confirmed_count), 1);
  const gatePct = Math.min(100, (gate1Target / domainMax) * 100);
  const filtered = labels.length !== totalLabels;

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
            Taxonomy v1 ({filtered ? `${labels.length} of ${totalLabels}` : totalLabels} labels
            {sampleSize != null ? `, ${sampleSize} sampled` : ''})
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-2">
          <label
            className="flex items-center gap-1.5 text-xs text-[var(--color-ink-3)]"
            title="Show only tags that have at most this many confirmed training images — the ones still short of Gate 1. Also narrows the tag filter below."
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
                : 'No labels yet — add the first one via "Modify labels".'}
            </p>
          )}

          {sorted.length > 0 && (
            <>
              <p className="text-[0.7rem] text-[var(--color-ink-4)]">
                Bars are confirmed training images, scaled to Gate 1's target of {gate1Target}{' '}
                (marked ▏below).
              </p>
              <div className="mt-3 space-y-2">
                {sorted.map((l) => {
                  const pct = Math.min(100, (l.confirmed_count / domainMax) * 100);
                  const active = activeLabel === l.label;
                  return (
                    <div key={l.id}>
                      <div className="flex items-baseline gap-1.5 min-w-0">
                        <button
                          type="button"
                          onClick={() => onFilter(l.label)}
                          title="Filter proposals to this label"
                          className={[
                            'min-w-0 truncate font-mono text-[0.76rem] hover:text-[var(--color-copper-2)]',
                            active ? 'text-[var(--color-copper)]' : 'text-[var(--color-ink-2)]',
                          ].join(' ')}
                        >
                          {l.label}
                        </button>
                        {l.pending_count > 0 && (
                          <span className="shrink-0 text-[0.68rem] text-[var(--color-ink-4)]">
                            · {l.pending_count}/{proposalTarget} pending
                          </span>
                        )}
                      </div>
                      <div className="mt-1 flex items-center gap-2">
                        <div className="relative h-3.5 flex-1 rounded-[var(--radius-xs)] bg-[var(--color-rule-soft)] overflow-hidden">
                          <div
                            className={[
                              'h-full rounded-r-[var(--radius-sm)] transition-[width]',
                              active ? 'bg-[var(--color-copper)]' : 'bg-[var(--color-sage)]',
                            ].join(' ')}
                            style={{ width: `${pct}%` }}
                            aria-hidden
                          />
                          <div
                            className="absolute top-0 bottom-0 w-px bg-[var(--color-ink)]/40"
                            // `left: N%` at N=100 places the whole 1px line just past
                            // the track's right edge, invisible under overflow-hidden
                            // (confirmed visually — the leader's bar hits the target
                            // and the tick vanished). Inset by the line's own width so
                            // it stays on-screen at every position, including 100%.
                            style={{ left: `calc(${gatePct}% - 1px)` }}
                            aria-hidden
                          />
                        </div>
                        <span className="w-8 shrink-0 text-right font-mono text-[0.7rem] tabular-nums text-[var(--color-ink-3)]">
                          {l.confirmed_count}
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

function ProposalTile({
  proposal,
  image,
  showOriginal,
  selectable,
  selected,
  onToggleSelect,
  labelOptions,
  draft,
  onDraftChange,
  corrected,
  dimmed,
  onConfirm,
  onDismiss,
  onRelabel,
  actionPending,
}: {
  proposal: NewDedupLabelProposal;
  image: ImagePublic | undefined;
  showOriginal: boolean;
  selectable: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  labelOptions: LabelOption[];
  /** The tag the operator intends for this image — seeded from the
   * suggestion, owned by the page (see the `drafts` map there). */
  draft: string;
  onDraftChange: (label: string) => void;
  corrected: boolean;
  dimmed: boolean;
  onConfirm: (label: string) => void;
  onDismiss: () => void;
  onRelabel: (label: string) => void;
  actionPending: boolean;
}) {
  const badgeTag = showOriginal ? image?.clip_fine_tag ?? null : proposal.label;
  const badgeConfidence = showOriginal ? image?.clip_confidence ?? null : proposal.confidence;

  const changed = corrected;
  const isDismissed = proposal.status === 'dismissed';
  // Row-level, not tab-level: on the All tab a pending row still gets its
  // Confirm/Dismiss buttons while its already-reviewed neighbours don't.
  const reviewed = proposal.status !== 'pending';

  // NB: the card must NOT be overflow-hidden — the tag picker's dropdown is
  // absolutely positioned and would be clipped away by it. Only the photo is
  // clipped (to round its top corners).
  return (
    <div
      className={[
        'border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)] transition-opacity',
        // Still fully interactive — hovering brings it back, so a greyed tile
        // can be re-tagged without leaving the tab.
        dimmed ? 'opacity-45 hover:opacity-100 focus-within:opacity-100' : '',
      ].join(' ')}
      data-dimmed={dimmed || undefined}
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
          <img
            src={imageSrc(image)}
            alt=""
            loading="lazy"
            className="absolute inset-0 h-full w-full object-cover"
          />
        )}
        <ImageTagBadge
          tag={badgeTag}
          confidence={badgeConfidence}
          className="absolute bottom-1.5 left-1.5"
        />
      </div>

      <div className="px-2 py-1.5 flex items-center justify-between gap-1">
        {!reviewed ? (
          <>
            <button
              type="button"
              onClick={() => onConfirm(draft)}
              disabled={actionPending || draft.trim() === ''}
              title={
                changed
                  ? `Confirm as “${draft}” instead of the suggested “${proposal.label}”`
                  : undefined
              }
              // Same text either way — the label can't grow, so it still fits
              // the narrowest (2-column) tile. Copper marks "this writes your
              // correction, not the suggestion".
              className={[
                'min-w-0 flex-1 truncate px-1.5 py-1 text-[0.7rem] rounded-[var(--radius-xs)] disabled:opacity-40',
                changed
                  ? 'bg-[var(--color-copper)] text-[var(--color-paper)]'
                  : 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]',
              ].join(' ')}
            >
              Confirm
            </button>
            <button
              type="button"
              onClick={onDismiss}
              disabled={actionPending}
              className="flex-1 px-1.5 py-1 text-[0.7rem] rounded-[var(--radius-xs)] bg-[var(--color-brick-soft)] text-[var(--color-brick)] disabled:opacity-40"
            >
              Dismiss
            </button>
          </>
        ) : changed ? (
          // Already in the training set — an edited tag saves in place rather
          // than going back through the confirm flow.
          <>
            <button
              type="button"
              onClick={() => onRelabel(draft)}
              disabled={actionPending}
              className="flex-1 px-1.5 py-1 text-[0.7rem] rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-40"
            >
              Save tag
            </button>
            <button
              type="button"
              onClick={() => onDraftChange(proposal.label)}
              disabled={actionPending}
              className="px-1.5 py-1 text-[0.7rem] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] disabled:opacity-40"
            >
              Cancel
            </button>
          </>
        ) : (
          <span className="text-[0.65rem] text-[var(--color-ink-4)] font-mono truncate">
            {proposal.status} · {proposal.reviewed_by ?? '—'}
          </span>
        )}
      </div>

      {/* Picker sits BELOW the action row on purpose: its dropdown is
        * absolutely positioned and opens downward, so above the buttons it
        * would paint over them and swallow the first click aimed at Confirm.
        * A dismissed proposal isn't in the training set, so it has no label
        * to correct and gets no picker at all. */}
      {!isDismissed && (
        <div className="px-2 pb-2">
          <LabelCombobox
            value={draft}
            onChange={onDraftChange}
            options={labelOptions}
            placeholder="tag…"
          />
        </div>
      )}
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
