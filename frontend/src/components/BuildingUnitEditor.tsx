/* BuildingUnitEditor — table editor for the unit list of a building_run.
 *
 * Initial rows come from `units_proposal.units` (the extractor's output)
 * or `units` (operator-confirmed list). The operator can edit any field,
 * add a new row (with `source='user_added'`), remove a row, then submit
 * to `POST /buildings/{id}/confirm_units`.
 *
 * unit_id is preserved across edits so any child estimations B2
 * eventually fans out stay linked to the same conceptual unit.
 * Newly-added units get an auto-incremented unit_id (`uN+1`).
 */

import { useEffect, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  ApiError,
  confirmBuildingUnits,
} from '@/lib/api';
import type { BuildingRun, BuildingUnit } from '@/lib/types';

interface Props {
  building: BuildingRun;
  onConfirmed: (next: BuildingRun) => void;
}

const _CONDITION_OPTS: { value: string; label: string }[] = [
  { value: '', label: '—' },
  { value: 'novostavba', label: 'novostavba' },
  { value: 'po_rekonstrukci', label: 'po rekonstrukci' },
  { value: 'velmi_dobry', label: 'velmi dobrý' },
  { value: 'dobry', label: 'dobrý' },
  { value: 'pred_rekonstrukci', label: 'před rekonstrukcí' },
  { value: 'k_demolici', label: 'k demolici' },
  { value: 'unknown', label: 'unknown' },
];

export default function BuildingUnitEditor({ building, onConfirmed }: Props) {
  const initial = pickInitialUnits(building);
  const [rows, setRows] = useState<BuildingUnit[]>(initial);

  // Re-seed when the underlying row changes (e.g. after re_extract).
  useEffect(() => {
    setRows(pickInitialUnits(building));
  }, [building.id, building.units_proposal, building.units]);

  const confirmMut = useMutation<BuildingRun, ApiError, BuildingUnit[]>({
    mutationFn: (units) =>
      confirmBuildingUnits(building.id, { units }),
    onSuccess: (next) => onConfirmed(next),
  });

  const patch = (idx: number, fields: Partial<BuildingUnit>) =>
    setRows((rs) =>
      rs.map((r, i) => (i === idx ? { ...r, ...fields } : r)),
    );

  const remove = (idx: number) =>
    setRows((rs) => rs.filter((_, i) => i !== idx));

  const add = () =>
    setRows((rs) => [
      ...rs,
      {
        unit_id: nextUnitId(rs),
        label: null,
        floor: null,
        area_m2: null,
        disposition: null,
        condition: null,
        is_potential: false,
        source: 'user_added',
        notes: null,
      },
    ]);

  const submit = () => {
    if (rows.length === 0) return;
    if (confirmMut.isPending) return;
    confirmMut.mutate(rows);
  };

  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)]">
      <header className="px-5 py-3 border-b border-[var(--color-rule)]">
        <h2 className="text-[1rem]" style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}>
          Review units
        </h2>
        <p className="text-[0.78rem] text-[var(--color-ink-3)] mt-1">
          The extractor proposed {initial.length} unit{initial.length === 1 ? '' : 's'}.
          Edit any field, add or remove rows, then confirm to start per-unit estimations.
        </p>
      </header>

      <div className="overflow-x-auto">
        <table className="w-full text-[0.83rem]">
          <thead>
            <tr className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
              <th className="text-left px-3 py-2">Id</th>
              <th className="text-left px-3 py-2">Label</th>
              <th className="text-left px-3 py-2">Floor</th>
              <th className="text-right px-3 py-2">Area m²</th>
              <th className="text-left px-3 py-2">Disposition</th>
              <th className="text-left px-3 py-2">Condition</th>
              <th className="text-left px-3 py-2">Potential?</th>
              <th className="text-left px-3 py-2">Notes</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.unit_id + ':' + i} className="border-t border-[var(--color-rule)]">
                <td className="px-3 py-2 text-[var(--color-ink-3)] font-mono">
                  {r.unit_id}
                </td>
                <td className="px-3 py-2">
                  <TextCell
                    value={r.label}
                    onChange={(v) => patch(i, { label: v })}
                  />
                </td>
                <td className="px-3 py-2">
                  <TextCell
                    value={r.floor}
                    onChange={(v) => patch(i, { floor: v })}
                  />
                </td>
                <td className="px-3 py-2 text-right">
                  <NumberCell
                    value={r.area_m2}
                    onChange={(v) => patch(i, { area_m2: v })}
                  />
                </td>
                <td className="px-3 py-2">
                  <TextCell
                    value={r.disposition}
                    onChange={(v) => patch(i, { disposition: v })}
                  />
                </td>
                <td className="px-3 py-2">
                  <select
                    value={r.condition ?? ''}
                    onChange={(e) =>
                      patch(i, { condition: e.target.value || null })
                    }
                    className="w-full bg-transparent border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-2 py-1 text-[0.83rem]"
                  >
                    {_CONDITION_OPTS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </td>
                <td className="px-3 py-2">
                  <label className="inline-flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={r.is_potential}
                      onChange={(e) =>
                        patch(i, { is_potential: e.target.checked })
                      }
                    />
                    <span className="text-[0.78rem] text-[var(--color-ink-3)]">
                      potential
                    </span>
                  </label>
                </td>
                <td className="px-3 py-2">
                  <TextCell
                    value={r.notes}
                    onChange={(v) => patch(i, { notes: v })}
                  />
                </td>
                <td className="px-3 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => remove(i)}
                    className="text-[var(--color-ink-3)] hover:text-[var(--color-brick)] text-[0.78rem]"
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <footer className="flex items-center justify-between px-5 py-3 border-t border-[var(--color-rule)]">
        <button
          type="button"
          onClick={add}
          disabled={confirmMut.isPending}
          className="text-[0.83rem] text-[var(--color-copper)] hover:text-[var(--color-copper-2)] disabled:opacity-50"
        >
          + Add unit
        </button>

        <div className="flex items-center gap-3">
          {confirmMut.error && (
            <span className="text-[0.78rem] text-[var(--color-brick)]">
              {confirmMut.error.message}
            </span>
          )}
          <button
            type="button"
            onClick={submit}
            disabled={rows.length === 0 || confirmMut.isPending}
            className={[
              'px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
              rows.length === 0 || confirmMut.isPending
                ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
                : 'bg-[var(--color-copper)] text-white border-[var(--color-copper)] hover:bg-[var(--color-copper-2)] hover:border-[var(--color-copper-2)]',
            ].join(' ')}
          >
            {confirmMut.isPending ? 'Confirming…' : 'Confirm units'}
          </button>
        </div>
      </footer>
    </section>
  );
}

/* ---------- cell editors ---------- */

function TextCell({
  value, onChange,
}: {
  value: string | null;
  onChange: (v: string | null) => void;
}) {
  return (
    <input
      type="text"
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value === '' ? null : e.target.value)}
      className="w-full bg-transparent border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-2 py-1 text-[0.83rem]"
    />
  );
}

function NumberCell({
  value, onChange,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
}) {
  return (
    <input
      type="number"
      min={0}
      step="0.1"
      value={value ?? ''}
      onChange={(e) => {
        const v = e.target.value;
        onChange(v === '' ? null : Number(v));
      }}
      className="w-24 bg-transparent border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-2 py-1 text-right text-[0.83rem]"
    />
  );
}

/* ---------- helpers ---------- */

function pickInitialUnits(b: BuildingRun): BuildingUnit[] {
  if (b.units && b.units.length > 0) return b.units;
  if (b.units_proposal && b.units_proposal.units.length > 0) {
    return b.units_proposal.units;
  }
  return [];
}

function nextUnitId(rs: BuildingUnit[]): string {
  let maxN = 0;
  for (const r of rs) {
    const m = /^u(\d+)$/.exec(r.unit_id);
    if (m) {
      const n = Number(m[1]);
      if (n > maxN) maxN = n;
    }
  }
  return `u${maxN + 1}`;
}
