import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  dismissBrokerCandidate,
  listBrokerMergeCandidates,
  listBrokerMerges,
  mergeBrokerCandidate,
  unmergeBrokers,
  type BrokerMergeBroker,
  type BrokerMergeCandidate,
} from '../lib/api';
import { prettyPhone } from '../lib/brokers';
import { fmtCount, fmtRelative } from '../lib/format';

export default function BrokerReview() {
  const candQ = useQuery({
    queryKey: ['broker-merge-candidates'],
    queryFn: () => listBrokerMergeCandidates(100),
  });
  const candidates = candQ.data?.candidates ?? [];

  return (
    <div className="px-6 py-8 max-w-4xl mx-auto text-[var(--color-ink)]">
      <Link to="/brokers" className="text-xs text-[var(--color-ink-3)] hover:text-[var(--color-copper)]">
        ← Makléři
      </Link>
      <header className="mt-2">
        <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">broker intelligence</p>
        <h1 className="mt-1 text-2xl font-[family-name:var(--font-display)]">Sloučit duplicity</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-3)] max-w-2xl">
          Záznamy se stejným jménem a&nbsp;firmou, které automat nesloučil (nemají
          společný osobní kontakt — typicky firemní účty za přepojovacím číslem).
          Zkontrolujte a&nbsp;slučte ručně. Sloučení je vratné.
        </p>
      </header>

      <div className="mt-6">
        {candQ.isLoading ? (
          <p className="text-sm text-[var(--color-ink-3)]">Načítám…</p>
        ) : candQ.isError ? (
          <p className="text-sm text-[var(--color-brick)]">{(candQ.error as Error).message}</p>
        ) : candidates.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-4)]">
            Žádné návrhy ke sloučení. Fronta se obnovuje denní synchronizací.
          </p>
        ) : (
          <ul className="space-y-3">
            {candidates.map((c) => (
              <li key={c.id}>
                <CandidateCard c={c} />
              </li>
            ))}
          </ul>
        )}
      </div>

      <RecentMerges />
    </div>
  );
}

function CandidateCard({ c }: { c: BrokerMergeCandidate }) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set(c.broker_ids));
  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['broker-merge-candidates'] });
    qc.invalidateQueries({ queryKey: ['broker-merges'] });
  };
  const mergeM = useMutation({
    mutationFn: () => mergeBrokerCandidate(c.id, [...selected]),
    onSuccess: invalidate,
  });
  const dismissM = useMutation({
    mutationFn: () => dismissBrokerCandidate(c.id),
    onSuccess: invalidate,
  });

  const firm = c.evidence.firm_name ?? c.evidence.firm_domain ?? '—';

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)]">
      <div className="border-b border-[var(--color-rule-soft)] px-4 py-2.5">
        <span className="text-sm text-[var(--color-ink)]">{c.evidence.name ?? '—'}</span>
        <span className="text-xs text-[var(--color-ink-3)]"> · {firm} · {c.broker_ids.length} záznamy</span>
      </div>

      <ul className="divide-y divide-[var(--color-rule-soft)]">
        {c.brokers.map((b) => (
          <BrokerRow key={b.broker_id} b={b} checked={selected.has(b.broker_id)}
            onToggle={() => toggle(b.broker_id)} />
        ))}
      </ul>

      <div className="flex items-center gap-2 border-t border-[var(--color-rule-soft)] px-4 py-2.5">
        <button type="button" disabled={selected.size < 2 || mergeM.isPending}
          onClick={() => mergeM.mutate()}
          className="text-xs rounded-[var(--radius-sm)] border border-[var(--color-copper)] bg-[var(--color-copper-soft)] px-3 py-1.5 hover:bg-[var(--color-copper)] hover:text-[var(--color-paper)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
          {mergeM.isPending ? 'Slučuji…' : `Sloučit vybrané (${selected.size})`}
        </button>
        <button type="button" disabled={dismissM.isPending} onClick={() => dismissM.mutate()}
          className="text-xs rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1.5 text-[var(--color-ink-3)] hover:border-[var(--color-copper)] disabled:opacity-40 transition-colors">
          Zamítnout
        </button>
        {(mergeM.isError || dismissM.isError) && (
          <span className="text-xs text-[var(--color-brick)]">
            {((mergeM.error ?? dismissM.error) as Error).message}
          </span>
        )}
      </div>
    </div>
  );
}

function BrokerRow({ b, checked, onToggle }: {
  b: BrokerMergeBroker; checked: boolean; onToggle: () => void;
}) {
  return (
    <li className="flex items-center gap-3 px-4 py-2">
      <input type="checkbox" checked={checked} onChange={onToggle}
        className="accent-[var(--color-copper)]" aria-label="zahrnout do sloučení" />
      <div className="min-w-0 flex-1">
        <Link to={`/brokers/${b.broker_id}`}
          className="text-sm text-[var(--color-ink)] hover:text-[var(--color-copper)]">
          {b.display_name ?? `#${b.broker_id}`}
        </Link>
        <p className="text-xs text-[var(--color-ink-3)] truncate">
          {b.primary_email ?? 'bez e-mailu'}
          {b.primary_phone ? ` · ${prettyPhone(b.primary_phone)}` : ''}
        </p>
      </div>
      <span className="shrink-0 text-xs font-[family-name:var(--font-mono)] tabular-nums text-[var(--color-ink-3)] text-right">
        {fmtCount(b.active_property_count)}
        <span className="block text-[0.62rem] text-[var(--color-ink-4)]">aktivních</span>
      </span>
    </li>
  );
}

function RecentMerges() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const mergesQ = useQuery({ queryKey: ['broker-merges'], queryFn: () => listBrokerMerges(50) });
  const unmergeM = useMutation({
    mutationFn: (g: string) => unmergeBrokers(g),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['broker-merges'] });
      qc.invalidateQueries({ queryKey: ['broker-merge-candidates'] });
    },
  });
  const merges = mergesQ.data?.merges ?? [];

  return (
    <div className="mt-8 border-t border-[var(--color-rule)] pt-4">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="text-xs tracking-[0.12em] uppercase text-[var(--color-ink-3)] hover:text-[var(--color-copper)]">
        {open ? '▾' : '▸'} nedávno sloučeno ({merges.length})
      </button>
      {open && (
        merges.length === 0 ? (
          <p className="mt-2 text-sm text-[var(--color-ink-4)]">Zatím nic.</p>
        ) : (
          <ul className="mt-3 space-y-1">
            {merges.map((m) => (
              <li key={m.merge_group_id}
                className="flex items-center justify-between gap-3 text-sm border border-[var(--color-rule-soft)] rounded-[var(--radius-sm)] px-3 py-1.5">
                <span className="min-w-0 truncate">
                  <Link to={`/brokers/${m.survivor_broker_id}`} className="hover:text-[var(--color-copper)]">
                    {m.survivor_name ?? `#${m.survivor_broker_id}`}
                  </Link>
                  <span className="text-[var(--color-ink-4)]">
                    {' '}← {m.retired_broker_ids.length} sloučeno
                    {m.source === 'auto' ? ' · auto' : ''} · {fmtRelative(m.merged_at)}
                  </span>
                </span>
                <button type="button" disabled={unmergeM.isPending}
                  onClick={() => unmergeM.mutate(m.merge_group_id)}
                  className="shrink-0 text-xs text-[var(--color-ink-3)] hover:text-[var(--color-copper)] disabled:opacity-40">
                  Rozdělit
                </button>
              </li>
            ))}
          </ul>
        )
      )}
    </div>
  );
}
