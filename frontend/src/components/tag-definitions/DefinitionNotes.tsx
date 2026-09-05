import { useState } from 'react';
import type { TagLabelNote } from '@/lib/api';
import { imageSrc } from '@/lib/imageUrl';

/* The operator's reasons for changing marks on this head, gathered from the
 * training-set page — the raw material for the next definition revision.
 *
 * THE RULE THIS PANEL EXISTS TO SERVE: these are not copied into the definition
 * one line per note. The definition is read by a model and by a person, and
 * either absorbs a short general rule and drowns in a list of specifics. Read
 * the batch, find the rule it points at, state that rule once at the level of
 * the lines already there, save — and then mark the batch absorbed so the same
 * notes are never read into a later revision. */
interface Props {
  notes: ReadonlyArray<TagLabelNote>;
  loading: boolean;
  /* The version an absorb would be recorded against — the ACTIVE definition.
   * Null until the head has one, which also disables the button: a note cannot
   * be absorbed by nothing. */
  activeDefinitionId: number | null;
  activeVersion: number | null;
  onAbsorb: (noteIds: number[]) => void;
  absorbing: boolean;
}

const VERB: Record<string, string> = {
  positive: 'applies', negative: 'does not', excluded: 'left out',
};

export default function DefinitionNotes({
  notes, loading, activeDefinitionId, activeVersion, onAbsorb, absorbing,
}: Props) {
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const allIds = notes.map((n) => n.id);
  const selected = picked.size ? [...picked].filter((id) => allIds.includes(id)) : allIds;

  return (
    <section className="mt-6 border-t border-[var(--color-rule)] pt-4" data-testid="definition-notes">
      <h2 className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        Notes from the training set{notes.length > 0 && ` · ${notes.length} open`}
      </h2>
      <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
        Why you changed marks on this head. Read them together and find the one rule they
        point at; state it once, at the level of the lines already here, then mark them
        absorbed. Never one line per note &mdash; a person has to hold this definition in
        their head.
      </p>

      {loading && <p className="mt-3 text-sm text-[var(--color-ink-3)]">Loading…</p>}
      {!loading && notes.length === 0 && (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          No open notes. Change a mark on the training-set page and say why.
        </p>
      )}

      {notes.length > 0 && (
        <>
          <ul className="mt-3 flex flex-col gap-2">
            {notes.map((n) => {
              const on = picked.size === 0 || picked.has(n.id);
              return (
                <li
                  key={n.id}
                  data-testid={`note-${n.id}`}
                  className={`flex gap-2 items-start rounded-[var(--radius-sm)] border p-1.5 ${
                    on ? 'border-[var(--color-rule)]' : 'border-[var(--color-rule)] opacity-50'
                  }`}
                >
                  <input
                    type="checkbox"
                    aria-label={`include note ${n.id}`}
                    checked={on}
                    onChange={() => setPicked((prev) => {
                      const next = new Set(prev.size ? prev : allIds);
                      if (next.has(n.id)) next.delete(n.id); else next.add(n.id);
                      return next.size === allIds.length ? new Set() : next;
                    })}
                    className="mt-1"
                  />
                  <a
                    href={imageSrc({ storage_path: n.storage_path, sreality_url: '' })}
                    target="_blank"
                    rel="noreferrer"
                    className="shrink-0"
                  >
                    <img
                      src={imageSrc({ storage_path: n.storage_path, sreality_url: '' })}
                      alt=""
                      loading="lazy"
                      className="w-16 h-12 object-cover rounded-[var(--radius-xs)]"
                    />
                  </a>
                  <div className="min-w-0 flex-1">
                    <p className="text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]">
                      {n.from_state ? VERB[n.from_state] : 'untouched'} &rarr; {VERB[n.to_state]}
                    </p>
                    <p className="text-[0.8125rem] text-[var(--color-ink)] text-pretty">{n.note}</p>
                  </div>
                </li>
              );
            })}
          </ul>
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              disabled={absorbing || activeDefinitionId == null || selected.length === 0}
              onClick={() => onAbsorb(selected)}
              title={activeDefinitionId == null
                ? 'Save a definition first; a note cannot be absorbed by nothing'
                : `Record that v${activeVersion} incorporated ${selected.length === allIds.length ? 'all' : selected.length} of these`}
              className="px-2.5 py-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-copper)] text-[var(--color-ink)] disabled:opacity-40"
            >
              mark {selected.length === allIds.length ? 'all' : selected.length} absorbed
              {activeVersion != null && ` into v${activeVersion}`}
            </button>
            <span className="text-[0.7rem] text-[var(--color-ink-4)]">
              Do this after saving the revision that carries the rule.
            </span>
          </div>
        </>
      )}
    </section>
  );
}
