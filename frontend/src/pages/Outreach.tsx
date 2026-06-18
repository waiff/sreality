import { useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createOutreachCampaign,
  listOutreachCampaigns,
  type OutreachCampaign,
} from '../lib/api';
import { chipsToGeoArrays } from '../lib/brokers';
import type { DistrictChip } from '../lib/filters';
import { LocationTypeahead } from '../components/filter-controls/LocationTypeahead';
import { PickButton } from '../components/controls';
import { fmtCount, fmtRelative } from '../lib/format';

const CATEGORY_OPTIONS: ReadonlyArray<{ value: string | null; label: string }> = [
  { value: 'byt', label: 'Byty' },
  { value: 'dum', label: 'Domy' },
  { value: 'pozemek', label: 'Pozemky' },
  { value: 'komercni', label: 'Komerční' },
  { value: null, label: 'Vše' },
];
const OFFER_OPTIONS: ReadonlyArray<{ value: string | null; label: string }> = [
  { value: 'prodej', label: 'Prodej' },
  { value: 'pronajem', label: 'Pronájem' },
  { value: null, label: 'Vše' },
];

export default function Outreach() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const listQ = useQuery({ queryKey: ['outreach-campaigns'], queryFn: listOutreachCampaigns });

  const [name, setName] = useState('');
  const [goal, setGoal] = useState('');
  const [guidance, setGuidance] = useState('');
  const [districts, setDistricts] = useState<DistrictChip[]>([]);
  const [categoryMain, setCategoryMain] = useState<string | null>('byt');
  const [categoryType, setCategoryType] = useState<string | null>('prodej');
  const geo = useMemo(() => chipsToGeoArrays(districts), [districts]);

  const createM = useMutation({
    mutationFn: () =>
      createOutreachCampaign({
        name: name.trim(),
        goal: goal.trim() || null,
        guidance: guidance.trim() || null,
        target: {
          region_ids: geo.regionIds,
          okres_ids: geo.okresIds,
          obec_ids: geo.obecIds,
          category_main: categoryMain,
          category_type: categoryType,
          metric: 'active_property_count',
        },
      }),
    onSuccess: (c) => {
      qc.invalidateQueries({ queryKey: ['outreach-campaigns'] });
      navigate(`/outreach/${c.id}`);
    },
  });

  const campaigns = listQ.data?.campaigns ?? [];

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto text-[var(--color-ink)]">
      <header>
        <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          broker intelligence
        </p>
        <h1 className="mt-1 text-2xl font-[family-name:var(--font-display)]">Oslovení</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-3)] max-w-2xl">
          Cílené oslovení makléřů k získání nemovitostí „pod rukou". Koncept napíše
          asistent, vy ho upravíte, schválíte a&nbsp;odešlete ručně. Žádné automatické
          rozesílání — každý dopis posíláte sami.
        </p>
      </header>

      {/* New campaign */}
      <div className="mt-6 border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-4">
        <p className="text-xs tracking-[0.12em] uppercase text-[var(--color-ink-3)]">
          nová kampaň
        </p>
        <div className="mt-3 grid gap-x-6 gap-y-3 sm:grid-cols-2">
          <Field label="Název" className="sm:col-span-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Off-market byty Praha"
              className={inputCls}
            />
          </Field>
          <Field label="Cíl (volně)" className="sm:col-span-2">
            <textarea
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              rows={2}
              placeholder="Hledám byty na prodej v Praze mimo veřejnou nabídku…"
              className={inputCls}
            />
          </Field>
          <Field label="Pokyny pro koncept (volitelné)" className="sm:col-span-2">
            <textarea
              value={guidance}
              onChange={(e) => setGuidance(e.target.value)}
              rows={2}
              placeholder="Tón, čím se odlišit, co nabídnout výměnou…"
              className={inputCls}
            />
          </Field>
          <Field label="Lokalita" className="min-w-[16rem]">
            <LocationTypeahead value={districts} onChange={(n) => setDistricts(n ?? [])} />
          </Field>
          <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
            <Field label="Typ">
              <Segmented options={CATEGORY_OPTIONS} value={categoryMain} onChange={setCategoryMain} />
            </Field>
            <Field label="Nabídka">
              <Segmented options={OFFER_OPTIONS} value={categoryType} onChange={setCategoryType} />
            </Field>
          </div>
        </div>
        <div className="mt-4 flex items-center gap-3">
          <button
            type="button"
            disabled={name.trim().length < 2 || createM.isPending}
            onClick={() => createM.mutate()}
            className="text-sm rounded-[var(--radius-sm)] border border-[var(--color-copper)] bg-[var(--color-copper-soft)] px-4 py-2 text-[var(--color-ink)] hover:bg-[var(--color-copper)] hover:text-[var(--color-paper)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {createM.isPending ? 'Zakládám…' : 'Založit kampaň'}
          </button>
          {createM.isError && (
            <span className="text-xs text-[var(--color-brick)]">
              {(createM.error as Error).message}
            </span>
          )}
        </div>
      </div>

      {/* Campaign ledger */}
      <div className="mt-7">
        <p className="text-xs tracking-[0.12em] uppercase text-[var(--color-ink-3)]">
          kampaně
        </p>
        {listQ.isLoading ? (
          <p className="mt-4 text-sm text-[var(--color-ink-3)]">Načítám…</p>
        ) : listQ.isError ? (
          <p className="mt-4 text-sm text-[var(--color-brick)]">{(listQ.error as Error).message}</p>
        ) : campaigns.length === 0 ? (
          <p className="mt-4 text-sm text-[var(--color-ink-4)]">
            Zatím žádné kampaně. Založte první výše.
          </p>
        ) : (
          <ul className="mt-3 space-y-2">
            {campaigns.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => navigate(`/outreach/${c.id}`)}
                  className="w-full text-left border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-3 hover:border-[var(--color-copper)] transition-colors"
                >
                  <CampaignRow c={c} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function CampaignRow({ c }: { c: OutreachCampaign }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm text-[var(--color-ink)]">{c.name}</span>
          <StatusChip status={c.status} />
        </div>
        <p className="mt-0.5 truncate text-xs text-[var(--color-ink-3)]">
          {c.goal || 'bez popisu'} · {fmtRelative(c.created_at)}
        </p>
      </div>
      <div className="shrink-0 flex items-center gap-3 text-xs font-[family-name:var(--font-mono)] tabular-nums text-[var(--color-ink-3)]">
        <Count label="konceptů" n={c.draft_count} />
        <Count label="schváleno" n={c.approved_count} />
        <Count label="odesláno" n={c.sent_count} accent />
      </div>
    </div>
  );
}

function Count({ label, n, accent }: { label: string; n?: number; accent?: boolean }) {
  return (
    <span className="flex flex-col items-end leading-tight">
      <span className={accent ? 'text-[var(--color-copper)]' : 'text-[var(--color-ink)]'}>
        {fmtCount(n ?? 0)}
      </span>
      <span className="text-[0.62rem] tracking-wide text-[var(--color-ink-4)]">{label}</span>
    </span>
  );
}

export function StatusChip({ status }: { status: string }) {
  const label: Record<string, string> = {
    draft: 'koncept', active: 'aktivní', archived: 'archiv',
    approved: 'schváleno', sent: 'odesláno', skipped: 'přeskočeno',
    replied: 'odpověděl', bounced: 'nedoručeno',
  };
  const accent = status === 'sent' || status === 'replied' || status === 'active';
  return (
    <span
      className={`shrink-0 text-[0.62rem] tracking-[0.1em] uppercase rounded-[var(--radius-sm)] border px-1.5 py-0.5 ${
        accent
          ? 'border-[var(--color-copper)] text-[var(--color-copper)]'
          : 'border-[var(--color-rule)] text-[var(--color-ink-3)]'
      }`}
    >
      {label[status] ?? status}
    </span>
  );
}

const inputCls =
  'w-full text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-3)] px-3 py-2 text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:ring-2 focus:ring-[var(--color-focus)]';

function Field({ label, children, className = '' }: { label: string; children: ReactNode; className?: string }) {
  return (
    <label className={`flex flex-col gap-1 ${className}`}>
      <span className="text-[0.7rem] tracking-[0.08em] uppercase text-[var(--color-ink-3)]">{label}</span>
      {children}
    </label>
  );
}

function Segmented<T extends string | number | null>({
  options, value, onChange,
}: {
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {options.map((o) => (
        <PickButton key={String(o.value)} on={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </PickButton>
      ))}
    </div>
  );
}
