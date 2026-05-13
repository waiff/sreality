/* BuildingDetail (`/building/:id`) — Phase B1 read-only-plus-confirmation view.
 *
 * Shows the building's parse-derived subject summary, current status,
 * the extractor's warnings, and (when status='awaiting_input') the
 * BuildingUnitEditor for operator confirmation. When the row is in a
 * later state the unit list renders read-only.
 *
 * Per-unit estimate strips + rollup totals + the business-case tab
 * land with B2/B3 — this page intentionally stops at the confirmation
 * step so B1 ships standalone.
 */

import { useMemo } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  getBuilding,
  reExtractBuilding,
} from '@/lib/api';
import { fmtAbsolute, fmtArea } from '@/lib/format';
import BuildingUnitEditor from '@/components/BuildingUnitEditor';
import type {
  BuildingRun,
  BuildingStatus,
  BuildingUnit,
} from '@/lib/types';

const buildingKey = (id: number) => ['building', id] as const;

export default function BuildingDetail() {
  const { id: idParam } = useParams();
  const id = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null;
  const qc = useQueryClient();

  const runQ = useQuery<BuildingRun, Error>({
    queryKey: id != null ? buildingKey(id) : ['building', null],
    queryFn: () => getBuilding(id as number),
    enabled: id != null,
    staleTime: 30_000,
  });

  const reExtractMut = useMutation<BuildingRun, ApiError, void>({
    mutationFn: () => reExtractBuilding(id as number),
    onSuccess: (next) => {
      if (id != null) qc.setQueryData(buildingKey(id), next);
    },
  });

  const onConfirmed = (next: BuildingRun) => {
    if (id != null) qc.setQueryData(buildingKey(id), next);
  };

  if (id == null) {
    return <Page><p className="text-sm">Invalid id.</p></Page>;
  }
  if (runQ.isLoading) {
    return <Page><p className="text-sm text-[var(--color-ink-3)]">Loading…</p></Page>;
  }
  if (runQ.error) {
    return (
      <Page>
        <p className="text-sm text-[var(--color-brick)]">
          {runQ.error.message}
        </p>
      </Page>
    );
  }

  const b = runQ.data!;
  return (
    <Page>
      <Crumb id={b.id} />
      <Header building={b} />
      <SubjectBlock building={b} />
      <Warnings building={b} />

      {b.status === 'awaiting_input' && (
        <div className="mt-2 flex justify-end">
          <button
            type="button"
            onClick={() => reExtractMut.mutate()}
            disabled={reExtractMut.isPending}
            className="text-[0.78rem] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] disabled:opacity-50"
          >
            {reExtractMut.isPending ? 'Re-extracting…' : 'Re-extract from snapshot'}
          </button>
        </div>
      )}

      {b.status === 'awaiting_input' ? (
        <BuildingUnitEditor building={b} onConfirmed={onConfirmed} />
      ) : (
        <ReadOnlyUnits building={b} />
      )}

      {b.status === 'failed' && b.error_message && (
        <div className="mt-6 p-4 rounded-[var(--radius-md)] border border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)] text-[var(--color-brick)] text-sm">
          <p className="font-medium">Building decomposition failed.</p>
          <p className="mt-1 text-[0.83rem]">{b.error_message}</p>
        </div>
      )}
    </Page>
  );
}

/* ---------- subcomponents ---------- */

function Page({ children }: { children: React.ReactNode }) {
  return (
    <div className="max-w-5xl mx-auto px-6 py-8">
      {children}
    </div>
  );
}

function Crumb({ id }: { id: number }) {
  return (
    <nav className="text-[0.7rem] tracking-[0.16em] uppercase text-[var(--color-ink-3)]">
      <Link to="/estimations" className="hover:text-[var(--color-ink)]">
        Estimations
      </Link>
      <span className="mx-2">/</span>
      <span>Building {id}</span>
    </nav>
  );
}

function Header({ building }: { building: BuildingRun }) {
  return (
    <header className="mt-3 flex items-start justify-between gap-4">
      <div>
        <h1
          className="text-[1.8rem] leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          Building #{building.id}
        </h1>
        <p className="mt-1 text-[0.83rem] text-[var(--color-ink-3)]">
          Created {fmtAbsolute(building.created_at)}
          {building.input_url ? (
            <>
              {' · '}
              <a
                href={building.input_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[var(--color-copper)] hover:text-[var(--color-copper-2)]"
              >
                source
              </a>
            </>
          ) : null}
        </p>
      </div>
      <StatusBadge status={building.status} />
    </header>
  );
}

function StatusBadge({ status }: { status: BuildingStatus }) {
  const palette: Record<BuildingStatus, { fg: string; bg: string }> = {
    pending:        { fg: 'var(--color-ink-3)', bg: 'var(--color-inset)' },
    extracting:     { fg: 'var(--color-copper)', bg: 'var(--color-copper-soft)' },
    awaiting_input: { fg: 'var(--color-ochre)', bg: 'var(--color-ochre-soft)' },
    estimating:     { fg: 'var(--color-copper)', bg: 'var(--color-copper-soft)' },
    success:        { fg: 'var(--color-sage)', bg: 'var(--color-sage-soft)' },
    failed:         { fg: 'var(--color-brick)', bg: 'var(--color-brick-soft)' },
  };
  const c = palette[status];
  return (
    <span
      className="shrink-0 px-2.5 py-1 rounded-[var(--radius-sm)] text-[0.7rem] tracking-[0.14em] uppercase"
      style={{ color: c.fg, background: c.bg }}
    >
      {status.replace('_', ' ')}
    </span>
  );
}

function SubjectBlock({ building }: { building: BuildingRun }) {
  const subject = building.subject_summary as
    | (Record<string, unknown> & {
        fields?: Record<string, unknown>;
        building?: Record<string, unknown>;
      })
    | null;
  const fields = (subject?.fields as Record<string, unknown>) || {};
  const buildingFacts = (subject?.building as Record<string, unknown>) || {};

  const rows = useMemo(
    () =>
      [
        ['Locality', fields.locality ?? fields.district],
        ['Category', combine(fields.category_main, fields.category_type)],
        ['Estate area', fmtAreaCell(fields.estate_area)],
        ['Usable area', fmtAreaCell(fields.usable_area)],
        ['Year built', buildingFacts.year_built],
        ['Floors', buildingFacts.floor_count],
        ['Construction', buildingFacts.construction_type],
        ['Condition', buildingFacts.condition ?? fields.condition],
        ['Energy', fields.energy_rating],
        ['Ownership', fields.ownership],
      ].filter(([, v]) => v != null && v !== ''),
    [fields, buildingFacts],
  );

  if (rows.length === 0) return null;
  return (
    <section className="mt-5 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-5 py-4">
      <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Subject
      </h2>
      <dl className="mt-3 grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-2 text-[0.85rem]">
        {rows.map(([k, v]) => (
          <div key={k as string}>
            <dt className="text-[var(--color-ink-3)] text-[0.75rem]">{k as string}</dt>
            <dd className="text-[var(--color-ink)]">{String(v)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Warnings({ building }: { building: BuildingRun }) {
  const w = building.warnings ?? [];
  if (w.length === 0) return null;
  return (
    <section className="mt-3 px-4 py-3 rounded-[var(--radius-sm)] border border-[var(--color-ochre)]/40 bg-[var(--color-ochre-soft)] text-[var(--color-ochre)] text-[0.83rem]">
      <p className="font-medium">Heads-up</p>
      <ul className="mt-1 list-disc list-inside space-y-1">
        {w.map((s, i) => <li key={i}>{s}</li>)}
      </ul>
    </section>
  );
}

function ReadOnlyUnits({ building }: { building: BuildingRun }) {
  const units: BuildingUnit[] =
    building.units && building.units.length > 0
      ? building.units
      : building.units_proposal?.units ?? [];
  if (units.length === 0) return null;
  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)]">
      <header className="px-5 py-3 border-b border-[var(--color-rule)]">
        <h2 className="text-[1rem]" style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}>
          Units
        </h2>
        <p className="text-[0.78rem] text-[var(--color-ink-3)] mt-1">
          {building.units
            ? 'Operator-confirmed list. Per-unit estimates land here when B2 ships.'
            : 'Extractor proposal — not yet confirmed.'}
        </p>
      </header>
      <table className="w-full text-[0.83rem]">
        <thead>
          <tr className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
            <th className="text-left px-4 py-2">Id</th>
            <th className="text-left px-4 py-2">Label</th>
            <th className="text-left px-4 py-2">Floor</th>
            <th className="text-right px-4 py-2">Area m²</th>
            <th className="text-left px-4 py-2">Disposition</th>
            <th className="text-left px-4 py-2">Condition</th>
            <th className="text-left px-4 py-2">Notes</th>
          </tr>
        </thead>
        <tbody>
          {units.map((u) => (
            <tr key={u.unit_id} className="border-t border-[var(--color-rule)]">
              <td className="px-4 py-2 font-mono text-[var(--color-ink-3)]">{u.unit_id}</td>
              <td className="px-4 py-2">{u.label ?? '—'}</td>
              <td className="px-4 py-2">{u.floor ?? '—'}</td>
              <td className="px-4 py-2 text-right">{u.area_m2 != null ? fmtArea(u.area_m2) : '—'}</td>
              <td className="px-4 py-2">{u.disposition ?? '—'}</td>
              <td className="px-4 py-2">
                {u.condition ?? '—'}
                {u.is_potential && (
                  <span className="ml-2 text-[0.7rem] tracking-[0.12em] uppercase text-[var(--color-ochre)]">
                    potential
                  </span>
                )}
              </td>
              <td className="px-4 py-2 text-[var(--color-ink-3)]">{u.notes ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/* ---------- helpers ---------- */

function combine(a: unknown, b: unknown): string | null {
  if (a == null && b == null) return null;
  return [a, b].filter((x) => x != null).join(' / ') || null;
}

function fmtAreaCell(v: unknown): string | null {
  if (typeof v !== 'number') return null;
  return fmtArea(v);
}
