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
  type NewDedupTaxonomyLabel,
  type NewDedupLabelProposal,
} from '@/lib/api';
import { fetchImagesByImageIds } from '@/lib/queries';
import { imageSrc } from '@/lib/imageUrl';
import { pushToast } from '@/lib/toast';
import { TrashIcon } from '@/components/icons';
import Tabs from '@/components/Tabs';
import ImageTagBadge from '@/components/ImageTagBadge';
import { CATEGORY_MAIN_TABS } from '@/lib/categoryMainTabs';
import type { ImagePublic } from '@/lib/types';

const OVERVIEW_KEY = ['new-dedup', 'labeling', 'overview'];
const PROPOSALS_KEY = ['new-dedup', 'labeling', 'proposals'];
const SETTINGS_KEY = ['new-dedup', 'settings'];

type Status = 'pending' | 'confirmed' | 'dismissed';
const STATUS_TABS: ReadonlyArray<{ key: Status; label: string }> = [
  { key: 'pending', label: 'Pending' },
  { key: 'confirmed', label: 'Confirmed' },
  { key: 'dismissed', label: 'Dismissed' },
];

export default function NewDedupLabeling() {
  const qc = useQueryClient();
  const overviewQ = useQuery({ queryKey: OVERVIEW_KEY, queryFn: getNewDedupLabelingOverview });
  const settingsQ = useQuery({ queryKey: SETTINGS_KEY, queryFn: listNewDedupSettings });

  const [labelFilter, setLabelFilter] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>('pending');
  const [showOriginal, setShowOriginal] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  // confirmMut/dismissMut below are ONE shared mutation instance for the
  // whole grid — TanStack Query's observer only ever reflects the MOST
  // RECENT .mutate() call's isPending/variables, so a second tile's click
  // would make an earlier tile's still-in-flight action look finished.
  // Track per-tile pending state locally instead.
  const [pendingActionIds, setPendingActionIds] = useState<ReadonlySet<number>>(new Set());

  const proposalsQ = useQuery({
    queryKey: [...PROPOSALS_KEY, status, labelFilter],
    queryFn: () =>
      listNewDedupProposals({ status, label: labelFilter ?? undefined, limit: 200 }),
  });
  const proposals = useMemo(() => proposalsQ.data?.data ?? [], [proposalsQ.data]);
  const imageIds = useMemo(() => proposals.map((p) => p.image_id), [proposals]);

  const imagesQ = useQuery({
    queryKey: ['new-dedup', 'labeling', 'images', imageIds],
    queryFn: () => fetchImagesByImageIds(imageIds),
    enabled: imageIds.length > 0,
  });
  const images = imagesQ.data;

  useEffect(() => setSelected(new Set()), [status, labelFilter]);
  useEffect(() => {
    setSelected((prev) => {
      const ids = new Set(imageIds);
      const next = new Set([...prev].filter((id) => ids.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [imageIds]);

  const settingValue = (key: string) => settingsQ.data?.data.find((s) => s.key === key)?.value;
  const gate1Target = (settingValue('labeling_gate1_target_per_tag') as number) ?? 150;
  const proposalTarget = (settingValue('labeling_target_proposals_per_category') as number) ?? 300;
  const secondaryModel = settingValue('labeling_secondary_model') as string | undefined;

  const invalidateOverview = () => qc.invalidateQueries({ queryKey: OVERVIEW_KEY });
  const invalidateProposals = () => qc.invalidateQueries({ queryKey: PROPOSALS_KEY });

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
  const growMut = useMutation({
    mutationFn: () => growNewDedupSample(Number(growCount), growCategory || null),
    onSuccess: (res) => {
      pushToast('ok', `Sample grew by ${res.data.added}.`);
      invalidateOverview();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });

  // --- proposal review ------------------------------------------------------

  const beginAction = (imageId: number) =>
    setPendingActionIds((prev) => new Set(prev).add(imageId));
  const endAction = (imageId: number) =>
    setPendingActionIds((prev) => {
      if (!prev.has(imageId)) return prev;
      const next = new Set(prev);
      next.delete(imageId);
      return next;
    });

  const confirmMut = useMutation({
    mutationFn: ({ imageId, model }: { imageId: number; model: string }) =>
      confirmNewDedupProposal(imageId, model),
    onSuccess: () => {
      invalidateProposals();
      invalidateOverview();
    },
    onError: (err: Error) => pushToast('err', err.message),
    onSettled: (_data, _err, vars) => endAction(vars.imageId),
  });
  const dismissMut = useMutation({
    mutationFn: ({ imageId, model }: { imageId: number; model: string }) =>
      dismissNewDedupProposal(imageId, model),
    onSuccess: () => {
      invalidateProposals();
      invalidateOverview();
    },
    onError: (err: Error) => pushToast('err', err.message),
    onSettled: (_data, _err, vars) => endAction(vars.imageId),
  });
  const bulkConfirmMut = useMutation({
    mutationFn: (model: string) => bulkConfirmNewDedupProposals(model, [...selected]),
    onSuccess: (res) => {
      pushToast('ok', `Confirmed ${res.data.confirmed}.`);
      setSelected(new Set());
      invalidateProposals();
      invalidateOverview();
    },
    onError: (err: Error) => pushToast('err', err.message),
  });
  const bulkDismissMut = useMutation({
    mutationFn: (model: string) => bulkDismissNewDedupProposals(model, [...selected]),
    onSuccess: (res) => {
      pushToast('ok', `Dismissed ${res.data.dismissed}.`);
      setSelected(new Set());
      invalidateProposals();
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
  // Only pending proposals under the CURRENT secondary model batch together —
  // an older model's leftover pending rows (from before a model config change)
  // still review one at a time via the per-tile buttons.
  const selectableIds = useMemo(
    () =>
      status === 'pending' && secondaryModel
        ? proposals.filter((p) => p.model === secondaryModel).map((p) => p.image_id)
        : [],
    [proposals, status, secondaryModel],
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

      <TaxonomySection
        labels={overviewQ.data?.data.labels ?? []}
        sampleSize={overviewQ.data?.data.sample_size}
        loading={!overviewQ.data && !overviewQ.error}
        gate1Target={gate1Target}
        proposalTarget={proposalTarget}
        activeLabel={labelFilter}
        onFilter={(label) => setLabelFilter((cur) => (cur === label ? null : label))}
        newLabelText={newLabelText}
        onNewLabelTextChange={setNewLabelText}
        onAdd={() => addLabelMut.mutate()}
        addPending={addLabelMut.isPending}
        onRename={(id, oldLabel, label) => renameLabelMut.mutate({ id, oldLabel, label })}
        renamePending={renameLabelMut.isPending}
        onRemove={(id, oldLabel) => removeLabelMut.mutate({ id, oldLabel })}
        removePending={removeLabelMut.isPending}
      />

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
            className="w-20 px-2 py-1 font-mono text-sm text-right rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)]"
          />
          <select
            value={growCategory}
            onChange={(e) => setGrowCategory(e.target.value)}
            className="px-2 py-1 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)]"
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
            className="px-3 py-1 text-xs rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-50"
          >
            Grow sample
          </button>
        </div>
        <p className="mt-2 text-xs text-[var(--color-ink-4)] leading-relaxed">
          Adds newest not-yet-sampled images to the pool the relabel job scores. Scoring itself
          runs separately via the "NEW DEDUP — Labeling secondary-CLIP proposals" GitHub Actions
          workflow (model: {secondaryModel ?? '…'}).
        </p>
      </section>

      <section className="mt-8">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <Tabs tabs={STATUS_TABS} active={status} onChange={setStatus} />
          <div className="flex items-center gap-1">
            <ToggleButton active={!showOriginal} onClick={() => setShowOriginal(false)}>
              New tag
            </ToggleButton>
            <ToggleButton active={showOriginal} onClick={() => setShowOriginal(true)}>
              Original tag
            </ToggleButton>
          </div>
        </div>

        {labelFilter && (
          <p className="mt-3 text-xs text-[var(--color-ink-3)]">
            Filtered to <span className="font-mono">{labelFilter}</span>.{' '}
            <button
              type="button"
              onClick={() => setLabelFilter(null)}
              className="underline decoration-dotted underline-offset-2 hover:text-[var(--color-copper-2)]"
            >
              clear
            </button>
          </p>
        )}

        {status === 'pending' && selectableIds.length > 0 && (
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
          <p className="mt-6 text-sm text-[var(--color-ink-3)]">No {status} proposals.</p>
        )}

        <div className="mt-4 grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
          {proposals.map((p) => (
            <ProposalTile
              key={`${p.image_id}:${p.model}`}
              proposal={p}
              image={images?.get(p.image_id)}
              showOriginal={showOriginal}
              selectable={status === 'pending' && p.model === secondaryModel}
              selected={selected.has(p.image_id)}
              onToggleSelect={() => toggle(p.image_id)}
              onConfirm={() => {
                beginAction(p.image_id);
                confirmMut.mutate({ imageId: p.image_id, model: p.model });
              }}
              onDismiss={() => {
                beginAction(p.image_id);
                dismissMut.mutate({ imageId: p.image_id, model: p.model });
              }}
              actionPending={
                pendingActionIds.has(p.image_id)
              }
              reviewed={status !== 'pending'}
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

function TaxonomySection({
  labels,
  sampleSize,
  loading,
  gate1Target,
  proposalTarget,
  activeLabel,
  onFilter,
  newLabelText,
  onNewLabelTextChange,
  onAdd,
  addPending,
  onRename,
  renamePending,
  onRemove,
  removePending,
}: {
  labels: NewDedupTaxonomyLabel[];
  sampleSize: number | undefined;
  loading: boolean;
  gate1Target: number;
  proposalTarget: number;
  activeLabel: string | null;
  onFilter: (label: string) => void;
  newLabelText: string;
  onNewLabelTextChange: (v: string) => void;
  onAdd: () => void;
  addPending: boolean;
  onRename: (id: number, oldLabel: string, label: string) => void;
  renamePending: boolean;
  onRemove: (id: number, oldLabel: string) => void;
  removePending: boolean;
}) {
  return (
    <section className="mt-8">
      <div className="flex items-center justify-between mb-3">
        <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Taxonomy v1 ({labels.length} labels{sampleSize != null ? `, ${sampleSize} sampled` : ''})
        </span>
      </div>

      {loading && <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>}
      {!loading && labels.length === 0 && (
        <p className="text-sm text-[var(--color-ink-3)]">
          No labels yet — add the first one below.
        </p>
      )}

      <div className="space-y-2">
        {labels.map((l) => (
          <TaxonomyLabelRow
            key={l.id}
            label={l}
            active={activeLabel === l.label}
            gate1Target={gate1Target}
            proposalTarget={proposalTarget}
            onFilter={() => onFilter(l.label)}
            onRename={(next) => onRename(l.id, l.label, next)}
            renamePending={renamePending}
            onRemove={() => onRemove(l.id, l.label)}
            removePending={removePending}
          />
        ))}
      </div>

      <div className="mt-3 flex items-center gap-2">
        <input
          type="text"
          value={newLabelText}
          onChange={(e) => onNewLabelTextChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && newLabelText.trim()) onAdd();
          }}
          placeholder="new label, e.g. interier - kuchyne"
          className="px-2 py-1 text-sm font-mono rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)] w-64"
        />
        <button
          type="button"
          onClick={onAdd}
          disabled={addPending || !newLabelText.trim()}
          className="px-3 py-1 text-xs rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-50"
        >
          Add label
        </button>
      </div>
    </section>
  );
}

function TaxonomyLabelRow({
  label,
  active,
  gate1Target,
  proposalTarget,
  onFilter,
  onRename,
  renamePending,
  onRemove,
  removePending,
}: {
  label: NewDedupTaxonomyLabel;
  active: boolean;
  gate1Target: number;
  proposalTarget: number;
  onFilter: () => void;
  onRename: (next: string) => void;
  renamePending: boolean;
  onRemove: () => void;
  removePending: boolean;
}) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(label.label);
  const [confirmingRemove, setConfirmingRemove] = useState(false);

  const gate1Pct = Math.min(100, Math.round((label.confirmed_count / gate1Target) * 100));

  return (
    <div
      className={[
        'border rounded-[var(--radius-sm)] px-3 py-2',
        active ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)]' : 'border-[var(--color-rule)]',
      ].join(' ')}
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0 flex items-center gap-2">
          {renaming ? (
            <input
              autoFocus
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && draft.trim()) {
                  onRename(draft.trim());
                  setRenaming(false);
                }
                if (e.key === 'Escape') {
                  setDraft(label.label);
                  setRenaming(false);
                }
              }}
              className="px-1.5 py-0.5 text-sm font-mono rounded-[var(--radius-xs)] border border-[var(--color-rule-strong)] bg-[var(--color-paper-2)]"
            />
          ) : (
            <button
              type="button"
              onClick={onFilter}
              className="font-mono text-sm hover:text-[var(--color-copper-2)] truncate"
              title="Filter proposals to this label"
            >
              {label.label}
            </button>
          )}
          {!label.active && (
            <span className="text-[0.6rem] tracking-[0.1em] uppercase px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-ink-4)]/20 text-[var(--color-ink-3)]">
              inactive
            </span>
          )}
        </div>

        <div className="flex items-center gap-3 text-xs font-mono tabular-nums text-[var(--color-ink-3)] shrink-0">
          <span title="Confirmed training images">{`✓ ${label.confirmed_count}/${gate1Target}`}</span>
          <span title="Pending proposals">{`? ${label.pending_count}/${proposalTarget}`}</span>
          {label.dismissed_count > 0 && (
            <span title="Dismissed proposals">{`✗ ${label.dismissed_count}`}</span>
          )}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {renaming ? (
            <>
              <button
                type="button"
                disabled={renamePending || !draft.trim()}
                onClick={() => {
                  onRename(draft.trim());
                  setRenaming(false);
                }}
                className="text-xs text-[var(--color-copper)] disabled:opacity-40"
              >
                Save
              </button>
              <button
                type="button"
                onClick={() => {
                  setDraft(label.label);
                  setRenaming(false);
                }}
                className="text-xs text-[var(--color-ink-3)]"
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => setRenaming(true)}
              className="text-xs text-[var(--color-ink-3)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-copper-2)]"
            >
              rename
            </button>
          )}
          <button
            type="button"
            onClick={() => setConfirmingRemove(true)}
            aria-label={`Remove ${label.label}`}
            className="text-[var(--color-ink-4)] hover:text-[var(--color-brick)]"
          >
            <TrashIcon className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div className="mt-1.5 h-1 rounded-full bg-[var(--color-rule-soft)] overflow-hidden">
        <div
          className="h-full bg-[var(--color-sage)]"
          style={{ width: `${gate1Pct}%` }}
          aria-hidden
        />
      </div>

      {confirmingRemove && (
        <div className="mt-2 pt-2 border-t border-[var(--color-rule-soft)] flex items-center gap-3 text-xs text-[var(--color-brick)]">
          <span>
            Remove {label.label}? {label.confirmed_count} training example
            {label.confirmed_count === 1 ? '' : 's'} and {label.pending_count + label.dismissed_count}{' '}
            proposal{label.pending_count + label.dismissed_count === 1 ? '' : 's'} go with it
            (images stay).
          </span>
          <button
            type="button"
            disabled={removePending}
            onClick={() => {
              onRemove();
              setConfirmingRemove(false);
            }}
            className="text-[var(--color-brick)] font-medium disabled:opacity-40"
          >
            Remove
          </button>
          <button
            type="button"
            onClick={() => setConfirmingRemove(false)}
            className="text-[var(--color-ink-3)]"
          >
            Cancel
          </button>
        </div>
      )}
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
  onConfirm,
  onDismiss,
  actionPending,
  reviewed,
}: {
  proposal: NewDedupLabelProposal;
  image: ImagePublic | undefined;
  showOriginal: boolean;
  selectable: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  onConfirm: () => void;
  onDismiss: () => void;
  actionPending: boolean;
  reviewed: boolean;
}) {
  const badgeTag = showOriginal ? image?.clip_fine_tag ?? null : proposal.label;
  const badgeConfidence = showOriginal ? image?.clip_confidence ?? null : proposal.confidence;

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] overflow-hidden bg-[var(--color-paper)]">
      <div className="relative aspect-[4/3] bg-[var(--color-inset)]">
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
              onClick={onConfirm}
              disabled={actionPending}
              className="flex-1 px-1.5 py-1 text-[0.7rem] rounded-[var(--radius-xs)] bg-[var(--color-sage-soft)] text-[var(--color-sage)] disabled:opacity-40"
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
        ) : (
          <span className="text-[0.65rem] text-[var(--color-ink-4)] font-mono">
            {proposal.status} · {proposal.reviewed_by ?? '—'}
          </span>
        )}
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
