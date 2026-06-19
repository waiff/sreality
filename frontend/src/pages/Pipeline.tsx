import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  archivePipelineStage,
  createPipelineStage,
  movePipelineCard,
  reorderPipelineStages,
  updatePipelineStage,
} from '@/lib/api';
import {
  fetchPipelineBoard,
  fetchPipelineStages,
  pipelineKeys,
} from '@/lib/queries';
import { fmtArea, fmtCzk } from '@/lib/format';
import { listingPath } from '@/lib/listingUrl';
import { TAG_COLORS, type PipelineBoardCard, type PipelineStage, type TagColor } from '@/lib/types';

export default function Pipeline() {
  const [manage, setManage] = useState(false);
  const stagesQ = useQuery({
    queryKey: pipelineKeys.stages,
    queryFn: fetchPipelineStages,
    staleTime: 60_000,
  });
  const boardQ = useQuery({
    queryKey: pipelineKeys.board,
    queryFn: fetchPipelineBoard,
    staleTime: 30_000,
  });

  const stages = stagesQ.data ?? [];
  const cards = boardQ.data ?? [];

  const byStage = useMemo(() => {
    const m = new Map<number, PipelineBoardCard[]>();
    for (const s of stagesQ.data ?? []) m.set(s.id, []);
    for (const c of boardQ.data ?? []) {
      const bucket = m.get(c.stage_id);
      if (bucket) bucket.push(c);
    }
    return m;
  }, [stagesQ.data, boardQ.data]);

  return (
    <div className="px-6 py-8">
      <header className="flex items-baseline justify-between">
        <div>
          <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
            Pipeline
          </p>
          <h1
            className="mt-1.5 text-[2.4rem] leading-[1.05]"
            style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
          >
            Pipeline obchodů
          </h1>
        </div>
        <div className="flex items-center gap-4">
          <p className="text-[0.75rem] tracking-wide text-[var(--color-ink-3)] font-mono tabular-nums">
            {cards.length} nemovitostí
          </p>
          <button
            type="button"
            onClick={() => setManage((v) => !v)}
            aria-pressed={manage}
            className="text-[0.72rem] tracking-[0.1em] uppercase px-2.5 py-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)]"
          >
            {manage ? 'Hotovo' : 'Spravovat fáze'}
          </button>
        </div>
      </header>

      {manage && stages.length > 0 && <StageManager stages={stages} />}

      {stagesQ.isLoading || boardQ.isLoading ? (
        <p className="mt-8 text-sm text-[var(--color-ink-3)]">Načítání…</p>
      ) : stagesQ.error || boardQ.error ? (
        <p className="mt-8 text-sm text-[var(--color-brick)]">
          Nepodařilo se načíst pipeline.
        </p>
      ) : cards.length === 0 ? (
        <p className="mt-8 text-sm text-[var(--color-ink-3)]">
          Zatím prázdné. Přidejte nemovitost do pipeline tlačítkem „Přidat do
          pipeline" na detailu inzerátu.
        </p>
      ) : (
        <div className="mt-6 flex gap-4 overflow-x-auto pb-4">
          {stages.map((s) => (
            <StageColumn
              key={s.id}
              stage={s}
              stages={stages}
              cards={byStage.get(s.id) ?? []}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function stageColor(stage: PipelineStage): string {
  return stage.color
    ? `var(--color-tag-${stage.color})`
    : 'var(--color-rule-strong)';
}

function StageManager({ stages }: { stages: PipelineStage[] }) {
  const qc = useQueryClient();
  const [err, setErr] = useState<string | null>(null);
  const [newLabel, setNewLabel] = useState('');

  const invalidate = () => {
    setErr(null);
    void qc.invalidateQueries({ queryKey: ['pipeline'] });
  };
  const onError = (e: unknown) =>
    setErr(e instanceof Error ? e.message : 'Akce selhala.');

  const reorder = useMutation({
    mutationFn: (ids: number[]) => reorderPipelineStages(ids),
    onSuccess: invalidate,
    onError,
  });
  const create = useMutation({
    mutationFn: (label: string) => createPipelineStage({ label }),
    onSuccess: () => {
      setNewLabel('');
      invalidate();
    },
    onError,
  });

  const move = (idx: number, dir: -1 | 1) => {
    const ids = stages.map((s) => s.id);
    const j = idx + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[idx], ids[j]] = [ids[j], ids[idx]];
    reorder.mutate(ids);
  };

  const submitNew = () => {
    const label = newLabel.trim();
    if (label) create.mutate(label);
  };

  return (
    <section className="mt-5 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4">
      <p className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        Fáze pipeline
      </p>
      {err && (
        <p className="mt-2 text-xs text-[var(--color-brick)]">{err}</p>
      )}
      <ul className="mt-3 space-y-2">
        {stages.map((s, i) => (
          <StageEditorRow
            key={s.id}
            stage={s}
            isFirst={i === 0}
            isLast={i === stages.length - 1}
            onMove={(dir) => move(i, dir)}
            onError={onError}
            invalidate={invalidate}
          />
        ))}
      </ul>
      <div className="mt-4 flex items-center gap-2 border-t border-[var(--color-rule)] pt-3">
        <input
          value={newLabel}
          onChange={(e) => setNewLabel(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submitNew();
          }}
          placeholder="Nová fáze…"
          maxLength={80}
          className="flex-1 px-2 py-1 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)]"
        />
        <button
          type="button"
          onClick={submitNew}
          disabled={!newLabel.trim() || create.isPending}
          className="text-[0.72rem] tracking-[0.1em] uppercase px-3 py-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)] disabled:opacity-50"
        >
          Přidat
        </button>
      </div>
    </section>
  );
}

function StageEditorRow({
  stage,
  isFirst,
  isLast,
  onMove,
  onError,
  invalidate,
}: {
  stage: PipelineStage;
  isFirst: boolean;
  isLast: boolean;
  onMove: (dir: -1 | 1) => void;
  onError: (e: unknown) => void;
  invalidate: () => void;
}) {
  const [label, setLabel] = useState(stage.label);

  const update = useMutation({
    mutationFn: (patch: {
      label?: string;
      color?: TagColor | null;
      is_terminal?: boolean;
      is_entry?: boolean;
    }) => updatePipelineStage(stage.id, patch),
    onSuccess: invalidate,
    onError,
  });
  const archive = useMutation({
    mutationFn: () => archivePipelineStage(stage.id),
    onSuccess: invalidate,
    onError,
  });

  const saveLabel = () => {
    const next = label.trim();
    if (next && next !== stage.label) update.mutate({ label: next });
    else setLabel(stage.label);
  };

  return (
    <li className="flex items-center gap-2">
      <span
        className="h-4 w-1 shrink-0 rounded-full"
        style={{ background: stageColor(stage) }}
        aria-hidden
      />
      <div className="flex shrink-0 flex-col leading-none">
        <button
          type="button"
          onClick={() => onMove(-1)}
          disabled={isFirst}
          aria-label="Posunout nahoru"
          className="text-[0.6rem] text-[var(--color-ink-3)] hover:text-[var(--color-ink)] disabled:opacity-25"
        >
          ▲
        </button>
        <button
          type="button"
          onClick={() => onMove(1)}
          disabled={isLast}
          aria-label="Posunout dolů"
          className="text-[0.6rem] text-[var(--color-ink-3)] hover:text-[var(--color-ink)] disabled:opacity-25"
        >
          ▼
        </button>
      </div>
      <input
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        onBlur={saveLabel}
        onKeyDown={(e) => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
        }}
        maxLength={80}
        aria-label="Název fáze"
        className="flex-1 min-w-0 px-2 py-1 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-transparent hover:border-[var(--color-rule)] focus:border-[var(--color-rule-strong)] focus:outline-none text-[var(--color-ink)]"
      />
      <select
        value={stage.color ?? ''}
        onChange={(e) =>
          update.mutate({ color: (e.target.value || null) as TagColor | null })
        }
        aria-label="Barva fáze"
        className="shrink-0 px-1.5 py-1 text-[0.72rem] rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink-2)] focus:outline-none"
      >
        <option value="">—</option>
        {TAG_COLORS.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={() => !stage.is_entry && update.mutate({ is_entry: true })}
        disabled={stage.is_entry}
        title={stage.is_entry ? 'Vstupní fáze (záložka)' : 'Nastavit jako vstupní'}
        aria-label="Vstupní fáze"
        className="shrink-0 text-[0.85rem] w-6 text-center disabled:cursor-default"
        style={{ color: stage.is_entry ? 'var(--color-copper)' : 'var(--color-ink-4)' }}
      >
        {stage.is_entry ? '★' : '☆'}
      </button>
      <label className="shrink-0 flex items-center gap-1 text-[0.68rem] text-[var(--color-ink-3)]">
        <input
          type="checkbox"
          checked={stage.is_terminal}
          onChange={(e) => update.mutate({ is_terminal: e.target.checked })}
          disabled={stage.is_entry}
        />
        konec
      </label>
      <button
        type="button"
        onClick={() => archive.mutate()}
        disabled={stage.is_entry || archive.isPending}
        title={
          stage.is_entry
            ? 'Vstupní fázi nelze archivovat'
            : 'Archivovat fázi (musí být prázdná)'
        }
        aria-label="Archivovat fázi"
        className="shrink-0 w-6 text-center text-[var(--color-ink-4)] hover:text-[var(--color-brick)] disabled:opacity-25"
      >
        ✕
      </button>
    </li>
  );
}

function StageColumn({
  stage,
  stages,
  cards,
}: {
  stage: PipelineStage;
  stages: PipelineStage[];
  cards: PipelineBoardCard[];
}) {
  return (
    <div className="w-72 shrink-0">
      <div
        className="flex items-baseline justify-between px-1 pb-2 border-b-2"
        style={{ borderColor: stageColor(stage) }}
      >
        <span
          className="text-[0.72rem] tracking-[0.14em] uppercase font-medium"
          style={{ color: stageColor(stage) }}
        >
          {stage.label}
        </span>
        <span className="font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-4)]">
          {cards.length}
        </span>
      </div>
      <ul className="mt-3 space-y-2">
        {cards.length === 0 ? (
          <li className="px-1 text-sm text-[var(--color-ink-4)]">—</li>
        ) : (
          cards.map((c) => (
            <li key={c.property_id}>
              <BoardCard card={c} stages={stages} />
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

function BoardCard({
  card,
  stages,
}: {
  card: PipelineBoardCard;
  stages: PipelineStage[];
}) {
  const qc = useQueryClient();
  const move = useMutation({
    mutationFn: (stage_id: number) => movePipelineCard(card.property_id, stage_id),
    onSuccess: () => qc.invalidateQueries({ queryKey: pipelineKeys.board }),
  });

  const meta = [card.disposition, card.district].filter(Boolean).join(' · ');

  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-2.5">
      {card.sreality_id != null ? (
        <Link
          to={listingPath(card.sreality_id)}
          className="font-mono tabular-nums text-sm text-[var(--color-ink)] hover:text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          {fmtCzk(card.price_czk)}
        </Link>
      ) : (
        <span className="font-mono tabular-nums text-sm text-[var(--color-ink)]">
          {fmtCzk(card.price_czk)}
        </span>
      )}
      <p className="mt-0.5 text-xs text-[var(--color-ink-2)]">
        {meta || <span className="text-[var(--color-ink-4)]">—</span>}
      </p>
      {card.area_m2 != null && (
        <p className="text-xs text-[var(--color-ink-4)] font-mono tabular-nums">
          {fmtArea(card.area_m2)}
        </p>
      )}
      <select
        value={card.stage_id}
        onChange={(e) => move.mutate(Number(e.target.value))}
        disabled={move.isPending}
        aria-label="Přesunout do fáze"
        className="mt-2 w-full px-2 py-1 text-[0.78rem] rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink-2)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
      >
        {stages.map((s) => (
          <option key={s.id} value={s.id}>
            {s.label}
          </option>
        ))}
      </select>
    </div>
  );
}
