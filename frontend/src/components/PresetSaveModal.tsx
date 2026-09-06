/* Name + colour dialog for saving / updating / editing a Browse filter preset.
 *
 * One small modal serves all flows (Browse owns which one is open and what to
 * do on submit). The colour swatches reuse the shared tag palette (TAG_COLORS)
 * so presets and tags speak one colour vocabulary. When a map area is currently
 * applied it offers an "Include current map area" toggle so the operator can
 * decide whether the viewport bounding box is part of the preset; the
 * metadata-only edit flow hides it (filters are untouched). Modelled on
 * CreateWatchdogModal for visual consistency. */

import { useEffect, useId, useRef, useState } from 'react';

import { type TagColor } from '@/lib/types';
import Dialog from '@/components/Dialog';
import TagColorPicker from '@/components/TagColorPicker';

export interface PresetSaveModalProps {
  title: string;
  initialName: string;
  initialColor: TagColor | null;
  submitLabel: string;
  /* Show the "Include current map area" toggle (a map area must be applied). */
  showMapAreaToggle: boolean;
  initialIncludeMapArea: boolean;
  busy: boolean;
  error: string | null;
  onSubmit: (name: string, includeMapArea: boolean, color: TagColor | null) => void;
  onClose: () => void;
}

export default function PresetSaveModal({
  title,
  initialName,
  initialColor,
  submitLabel,
  showMapAreaToggle,
  initialIncludeMapArea,
  busy,
  error,
  onSubmit,
  onClose,
}: PresetSaveModalProps) {
  const [name, setName] = useState(initialName);
  const [includeMapArea, setIncludeMapArea] = useState(initialIncludeMapArea);
  const [color, setColor] = useState<TagColor | null>(initialColor);
  const inputRef = useRef<HTMLInputElement>(null);
  const titleId = useId();

  /* Focus is the primitive's (`initialFocus` below). What this adds is the
   * SELECTION, so the suggested name can be typed straight over. Mount-only:
   * a dependency here is the focus-theft bug lib/useDialog describes. */
  useEffect(() => {
    inputRef.current?.select();
  }, []);

  const trimmed = name.trim();
  const submit = () => {
    if (trimmed.length === 0 || busy) return;
    onSubmit(trimmed, includeMapArea, color);
  };

  /* Escape, the focus trap, initial + restored focus, the backdrop click and
   * the body scroll lock all come from <Dialog> (lib/useDialog.ts). The
   * hand-rolled window listener that used to live here is gone, and so is the
   * panel's `onClick={(e) => e.stopPropagation()}` — that existed only to stop
   * the backdrop's own `onClick={onClose}` from firing on a click INSIDE the
   * dialog, and <Dialog> makes the target test instead of asking the panel to
   * swallow every click that reaches it. */
  return (
    <Dialog open onClose={onClose} labelledBy={titleId} initialFocus={inputRef} className="w-full max-w-md p-5">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Filter preset
      </p>
      <h2
        id={titleId}
        className="mt-1 text-xl leading-tight"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {title}
      </h2>

      <label className="mt-4 block">
        <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
          Preset name
        </span>
        <input
          ref={inputRef}
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit();
          }}
          placeholder="e.g. 2+kk Praha pod 6M"
          className="mt-1 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)] focus-visible:border-[var(--color-copper)]"
        />
      </label>

      <div className="mt-4">
        <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
          Color
        </span>
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <TagColorPicker
            value={color}
            onChange={setColor}
            showNull
            size="md"
            ringOffsetVar="var(--color-paper)"
          />
        </div>
      </div>

      {showMapAreaToggle ? (
        <button
          type="button"
          onClick={() => setIncludeMapArea((v) => !v)}
          aria-pressed={includeMapArea}
          className="mt-3 flex w-full items-center gap-2 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-2 text-left transition-colors hover:border-[var(--color-rule-strong)]"
        >
          <span
            className={[
              'flex h-4 w-4 shrink-0 items-center justify-center rounded-[3px] border text-[0.6rem]',
              includeMapArea
                ? 'border-[var(--color-copper)] bg-[var(--color-copper)] text-white'
                : 'border-[var(--color-rule-strong)] text-transparent',
            ].join(' ')}
            aria-hidden
          >
            ✓
          </span>
          <span className="text-[0.8rem] text-[var(--color-ink-2)]">
            Include the current map area in this preset
          </span>
        </button>
      ) : null}

      {error ? (
        <p className="mt-3 text-[0.8rem] text-[var(--color-brick)]">{error}</p>
      ) : null}

      <div className="mt-5 flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)] transition-colors"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={trimmed.length === 0 || busy}
          className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors disabled:opacity-50"
        >
          {busy ? 'Saving…' : submitLabel}
        </button>
      </div>
    </Dialog>
  );
}
