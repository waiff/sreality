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

import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  deleteBuildingAttachment,
  fetchBuildingAttachmentBlob,
  getBuilding,
  reExtractBuilding,
  updateBuildingInputs,
  uploadBuildingAttachment,
} from '@/lib/api';
import { fmtAbsolute, fmtArea, fmtCzk } from '@/lib/format';
import BuildingUnitEditor from '@/components/BuildingUnitEditor';
import type {
  BuildingAttachment,
  BuildingChildRun,
  BuildingRun,
  BuildingStatus,
  BuildingUnit,
} from '@/lib/types';

const ATTACHMENT_MIME = ['image/png', 'image/jpeg', 'image/webp'];
const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
const ATTACHMENT_MAX_FILES = 20;
const EDITABLE_STATUSES: ReadonlyArray<BuildingStatus> = [
  'pending', 'extracting', 'awaiting_input',
];

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
      <OperatorInputsSection building={b} onUpdated={onConfirmed} />
      <AttachmentsSection building={b} onChanged={onConfirmed} />

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

      {(b.status === 'estimating' || b.status === 'success' || b.status === 'failed') && (
        <PerUnitEstimates building={b} />
      )}

      {(b.status === 'success' || b.status === 'estimating') && (
        <RollupTotals building={b} />
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

/* ---------- operator inputs ---------- */

function OperatorInputsSection({
  building, onUpdated,
}: {
  building: BuildingRun;
  onUpdated: (next: BuildingRun) => void;
}) {
  const editable = EDITABLE_STATUSES.includes(building.status);
  const [instr, setInstr] = useState(building.special_instructions ?? '');
  const [ctx, setCtx] = useState(building.contextual_text ?? '');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setInstr(building.special_instructions ?? '');
    setCtx(building.contextual_text ?? '');
  }, [building.id, building.special_instructions, building.contextual_text]);

  const mut = useMutation<BuildingRun, ApiError>({
    mutationFn: () =>
      updateBuildingInputs(building.id, {
        special_instructions: instr.trim() || null,
        contextual_text: ctx.trim() || null,
      }),
    onSuccess: (next) => {
      setError(null);
      onUpdated(next);
    },
    onError: (e) => setError(e.message),
  });

  const dirty =
    (instr || '') !== (building.special_instructions ?? '') ||
    (ctx || '') !== (building.contextual_text ?? '');

  const hasAny =
    !!(building.special_instructions || building.contextual_text) ||
    editable;
  if (!hasAny) return null;

  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)]">
      <header className="px-5 py-3 border-b border-[var(--color-rule)]">
        <h2 className="text-[0.85rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Operator context
        </h2>
      </header>
      <div className="p-5 space-y-3">
        <div>
          <label
            htmlFor={`building-${building.id}-instr`}
            className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]"
          >
            Special instructions
          </label>
          {editable ? (
            <textarea
              id={`building-${building.id}-instr`}
              value={instr}
              onChange={(e) => setInstr(e.target.value)}
              disabled={mut.isPending}
              rows={2}
              maxLength={10_000}
              placeholder="e.g. Treat the attic as habitable. Owner says heating refurbished in 2022."
              className="mt-1 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
            />
          ) : (
            <pre className="mt-1 whitespace-pre-wrap text-[0.85rem] leading-relaxed font-sans text-[var(--color-ink)]">
              {building.special_instructions || (
                <span className="text-[var(--color-ink-3)]">—</span>
              )}
            </pre>
          )}
        </div>
        <div>
          <label
            htmlFor={`building-${building.id}-ctx`}
            className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]"
          >
            Property context
          </label>
          {editable ? (
            <textarea
              id={`building-${building.id}-ctx`}
              value={ctx}
              onChange={(e) => setCtx(e.target.value)}
              disabled={mut.isPending}
              rows={4}
              maxLength={20_000}
              placeholder="Anything the listing doesn't say — legal status, neighbours, planning, recent work, …"
              className="mt-1 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
            />
          ) : (
            <pre className="mt-1 whitespace-pre-wrap text-[0.85rem] leading-relaxed font-sans text-[var(--color-ink)]">
              {building.contextual_text || (
                <span className="text-[var(--color-ink-3)]">—</span>
              )}
            </pre>
          )}
        </div>
        {editable && (
          <div className="flex items-center justify-between gap-3">
            {error && (
              <p className="text-[0.78rem] text-[var(--color-brick)]">{error}</p>
            )}
            <p className="text-[0.7rem] text-[var(--color-ink-3)]">
              Re-extract after edits so the new context flows into the unit
              proposal.
            </p>
            <button
              type="button"
              onClick={() => mut.mutate()}
              disabled={!dirty || mut.isPending}
              className={[
                'shrink-0 px-3 py-1.5 text-[0.78rem] rounded-[var(--radius-sm)] border',
                !dirty || mut.isPending
                  ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
                  : 'bg-[var(--color-copper)] text-white border-[var(--color-copper)] hover:bg-[var(--color-copper-2)]',
              ].join(' ')}
            >
              {mut.isPending ? 'Saving…' : 'Save'}
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

/* ---------- attachments ---------- */

function AttachmentsSection({
  building, onChanged,
}: {
  building: BuildingRun;
  onChanged: (next: BuildingRun) => void;
}) {
  const editable = EDITABLE_STATUSES.includes(building.status);
  const attachments = building.attachments ?? [];
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  const refetch = async () => {
    const next = await getBuilding(building.id);
    onChanged(next);
    qc.setQueryData(['building', building.id], next);
  };

  const uploadMut = useMutation<void, ApiError, FileList>({
    mutationFn: async (files) => {
      const list = Array.from(files);
      for (const f of list) {
        if (!ATTACHMENT_MIME.includes(f.type)) {
          throw new ApiError(
            `${f.name}: unsupported type ${f.type || 'unknown'}`,
            415, null,
          );
        }
        if (f.size > ATTACHMENT_MAX_BYTES) {
          throw new ApiError(
            `${f.name}: ${(f.size / 1024 / 1024).toFixed(1)} MB > 25 MB cap`,
            413, null,
          );
        }
      }
      for (const f of list) {
        await uploadBuildingAttachment(building.id, f);
      }
    },
    onSuccess: async () => {
      setError(null);
      await refetch();
    },
    onError: (e) => setError(e.message),
  });

  const deleteMut = useMutation<void, ApiError, number>({
    mutationFn: async (attachmentId) => {
      await deleteBuildingAttachment(building.id, attachmentId);
    },
    onSuccess: async () => {
      setError(null);
      await refetch();
    },
    onError: (e) => setError(e.message),
  });

  const showSection = editable || attachments.length > 0;
  if (!showSection) return null;

  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)]">
      <header className="px-5 py-3 border-b border-[var(--color-rule)] flex items-baseline justify-between gap-4">
        <h2 className="text-[0.85rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Attachments
        </h2>
        <span className="text-[0.7rem] text-[var(--color-ink-3)]">
          {attachments.length}/{ATTACHMENT_MAX_FILES}
        </span>
      </header>
      <div className="p-5 space-y-3">
        {attachments.length === 0 && (
          <p className="text-[0.85rem] text-[var(--color-ink-3)]">
            No attachments yet. Upload floor plans, photos, or technical
            drawings — the extractor will read them on the next pass.
          </p>
        )}
        {attachments.length > 0 && (
          <ul className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
            {attachments.map((a) => (
              <AttachmentCard
                key={a.id}
                buildingId={building.id}
                attachment={a}
                onDelete={
                  editable
                    ? () => deleteMut.mutate(a.id)
                    : undefined
                }
                deleting={deleteMut.isPending}
              />
            ))}
          </ul>
        )}
        {editable && (
          <div>
            <input
              type="file"
              multiple
              accept={ATTACHMENT_MIME.join(',')}
              disabled={uploadMut.isPending}
              onChange={(e) => {
                if (e.target.files && e.target.files.length > 0) {
                  uploadMut.mutate(e.target.files);
                }
                e.target.value = '';
              }}
              className="block text-[0.78rem] text-[var(--color-ink-2)] file:mr-2 file:rounded-[var(--radius-sm)] file:border file:border-[var(--color-rule)] file:bg-[var(--color-inset)] file:text-[var(--color-ink)] file:px-3 file:py-1.5 file:text-[0.78rem] file:cursor-pointer hover:file:bg-[var(--color-paper)] disabled:opacity-60"
            />
            <p className="mt-1 text-[0.7rem] text-[var(--color-ink-3)]">
              {uploadMut.isPending
                ? 'Uploading…'
                : 'PNG / JPEG / WebP. Up to 20 files, 25 MB each.'}
            </p>
          </div>
        )}
        {error && (
          <p className="text-[0.78rem] text-[var(--color-brick)]">{error}</p>
        )}
      </div>
    </section>
  );
}

function AttachmentCard({
  buildingId, attachment, onDelete, deleting,
}: {
  buildingId: number;
  attachment: BuildingAttachment;
  onDelete?: () => void;
  deleting: boolean;
}) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let revoked = false;
    let objectUrl: string | null = null;
    (async () => {
      try {
        const blob = await fetchBuildingAttachmentBlob(buildingId, attachment.id);
        if (revoked) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      } catch {
        // Leave src=null; the card renders a filename-only fallback.
      }
    })();
    return () => {
      revoked = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [buildingId, attachment.id]);

  return (
    <li className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-inset)] overflow-hidden">
      <div className="aspect-[4/3] bg-[var(--color-paper)] flex items-center justify-center">
        {src ? (
          <img
            src={src}
            alt={attachment.filename}
            className="max-h-full max-w-full object-contain"
          />
        ) : (
          <span className="text-[0.7rem] text-[var(--color-ink-3)]">
            preview…
          </span>
        )}
      </div>
      <div className="px-3 py-2 flex items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[0.78rem] text-[var(--color-ink)]" title={attachment.filename}>
            {attachment.filename}
          </p>
          <p className="text-[0.7rem] text-[var(--color-ink-3)]">
            {(attachment.byte_size / 1024).toFixed(0)} KB
            {attachment.width_px && attachment.height_px
              ? ` · ${attachment.width_px}×${attachment.height_px}`
              : ''}
          </p>
        </div>
        {onDelete && (
          <button
            type="button"
            onClick={onDelete}
            disabled={deleting}
            className="shrink-0 text-[var(--color-ink-3)] hover:text-[var(--color-brick)] disabled:opacity-40 text-sm"
            aria-label={`Delete ${attachment.filename}`}
          >
            ×
          </button>
        )}
      </div>
    </li>
  );
}

/* ---------- per-unit child estimations (B2) ---------- */

function PerUnitEstimates({ building }: { building: BuildingRun }) {
  const children = building.children ?? [];
  const units = building.units ?? [];

  if (children.length === 0 && building.status === 'estimating') {
    return (
      <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] p-5">
        <p className="text-[0.85rem] text-[var(--color-ink-3)]">
          Per-unit estimations are running…
        </p>
      </section>
    );
  }
  if (children.length === 0) return null;

  const unitById = new Map(units.map((u) => [u.unit_id, u] as const));

  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)]">
      <header className="px-5 py-3 border-b border-[var(--color-rule)] flex items-baseline justify-between gap-4">
        <h2 className="text-[0.85rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Per-unit estimates
        </h2>
        <span className="text-[0.7rem] text-[var(--color-ink-3)]">
          {children.length}
        </span>
      </header>
      <ul className="divide-y divide-[var(--color-rule)]">
        {children.map((child) => (
          <ChildEstimateRow
            key={child.id}
            child={child}
            unit={child.building_unit_id ? unitById.get(child.building_unit_id) ?? null : null}
          />
        ))}
      </ul>
    </section>
  );
}

function ChildEstimateRow({
  child, unit,
}: {
  child: BuildingChildRun;
  unit: BuildingUnit | null;
}) {
  const label =
    unit?.label ||
    (unit?.disposition && unit?.floor ? `${unit.disposition} · ${unit.floor}` : null) ||
    child.building_unit_id ||
    `run #${child.id}`;
  return (
    <li className="px-5 py-3 flex items-center justify-between gap-4">
      <div className="min-w-0 flex-1">
        <Link
          to={`/estimation/${child.id}`}
          className="text-[0.9rem] text-[var(--color-ink)] hover:text-[var(--color-copper)]"
        >
          {label}
        </Link>
        {child.estimate_kind && (
          <span className="ml-2 text-[0.7rem] uppercase tracking-[0.15em] text-[var(--color-ink-3)]">
            {child.estimate_kind}
          </span>
        )}
        {unit?.area_m2 != null && (
          <span className="ml-2 text-[0.78rem] text-[var(--color-ink-3)]">
            {fmtArea(unit.area_m2)}
          </span>
        )}
        {unit?.is_potential && (
          <span className="ml-2 text-[0.7rem] uppercase tracking-[0.15em] text-[var(--color-copper)]">
            potential
          </span>
        )}
      </div>
      <ChildReadout child={child} />
      <ChildStatusBadge status={child.status} />
    </li>
  );
}

function ChildReadout({ child }: { child: BuildingChildRun }) {
  if (child.status === 'success') {
    if (child.estimate_kind === 'sale') {
      const median = child.estimated_sale_price_czk;
      const p25 = child.sale_p25_czk;
      const p75 = child.sale_p75_czk;
      if (median == null) {
        return <span className="text-[0.78rem] text-[var(--color-ink-3)]">no estimate</span>;
      }
      return (
        <div className="text-right tabular-nums">
          <span className="text-[0.9rem] text-[var(--color-ink)]">{fmtCzk(median)}</span>
          {p25 != null && p75 != null && (
            <p className="text-[0.7rem] text-[var(--color-ink-3)]">
              {fmtCzk(p25)} – {fmtCzk(p75)}
            </p>
          )}
        </div>
      );
    }
    const median = child.estimated_monthly_rent_czk;
    const p25 = child.rent_p25_czk;
    const p75 = child.rent_p75_czk;
    if (median == null) {
      return <span className="text-[0.78rem] text-[var(--color-ink-3)]">no estimate</span>;
    }
    return (
      <div className="text-right tabular-nums">
        <span className="text-[0.9rem] text-[var(--color-ink)]">
          {fmtCzk(median)}<span className="text-[var(--color-ink-3)]">/mo</span>
        </span>
        {p25 != null && p75 != null && (
          <p className="text-[0.7rem] text-[var(--color-ink-3)]">
            {fmtCzk(p25)} – {fmtCzk(p75)}
          </p>
        )}
      </div>
    );
  }
  if (child.status === 'failed') {
    return (
      <span
        className="text-[0.78rem] text-[var(--color-brick)] max-w-[18rem] truncate"
        title={child.error_message ?? undefined}
      >
        {child.error_message ?? 'failed'}
      </span>
    );
  }
  return (
    <span className="text-[0.78rem] text-[var(--color-ink-3)]">running…</span>
  );
}

function ChildStatusBadge({ status }: { status: BuildingChildRun['status'] }) {
  const styles: Record<BuildingChildRun['status'], string> = {
    pending: 'text-[var(--color-ink-3)]',
    running: 'text-[var(--color-ink-3)]',
    success: 'text-[var(--color-ink-2)]',
    failed: 'text-[var(--color-brick)]',
  };
  return (
    <span className={`text-[0.7rem] tracking-[0.15em] uppercase ${styles[status]}`}>
      {status}
    </span>
  );
}

/* ---------- rollup totals (B2) ---------- */

function RollupTotals({ building }: { building: BuildingRun }) {
  const hasRent =
    building.total_rent_p25_czk != null ||
    building.total_rent_p50_czk != null ||
    building.total_rent_p75_czk != null;
  const hasSale =
    building.total_sale_p25_czk != null ||
    building.total_sale_p50_czk != null ||
    building.total_sale_p75_czk != null;
  if (!hasRent && !hasSale) return null;
  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] p-5">
      <header className="flex items-baseline justify-between gap-4">
        <h2 className="text-[0.85rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Building totals
        </h2>
        <span className="text-[0.7rem] text-[var(--color-ink-3)]">
          sum of per-unit medians; IQR endpoints summed
        </span>
      </header>
      <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-6">
        {hasRent && (
          <TotalsCell
            label="monthly rent"
            unit="/mo"
            p25={building.total_rent_p25_czk}
            p50={building.total_rent_p50_czk}
            p75={building.total_rent_p75_czk}
          />
        )}
        {hasSale && (
          <TotalsCell
            label="sale price"
            unit={null}
            p25={building.total_sale_p25_czk}
            p50={building.total_sale_p50_czk}
            p75={building.total_sale_p75_czk}
          />
        )}
      </div>
    </section>
  );
}

function TotalsCell({
  label, unit, p25, p50, p75,
}: {
  label: string;
  unit: string | null;
  p25: number | null;
  p50: number | null;
  p75: number | null;
}) {
  return (
    <div>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {label}
      </p>
      <p
        className="mt-1 text-2xl tabular-nums"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {p50 != null ? fmtCzk(p50) : '—'}
        {unit && (
          <span className="text-[var(--color-ink-3)] text-base ml-1">{unit}</span>
        )}
      </p>
      {p25 != null && p75 != null && (
        <p className="mt-1 text-[0.85rem] text-[var(--color-ink-3)] tabular-nums">
          {fmtCzk(p25)} – {fmtCzk(p75)}
        </p>
      )}
    </div>
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
