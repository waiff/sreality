import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { movePipelineCard } from '@/lib/api';
import {
  fetchPipelineBoard,
  fetchPipelineStages,
  pipelineKeys,
} from '@/lib/queries';
import { fmtArea, fmtCzk } from '@/lib/format';
import { listingPath } from '@/lib/listingUrl';
import type { PipelineBoardCard, PipelineStage } from '@/lib/types';

export default function Pipeline() {
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
        <p className="text-[0.75rem] tracking-wide text-[var(--color-ink-3)] font-mono tabular-nums">
          {cards.length} nemovitostí
        </p>
      </header>

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
