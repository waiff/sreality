/* Compact popup for kicking off a new estimation OR a new building decomposition.
 *
 * Apartment kind (default):
 *   Paste a listing URL → POST /estimations/preview to extract spec →
 *   POST /estimations to start the run → navigate to /estimation/:id.
 *
 * Building kind (Phase B1):
 *   Paste a `dum` / `komercni` URL → POST /buildings/from_url runs the
 *   extractor synchronously → navigate to /building/:id where the
 *   operator reviews the unit proposal and confirms.
 *
 * Estimate kind (rent vs sale) for the apartment path is operator-
 * chosen via a segmented control, defaulted to rent. The toggle always
 * wins — pasting a `prodej` URL while the toggle is on Rent still
 * submits a rent estimate. Provider and population fall back to the
 * same defaults the form used to ship with (Claude + active-only).
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import {
  ApiError,
  createBuildingFromUrl,
  previewListingUrl,
  reExtractBuilding,
  uploadBuildingAttachment,
} from '@/lib/api';
import { submitEstimation } from '@/lib/queries';
import type {
  BuildingRun,
  CreateEstimationIn,
  EstimationRun,
  ParseResult,
} from '@/lib/types';

const ATTACHMENT_MIME = ['image/png', 'image/jpeg', 'image/webp'];
const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
const ATTACHMENT_MAX_FILES = 20;

/* -------------------------------------------------------------------------- */
/* Context — wires the CTA button (Shell) and the "+ New estimation"          */
/* affordances on EstimationList / empty states to the same modal instance.   */
/* -------------------------------------------------------------------------- */

interface ModalCtx {
  open: () => void;
  close: () => void;
  isOpen: boolean;
}

const ctx = createContext<ModalCtx | null>(null);

export function useNewEstimationModal(): ModalCtx {
  const v = useContext(ctx);
  if (!v) {
    throw new Error('useNewEstimationModal must be used inside <NewEstimationProvider>');
  }
  return v;
}

export function NewEstimationProvider({ children }: { children: ReactNode }) {
  const [isOpen, setOpen] = useState(false);
  const value = useMemo<ModalCtx>(
    () => ({
      open: () => setOpen(true),
      close: () => setOpen(false),
      isOpen,
    }),
    [isOpen],
  );
  return (
    <ctx.Provider value={value}>
      {children}
      {isOpen && <NewEstimationModal onClose={() => setOpen(false)} />}
    </ctx.Provider>
  );
}

/* -------------------------------------------------------------------------- */
/* Modal                                                                      */
/* -------------------------------------------------------------------------- */

type Kind = 'apartment' | 'building';
type EstimateKind = 'rent' | 'sale';

function NewEstimationModal({ onClose }: { onClose: () => void }) {
  const [kind, setKind] = useState<Kind>('apartment');
  const [estimateKind, setEstimateKind] = useState<EstimateKind>('rent');
  const [url, setUrl] = useState('');
  const [specialInstructions, setSpecialInstructions] = useState('');
  const [contextualText, setContextualText] = useState('');
  const [attachmentFiles, setAttachmentFiles] = useState<File[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [uploadStage, setUploadStage] = useState<
    { done: number; total: number } | null
  >(null);
  const navigate = useNavigate();

  const previewMut = useMutation<ParseResult, ApiError, string>({
    mutationFn: (url) => previewListingUrl(url),
  });
  const submitMut = useMutation<EstimationRun, ApiError, ParseResult>({
    mutationFn: (preview) =>
      submitEstimation(
        buildEstimationPayload(
          preview, estimateKind, specialInstructions, contextualText,
        ),
      ),
    onSuccess: (run) => {
      onClose();
      navigate(`/estimation/${run.id}`);
    },
  });
  const buildingMut = useMutation<BuildingRun, ApiError, string>({
    mutationFn: async (url) => {
      const run = await createBuildingFromUrl({
        source: 'ui',
        url,
        special_instructions: specialInstructions.trim() || null,
        contextual_text: contextualText.trim() || null,
      });
      if (attachmentFiles.length > 0) {
        setUploadStage({ done: 0, total: attachmentFiles.length });
        for (let i = 0; i < attachmentFiles.length; i++) {
          await uploadBuildingAttachment(run.id, attachmentFiles[i]);
          setUploadStage({ done: i + 1, total: attachmentFiles.length });
        }
        // Re-extract so the operator-uploaded images become part of the
        // unit proposal. The extractor cache misses on any run with
        // attachments, so this is real work, not a no-op.
        await reExtractBuilding(run.id);
      }
      return run;
    },
    onSuccess: (run) => {
      onClose();
      navigate(`/building/${run.id}`);
    },
  });

  const pending =
    previewMut.isPending || submitMut.isPending || buildingMut.isPending;
  const error =
    previewMut.error || submitMut.error || buildingMut.error;

  const submit = useCallback(() => {
    const trimmed = url.trim();
    if (!trimmed || pending) return;
    if (kind === 'building') {
      buildingMut.mutate(trimmed);
      return;
    }
    previewMut.mutate(trimmed, {
      onSuccess: (preview) => submitMut.mutate(preview),
    });
  }, [url, kind, pending, previewMut, submitMut, buildingMut]);

  const onAttachmentsPicked = useCallback(
    (picked: FileList | null) => {
      if (!picked || picked.length === 0) return;
      setAttachmentError(null);
      const next = [...attachmentFiles];
      for (const f of Array.from(picked)) {
        if (!ATTACHMENT_MIME.includes(f.type)) {
          setAttachmentError(
            `"${f.name}" is ${f.type || 'an unknown type'}. ` +
            'Allowed: PNG, JPEG, WebP.',
          );
          continue;
        }
        if (f.size > ATTACHMENT_MAX_BYTES) {
          setAttachmentError(
            `"${f.name}" is ${(f.size / 1024 / 1024).toFixed(1)} MB. ` +
            'Max is 25 MB per file.',
          );
          continue;
        }
        if (next.length >= ATTACHMENT_MAX_FILES) {
          setAttachmentError(
            `Cap is ${ATTACHMENT_MAX_FILES} files per building.`,
          );
          break;
        }
        if (next.some((x) => x.name === f.name && x.size === f.size)) {
          continue;
        }
        next.push(f);
      }
      setAttachmentFiles(next);
    },
    [attachmentFiles],
  );

  const removeAttachment = useCallback((idx: number) => {
    setAttachmentFiles((files) => files.filter((_, i) => i !== idx));
  }, []);

  // Esc closes; Enter submits when the input is focused.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (pending) return;
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, pending]);

  const title =
    kind === 'building' ? 'Which building?' : 'Where is the listing?';
  const placeholder =
    kind === 'building'
      ? 'https://www.sreality.cz/detail/prodej/dum/…'
      : 'https://www.sreality.cz/detail/…';
  const helpCopy =
    kind === 'building'
      ? 'We read description + floor plans + photos, propose the apartment units, and open the building for your review.'
      : 'We scrape the listing, kick off the estimate, and open the run. You can adjust the spec and re-run from the detail page.';
  const submitLabel = pending
    ? previewMut.isPending
      ? 'Scraping…'
      : buildingMut.isPending
        ? uploadStage
          ? `Uploading ${uploadStage.done}/${uploadStage.total}…`
          : 'Extracting units…'
        : 'Submitting…'
    : kind === 'building'
      ? 'Decompose'
      : 'Estimate';

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center px-4 pt-[16vh] pb-10 bg-[var(--color-ink)]/40 backdrop-blur-[2px]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="new-estimation-title"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !pending) onClose();
      }}
    >
      <div className="w-full max-w-xl rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] shadow-2xl">
        <header className="flex items-baseline justify-between gap-4 px-6 pt-5 pb-3">
          <div>
            <p className="text-[0.65rem] tracking-[0.22em] uppercase text-[var(--color-ink-3)]">
              New {kind === 'building' ? 'building' : 'estimation'}
            </p>
            <h2
              id="new-estimation-title"
              className="mt-1 text-[1.35rem] leading-tight"
              style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
            >
              {title}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={pending}
            aria-label="Close"
            className="shrink-0 -mr-1 px-2 py-1 text-[var(--color-ink-3)] hover:text-[var(--color-ink)] disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <CloseGlyph />
          </button>
        </header>

        <div className="px-6 pb-6">
          <KindToggle kind={kind} setKind={setKind} disabled={pending} />

          {kind === 'apartment' && (
            <EstimateKindToggle
              estimateKind={estimateKind}
              setEstimateKind={setEstimateKind}
              disabled={pending}
            />
          )}

          <label
            htmlFor="new-estimation-url"
            className="mt-4 block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]"
          >
            Listing URL
          </label>
          <div className="mt-2 flex items-stretch gap-2">
            <input
              id="new-estimation-url"
              type="url"
              inputMode="url"
              autoFocus
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder={placeholder}
              disabled={pending}
              className="flex-1 min-w-0 px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
            />
            <button
              type="button"
              onClick={submit}
              disabled={!url.trim() || pending}
              className={[
                'shrink-0 px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
                !url.trim() || pending
                  ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
                  : 'bg-[var(--color-copper)] text-white border-[var(--color-copper)] hover:bg-[var(--color-copper-2)] hover:border-[var(--color-copper-2)]',
              ].join(' ')}
            >
              {pending ? <Spinner label={submitLabel} /> : submitLabel}
            </button>
          </div>

          {error && <ErrorBlock error={error} kind={kind} />}

          <OperatorInputs
            specialInstructions={specialInstructions}
            setSpecialInstructions={setSpecialInstructions}
            contextualText={contextualText}
            setContextualText={setContextualText}
            disabled={pending}
            allowAttachments={kind === 'building'}
            attachments={attachmentFiles}
            attachmentError={attachmentError}
            onAttachmentsPicked={onAttachmentsPicked}
            onRemoveAttachment={removeAttachment}
          />

          <p className="mt-4 text-[0.75rem] text-[var(--color-ink-3)] leading-relaxed">
            {helpCopy}
          </p>
        </div>
      </div>
    </div>
  );
}

function OperatorInputs({
  specialInstructions,
  setSpecialInstructions,
  contextualText,
  setContextualText,
  disabled,
  allowAttachments,
  attachments,
  attachmentError,
  onAttachmentsPicked,
  onRemoveAttachment,
}: {
  specialInstructions: string;
  setSpecialInstructions: (v: string) => void;
  contextualText: string;
  setContextualText: (v: string) => void;
  disabled: boolean;
  allowAttachments: boolean;
  attachments: File[];
  attachmentError: string | null;
  onAttachmentsPicked: (files: FileList | null) => void;
  onRemoveAttachment: (idx: number) => void;
}) {
  return (
    <details
      className="mt-4 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)]"
    >
      <summary
        className="cursor-pointer select-none px-3 py-2 text-[0.72rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]"
      >
        Operator context
        {(specialInstructions || contextualText || attachments.length > 0) && (
          <span className="ml-2 text-[var(--color-copper)] normal-case tracking-normal text-[0.7rem]">
            {[
              specialInstructions && 'instructions',
              contextualText && 'context',
              attachments.length > 0 && `${attachments.length} file${attachments.length === 1 ? '' : 's'}`,
            ].filter(Boolean).join(' · ')}
          </span>
        )}
      </summary>
      <div className="px-3 pb-3 pt-1 space-y-3">
        <div>
          <label
            htmlFor="new-est-instructions"
            className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]"
          >
            Special instructions
          </label>
          <textarea
            id="new-est-instructions"
            value={specialInstructions}
            onChange={(e) => setSpecialInstructions(e.target.value)}
            disabled={disabled}
            rows={2}
            maxLength={10_000}
            placeholder="e.g. Use a 1500m radius. Weight comparables in the same block heavier."
            className="mt-1 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
          />
          <p className="mt-1 text-[0.7rem] text-[var(--color-ink-3)]">
            Wrapped in &lt;operator_instructions&gt; in the agent's prompt.
          </p>
        </div>
        <div>
          <label
            htmlFor="new-est-context"
            className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]"
          >
            Property context
          </label>
          <textarea
            id="new-est-context"
            value={contextualText}
            onChange={(e) => setContextualText(e.target.value)}
            disabled={disabled}
            rows={3}
            maxLength={20_000}
            placeholder="Anything the listing doesn't say: legal status, recent renovations, neighbours, planning, etc."
            className="mt-1 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
          />
        </div>
        {allowAttachments && (
          <div>
            <label className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
              Attachments — floor plans, photos, drawings
            </label>
            <input
              type="file"
              multiple
              accept={ATTACHMENT_MIME.join(',')}
              disabled={disabled}
              onChange={(e) => {
                onAttachmentsPicked(e.target.files);
                e.target.value = '';
              }}
              className="mt-1 block w-full text-[0.78rem] text-[var(--color-ink-2)] file:mr-2 file:rounded-[var(--radius-sm)] file:border file:border-[var(--color-rule)] file:bg-[var(--color-inset)] file:text-[var(--color-ink)] file:px-3 file:py-1.5 file:text-[0.78rem] file:cursor-pointer hover:file:bg-[var(--color-paper)] disabled:opacity-60"
            />
            {attachmentError && (
              <p className="mt-1 text-[0.72rem] text-[var(--color-brick)]">
                {attachmentError}
              </p>
            )}
            {attachments.length > 0 && (
              <ul className="mt-2 space-y-1 text-[0.78rem]">
                {attachments.map((f, i) => (
                  <li
                    key={`${f.name}-${f.size}-${i}`}
                    className="flex items-center justify-between gap-2 px-2 py-1 rounded-[var(--radius-sm)] bg-[var(--color-inset)]"
                  >
                    <span className="truncate" title={f.name}>
                      {f.name}
                      <span className="ml-2 text-[var(--color-ink-3)]">
                        {(f.size / 1024).toFixed(0)} KB
                      </span>
                    </span>
                    <button
                      type="button"
                      onClick={() => onRemoveAttachment(i)}
                      disabled={disabled}
                      className="text-[var(--color-ink-3)] hover:text-[var(--color-brick)] disabled:opacity-40"
                      aria-label={`Remove ${f.name}`}
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-1 text-[0.7rem] text-[var(--color-ink-3)]">
              Up to {ATTACHMENT_MAX_FILES} files, 25 MB each. Uploaded after
              the building row is created; the extractor re-runs so it can
              read them.
            </p>
          </div>
        )}
      </div>
    </details>
  );
}

/* -------------------------------------------------------------------------- */
/* Kind toggle                                                                */
/* -------------------------------------------------------------------------- */

function KindToggle({
  kind, setKind, disabled,
}: {
  kind: Kind;
  setKind: (k: Kind) => void;
  disabled: boolean;
}) {
  return (
    <div
      role="radiogroup"
      aria-label="What are you pasting?"
      className="flex items-stretch gap-0 rounded-[var(--radius-sm)] border border-[var(--color-rule)] overflow-hidden bg-[var(--color-inset)]"
    >
      <KindButton
        active={kind === 'apartment'}
        onClick={() => setKind('apartment')}
        disabled={disabled}
        label="Apartment"
        sub="One flat → rent / sale estimate"
      />
      <KindButton
        active={kind === 'building'}
        onClick={() => setKind('building')}
        disabled={disabled}
        label="Building"
        sub="Decompose → estimate per unit"
      />
    </div>
  );
}

function EstimateKindToggle({
  estimateKind, setEstimateKind, disabled,
}: {
  estimateKind: EstimateKind;
  setEstimateKind: (k: EstimateKind) => void;
  disabled: boolean;
}) {
  return (
    <div className="mt-3">
      <span className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Estimate
      </span>
      <div
        role="radiogroup"
        aria-label="Estimate rent or sale price"
        className="mt-2 inline-flex items-stretch gap-0 rounded-[var(--radius-sm)] border border-[var(--color-rule)] overflow-hidden bg-[var(--color-inset)]"
      >
        <EstimateKindButton
          active={estimateKind === 'rent'}
          onClick={() => setEstimateKind('rent')}
          disabled={disabled}
          label="Rent"
        />
        <EstimateKindButton
          active={estimateKind === 'sale'}
          onClick={() => setEstimateKind('sale')}
          disabled={disabled}
          label="Sale"
        />
      </div>
    </div>
  );
}

function EstimateKindButton({
  active, onClick, disabled, label,
}: {
  active: boolean;
  onClick: () => void;
  disabled: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onClick}
      disabled={disabled}
      className={[
        'px-4 py-1.5 text-[0.8rem] transition-colors disabled:opacity-50 disabled:cursor-not-allowed',
        active
          ? 'bg-[var(--color-paper)] text-[var(--color-ink)]'
          : 'bg-transparent text-[var(--color-ink-3)] hover:text-[var(--color-ink)]',
      ].join(' ')}
      style={{ fontWeight: active ? 600 : 500 }}
    >
      {label}
    </button>
  );
}

function KindButton({
  active, onClick, disabled, label, sub,
}: {
  active: boolean;
  onClick: () => void;
  disabled: boolean;
  label: string;
  sub: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onClick}
      disabled={disabled}
      className={[
        'flex-1 px-3 py-2 text-left transition-colors disabled:opacity-50 disabled:cursor-not-allowed',
        active
          ? 'bg-[var(--color-paper)] text-[var(--color-ink)]'
          : 'bg-transparent text-[var(--color-ink-3)] hover:text-[var(--color-ink)]',
      ].join(' ')}
    >
      <span className="block text-[0.8rem]" style={{ fontWeight: active ? 600 : 500 }}>
        {label}
      </span>
      <span className="block text-[0.7rem] mt-0.5 text-[var(--color-ink-3)]">
        {sub}
      </span>
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                    */
/* -------------------------------------------------------------------------- */

function buildEstimationPayload(
  preview: ParseResult,
  estimateKind: EstimateKind,
  specialInstructions: string,
  contextualText: string,
): CreateEstimationIn {
  const mode = estimateKind === 'rent' ? 'agent' : 'deterministic';
  return {
    source: 'ui',
    mode,
    provider: 'anthropic',
    population: 'active',
    estimate_kind: estimateKind,
    url: preview.source_url,
    special_instructions: specialInstructions.trim() || null,
    contextual_text: contextualText.trim() || null,
  };
}

function Spinner({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-2">
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
        <circle
          cx="6" cy="6" r="4.5"
          stroke="currentColor" strokeWidth="1.5"
          strokeOpacity="0.25" fill="none"
        />
        <path
          d="M6 1.5 a 4.5 4.5 0 0 1 4.5 4.5"
          stroke="currentColor" strokeWidth="1.5" fill="none"
          strokeLinecap="round"
        >
          <animateTransform
            attributeName="transform" type="rotate"
            from="0 6 6" to="360 6 6" dur="0.9s"
            repeatCount="indefinite"
          />
        </path>
      </svg>
      <span>{label}</span>
    </span>
  );
}

function CloseGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
      <path
        d="M3 3 L11 11 M11 3 L3 11"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function ErrorBlock({ error, kind }: { error: ApiError; kind: Kind }) {
  const headline = (() => {
    if (error.status === 400) {
      if (kind === 'building') {
        return error.message || "That URL isn't a building listing.";
      }
      return "That URL doesn't look like a supported listing page.";
    }
    if (error.status === 401) {
      return 'API authentication failed. Check VITE_API_TOKEN.';
    }
    if (error.status === 502) {
      return "Couldn't reach the listing source right now.";
    }
    if (error.status === 0) {
      return 'Network error — check your connection or the API URL.';
    }
    return `Request failed (HTTP ${error.status}).`;
  })();
  return (
    <div className="mt-3 px-3 py-2 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-[var(--color-brick)] text-sm">
      <p className="font-medium">{headline}</p>
      {error.message && error.message !== headline && (
        <p className="mt-1 text-[0.78rem] text-[var(--color-brick)]/85">
          {error.message}
        </p>
      )}
    </div>
  );
}
