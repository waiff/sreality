import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  generateOutreachDrafts,
  getOutreachCampaign,
  listOutreachMessages,
  listOutreachSuppressions,
  previewOutreachTargets,
  regenerateOutreachMessage,
  removeOutreachSuppression,
  addOutreachSuppression,
  updateOutreachCampaign,
  updateOutreachMessage,
  type OutreachMessage,
} from '../lib/api';
import { prettyPhone } from '../lib/brokers';
import { fmtRelative } from '../lib/format';
import { usePageTitle } from '@/lib/pageTitle';
import { StatusChip } from './Outreach';

export default function OutreachDetail() {
  const { id } = useParams<{ id: string }>();
  const campaignId = Number(id);
  const qc = useQueryClient();
  const keys = {
    campaign: ['outreach-campaign', campaignId],
    messages: ['outreach-messages', campaignId],
    suppressions: ['outreach-suppressions'],
  };

  const campaignQ = useQuery({
    queryKey: keys.campaign,
    queryFn: () => getOutreachCampaign(campaignId),
    enabled: Number.isFinite(campaignId),
  });
  const messagesQ = useQuery({
    queryKey: keys.messages,
    queryFn: () => listOutreachMessages(campaignId),
    enabled: Number.isFinite(campaignId),
  });
  const targetsQ = useQuery({
    queryKey: ['outreach-targets', campaignId],
    queryFn: () => previewOutreachTargets(campaignId, 50),
    enabled: Number.isFinite(campaignId),
  });

  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: keys.messages });
    qc.invalidateQueries({ queryKey: keys.campaign });
    qc.invalidateQueries({ queryKey: ['outreach-targets', campaignId] });
  };

  const generateM = useMutation({
    mutationFn: () => generateOutreachDrafts(campaignId, 25),
    onSuccess: invalidateAll,
  });

  const campaign = campaignQ.data;
  const messages = messagesQ.data?.messages ?? [];
  const pendingTargets = targetsQ.data?.count ?? 0;

  usePageTitle(campaign?.name ?? null);

  if (campaignQ.isLoading) {
    return <Centered>Načítám kampaň…</Centered>;
  }
  if (campaignQ.isError || !campaign) {
    return <Centered tone="error">Kampaň nenalezena.</Centered>;
  }

  return (
    <div className="px-6 py-8 max-w-4xl mx-auto text-[var(--color-ink)]">
      <Link to="/outreach" className="text-xs text-[var(--color-ink-3)] hover:text-[var(--color-copper)]">
        ← Oslovení
      </Link>

      <header className="mt-2 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-[family-name:var(--font-display)] truncate">{campaign.name}</h1>
            <StatusChip status={campaign.status} />
          </div>
          {campaign.goal && (
            <p className="mt-1 text-sm text-[var(--color-ink-3)] max-w-2xl">{campaign.goal}</p>
          )}
        </div>
        <StatusSelect campaignId={campaignId} status={campaign.status}
          onChanged={() => qc.invalidateQueries({ queryKey: keys.campaign })} />
      </header>

      {/* Generate bar */}
      <div className="mt-5 border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-3.5 flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm text-[var(--color-ink-3)]">
          <span className="font-[family-name:var(--font-mono)] tabular-nums text-[var(--color-ink)]">
            {pendingTargets}
          </span>{' '}
          oslovitelných makléřů čeká na koncept
          <span className="block text-xs text-[var(--color-ink-4)]">
            (mají e-mail, nejsou na seznamu „nekontaktovat" a&nbsp;ještě nemají koncept)
          </span>
        </div>
        <button
          type="button"
          disabled={generateM.isPending || pendingTargets === 0}
          onClick={() => generateM.mutate()}
          className="text-sm rounded-[var(--radius-sm)] border border-[var(--color-copper)] bg-[var(--color-copper-soft)] px-4 py-2 hover:bg-[var(--color-copper)] hover:text-[var(--color-paper)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {generateM.isPending ? 'Píšu koncepty…' : `Napsat koncepty (${Math.min(pendingTargets, 25)})`}
        </button>
      </div>
      {generateM.isError && (
        <p className="mt-2 text-xs text-[var(--color-brick)]">{(generateM.error as Error).message}</p>
      )}

      {/* Messages */}
      <div className="mt-6">
        {messagesQ.isLoading ? (
          <p className="text-sm text-[var(--color-ink-3)]">Načítám koncepty…</p>
        ) : messages.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-4)]">
            Zatím žádné koncepty. Vygenerujte je tlačítkem výše.
          </p>
        ) : (
          <ul className="space-y-3">
            {messages.map((m) => (
              <li key={m.id}>
                <MessageCard m={m} onChanged={invalidateAll} />
              </li>
            ))}
          </ul>
        )}
      </div>

      <SuppressionPanel />
    </div>
  );
}

function MessageCard({ m, onChanged }: { m: OutreachMessage; onChanged: () => void }) {
  const qc = useQueryClient();
  const [subject, setSubject] = useState(m.subject ?? '');
  const [body, setBody] = useState(m.body ?? '');
  // Re-sync when a regenerate replaces the draft underneath us.
  useEffect(() => { setSubject(m.subject ?? ''); setBody(m.body ?? ''); },
    [m.subject, m.body]);
  const dirty = subject !== (m.subject ?? '') || body !== (m.body ?? '');
  const locked = m.status === 'sent' || m.status === 'replied';

  const patchM = useMutation({
    mutationFn: (patch: Parameters<typeof updateOutreachMessage>[1]) =>
      updateOutreachMessage(m.id, patch),
    onSuccess: onChanged,
  });
  const regenM = useMutation({
    mutationFn: () => regenerateOutreachMessage(m.id),
    onSuccess: onChanged,
  });
  const suppressM = useMutation({
    mutationFn: () => addOutreachSuppression(m.broker_id, 'z konceptu oslovení'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['outreach-suppressions'] });
      patchM.mutate({ status: 'skipped' });
    },
  });

  const mailto =
    `mailto:${encodeURIComponent(m.to_email ?? '')}` +
    `?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)]">
      {/* margin note: who + footprint */}
      <div className="flex items-center justify-between gap-3 border-b border-[var(--color-rule-soft)] px-4 py-2.5">
        <div className="min-w-0">
          <Link to={`/brokers/${m.broker_id}`}
            className="text-sm text-[var(--color-ink)] hover:text-[var(--color-copper)]">
            {m.broker_name ?? 'Neznámý makléř'}
          </Link>
          <p className="text-xs text-[var(--color-ink-3)] truncate">
            {m.firm_name ?? 'nezávislý'}
            {m.to_email ? ` · ${m.to_email}` : ''}
            {m.to_phone ? ` · ${prettyPhone(m.to_phone)}` : ''}
          </p>
        </div>
        <StatusChip status={m.status} />
      </div>

      {/* the letter */}
      <div className="px-4 py-3 space-y-2">
        <input
          value={subject}
          disabled={locked}
          onChange={(e) => setSubject(e.target.value)}
          placeholder="Předmět"
          className="w-full text-sm font-medium border border-transparent hover:border-[var(--color-rule)] focus:border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-transparent px-2 py-1 text-[var(--color-ink)] focus:outline-none focus:ring-2 focus:ring-[var(--color-focus)] disabled:opacity-70"
        />
        <textarea
          value={body}
          disabled={locked}
          onChange={(e) => setBody(e.target.value)}
          rows={7}
          className="w-full text-sm leading-relaxed border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-3)] px-3 py-2 text-[var(--color-ink)] focus:outline-none focus:ring-2 focus:ring-[var(--color-focus)] disabled:opacity-70 font-[family-name:var(--font-mono)]"
        />
      </div>

      {/* actions */}
      <div className="flex flex-wrap items-center gap-2 border-t border-[var(--color-rule-soft)] px-4 py-2.5">
        {dirty && !locked && (
          <Action onClick={() => patchM.mutate({ subject, body })}>Uložit úpravy</Action>
        )}
        {!locked && m.status !== 'approved' && (
          <Action onClick={() => patchM.mutate({ status: 'approved' })}>Schválit</Action>
        )}
        <a
          href={mailto}
          className="text-xs rounded-[var(--radius-sm)] border border-[var(--color-copper)] bg-[var(--color-copper-soft)] px-3 py-1.5 hover:bg-[var(--color-copper)] hover:text-[var(--color-paper)] transition-colors"
        >
          Otevřít v e-mailu
        </a>
        {m.status !== 'sent' && (
          <Action accent onClick={() => patchM.mutate({ status: 'sent' })}>Označit odesláno</Action>
        )}
        <Action onClick={() => regenM.mutate()} disabled={regenM.isPending || locked}>
          {regenM.isPending ? 'Přepisuji…' : 'Přepsat'}
        </Action>
        {m.status !== 'skipped' && !locked && (
          <Action onClick={() => patchM.mutate({ status: 'skipped' })}>Přeskočit</Action>
        )}
        <Action onClick={() => suppressM.mutate()} disabled={suppressM.isPending}>
          Nekontaktovat
        </Action>
        {m.sent_at && (
          <span className="ml-auto text-[0.7rem] text-[var(--color-ink-4)]">
            odesláno {fmtRelative(m.sent_at)}
          </span>
        )}
      </div>
    </div>
  );
}

function SuppressionPanel() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const supQ = useQuery({ queryKey: ['outreach-suppressions'], queryFn: listOutreachSuppressions });
  const removeM = useMutation({
    mutationFn: (brokerId: number) => removeOutreachSuppression(brokerId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['outreach-suppressions'] }),
  });
  const rows = supQ.data?.suppressions ?? [];

  return (
    <div className="mt-8 border-t border-[var(--color-rule)] pt-4">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="text-xs tracking-[0.12em] uppercase text-[var(--color-ink-3)] hover:text-[var(--color-copper)]">
        {open ? '▾' : '▸'} seznam „nekontaktovat" ({rows.length})
      </button>
      {open && (
        rows.length === 0 ? (
          <p className="mt-2 text-sm text-[var(--color-ink-4)]">Seznam je prázdný.</p>
        ) : (
          <ul className="mt-3 space-y-1">
            {rows.map((s) => (
              <li key={s.broker_id}
                className="flex items-center justify-between gap-3 text-sm border border-[var(--color-rule-soft)] rounded-[var(--radius-sm)] px-3 py-1.5">
                <Link to={`/brokers/${s.broker_id}`} className="truncate hover:text-[var(--color-copper)]">
                  {s.broker_name ?? `#${s.broker_id}`}
                  {s.reason ? <span className="text-[var(--color-ink-4)]"> · {s.reason}</span> : null}
                </Link>
                <button type="button" onClick={() => removeM.mutate(s.broker_id)}
                  className="shrink-0 text-xs text-[var(--color-ink-3)] hover:text-[var(--color-copper)]">
                  obnovit
                </button>
              </li>
            ))}
          </ul>
        )
      )}
    </div>
  );
}

function StatusSelect({ campaignId, status, onChanged }: {
  campaignId: number; status: string; onChanged: () => void;
}) {
  const m = useMutation({
    mutationFn: (next: string) => updateOutreachCampaign(campaignId, { status: next }),
    onSuccess: onChanged,
  });
  const opts = [
    { v: 'draft', l: 'koncept' },
    { v: 'active', l: 'aktivní' },
    { v: 'archived', l: 'archiv' },
  ];
  return (
    <div className="shrink-0 flex gap-1">
      {opts.map((o) => (
        <button key={o.v} type="button" onClick={() => m.mutate(o.v)}
          className={`text-[0.7rem] tracking-[0.08em] uppercase rounded-[var(--radius-sm)] border px-2 py-1 transition-colors ${
            o.v === status
              ? 'border-[var(--color-copper)] text-[var(--color-copper)]'
              : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:border-[var(--color-copper)]'
          }`}>
          {o.l}
        </button>
      ))}
    </div>
  );
}

function Action({ children, onClick, disabled, accent }: {
  children: React.ReactNode; onClick: () => void; disabled?: boolean; accent?: boolean;
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      className={`text-xs rounded-[var(--radius-sm)] border px-3 py-1.5 transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
        accent
          ? 'border-[var(--color-copper)] text-[var(--color-copper)] hover:bg-[var(--color-copper)] hover:text-[var(--color-paper)]'
          : 'border-[var(--color-rule)] text-[var(--color-ink)] hover:border-[var(--color-copper)]'
      }`}>
      {children}
    </button>
  );
}

function Centered({ children, tone }: { children: React.ReactNode; tone?: 'error' }) {
  return (
    <div className="px-6 py-16 text-center">
      <p className={`text-sm ${tone === 'error' ? 'text-[var(--color-brick)]' : 'text-[var(--color-ink-3)]'}`}>
        {children}
      </p>
    </div>
  );
}
