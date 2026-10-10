/* Files on operator notes (migration 592): the drop zones, the file picker, the composer's
 * staged files and a saved note's attachments. The API stores and serves the bytes; image
 * thumbnails are blob URLs of what it returns. */

import { useEffect, useRef, useState } from 'react';
import type { DragEvent as ReactDragEvent } from 'react';
import { ApiError, deleteNoteAttachment, fetchNoteAttachmentBlob } from '@/lib/api';
import type { NoteAttachment } from '@/lib/types';
import {
  NOTE_ATTACHMENT_ACCEPT,
  fmtBytes,
  isPreviewableImage,
} from '@/lib/noteAttachments';
import { FileIcon, PaperclipIcon } from '@/components/icons';

const carriesFiles = (e: { dataTransfer: DataTransfer | null }): boolean =>
  Array.from(e.dataTransfer?.types ?? []).includes('Files');

/* One drop zone. Zones nest (a note inside the Notes section): each stops the drag events it
 * handles, so only the innermost zone under the cursor lights up and receives the drop. Text
 * drags pass through untouched. */
export function useFileDrop(onFiles: (files: File[]) => void) {
  const [over, setOver] = useState(false);
  const depth = useRef(0);
  const handlers = {
    onDragEnter: (e: ReactDragEvent) => {
      if (!carriesFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      depth.current += 1;
      setOver(true);
    },
    onDragOver: (e: ReactDragEvent) => {
      if (!carriesFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = 'copy';
    },
    onDragLeave: (e: ReactDragEvent) => {
      if (!carriesFiles(e)) return;
      e.stopPropagation();
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setOver(false);
    },
    onDrop: (e: ReactDragEvent) => {
      if (!carriesFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      depth.current = 0;
      setOver(false);
      const files = Array.from(e.dataTransfer.files);
      if (files.length > 0) onFiles(files);
    },
  };
  return { over, handlers };
}

/* A file dropped just outside a zone would make the browser open it in place of the app,
 * losing a half-written note. While mounted, a stray file drop does nothing. */
export function useStrayFileDropGuard() {
  useEffect(() => {
    const guard = (e: DragEvent) => {
      if (e.defaultPrevented || !carriesFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'none';
    };
    window.addEventListener('dragover', guard);
    window.addEventListener('drop', guard);
    return () => {
      window.removeEventListener('dragover', guard);
      window.removeEventListener('drop', guard);
    };
  }, []);
}

export function DropHint({ label }: { label: string }) {
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center gap-2 rounded-[var(--radius-sm)] border-2 border-dashed border-[var(--color-copper)] bg-[var(--color-paper)]/85 text-[0.78rem] font-medium text-[var(--color-copper)]"
    >
      <PaperclipIcon className="h-4 w-4" />
      {label}
    </div>
  );
}

export function AttachButton({
  onFiles,
  label,
  showText = false,
  disabled = false,
}: {
  onFiles: (files: File[]) => void;
  label: string;
  showText?: boolean;
  disabled?: boolean;
}) {
  const input = useRef<HTMLInputElement | null>(null);
  return (
    <>
      <button
        type="button"
        onClick={() => input.current?.click()}
        disabled={disabled}
        aria-label={label}
        title={label}
        className={
          showText
            ? 'inline-flex items-center gap-1 px-1.5 py-0.5 text-[0.72rem] rounded-[var(--radius-xs)] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] hover:bg-[var(--color-paper-2)] disabled:opacity-40 transition-colors'
            : 'inline-flex items-center justify-center w-5 h-5 rounded-[var(--radius-xs)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] disabled:opacity-40 transition-colors'
        }
      >
        <PaperclipIcon className={showText ? 'h-3.5 w-3.5' : 'h-[11px] w-[11px]'} />
        {showText && <span aria-hidden>Attach</span>}
      </button>
      <input
        ref={input}
        type="file"
        multiple
        accept={NOTE_ATTACHMENT_ACCEPT}
        tabIndex={-1}
        aria-label={label}
        data-testid="attach-input"
        className="hidden"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = '';
          if (files.length > 0) onFiles(files);
        }}
      />
    </>
  );
}

/* What went wrong with a drop or an upload, one line per file, until dismissed. */
export function FileProblems({
  problems,
  onDismiss,
}: {
  problems: string[];
  onDismiss: () => void;
}) {
  if (problems.length === 0) return null;
  return (
    <div role="alert" className="mt-1.5 flex items-start gap-2 text-[0.7rem] text-[var(--color-brick)]">
      <ul className="min-w-0 flex-1 space-y-0.5">
        {problems.map((p, i) => (
          <li key={i} className="break-words">{p}</li>
        ))}
      </ul>
      <button
        type="button"
        onClick={onDismiss}
        className="shrink-0 text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]"
      >
        Dismiss
      </button>
    </div>
  );
}

/* --- the composer's files, not uploaded yet ------------------------------- */

export function StagedFiles({
  files,
  onRemove,
  disabled,
}: {
  files: File[];
  onRemove: (file: File) => void;
  disabled: boolean;
}) {
  if (files.length === 0) return null;
  return (
    <ul aria-label="Files to attach" className="mt-2 flex flex-wrap gap-2">
      {files.map((file) => (
        <li key={`${file.name}|${file.size}|${file.lastModified}`}>
          <StagedFile file={file} onRemove={() => onRemove(file)} disabled={disabled} />
        </li>
      ))}
    </ul>
  );
}

function StagedFile({
  file,
  onRemove,
  disabled,
}: {
  file: File;
  onRemove: () => void;
  disabled: boolean;
}) {
  const preview = useLocalObjectUrl(isPreviewableImage(file.type) ? file : null);
  return (
    <FileTile
      name={file.name}
      size={file.size}
      thumbnail={preview}
      remove={{ label: `Remove ${file.name}`, onClick: onRemove, disabled, always: true }}
    />
  );
}

function useLocalObjectUrl(file: File | null): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!file) return;
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);
  return url;
}

/* --- a saved note's attachments ------------------------------------------- */

export function NoteAttachmentList({
  propertyId,
  noteId,
  attachments,
  uploading,
  onRemoved,
}: {
  propertyId: number;
  noteId: number;
  attachments: NoteAttachment[];
  uploading: File[];
  onRemoved: () => Promise<unknown>;
}) {
  const [confirming, setConfirming] = useState<NoteAttachment | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (attachments.length === 0 && uploading.length === 0) return null;

  const remove = (a: NoteAttachment) => {
    setBusy(true);
    setError(null);
    deleteNoteAttachment(propertyId, noteId, a.id)
      .then(onRemoved)
      .then(() => setConfirming(null))
      .catch((err: ApiError | Error) => setError(err.message || 'Failed to remove the file'))
      .finally(() => setBusy(false));
  };

  return (
    <div className="mt-2">
      <ul aria-label="Attachments" className="flex flex-wrap gap-2">
        {attachments.map((a) => (
          <li key={a.id}>
            <SavedAttachment
              propertyId={propertyId}
              noteId={noteId}
              attachment={a}
              onRemove={() => setConfirming(a)}
            />
          </li>
        ))}
        {uploading.map((file) => (
          <li key={`up|${file.name}|${file.size}|${file.lastModified}`}>
            <FileTile name={file.name} size={file.size} thumbnail={null} uploading />
          </li>
        ))}
      </ul>
      {confirming && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <span className="text-[0.7rem] text-[var(--color-brick)] break-all">
            Remove {confirming.filename}?
          </span>
          <button
            type="button"
            onClick={() => remove(confirming)}
            disabled={busy}
            className="px-2 py-0.5 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-brick-soft)] text-[var(--color-brick)] hover:bg-[var(--color-brick)]/15 disabled:opacity-50 transition-colors"
          >
            {busy ? 'Removing…' : 'Remove'}
          </button>
          <button
            type="button"
            onClick={() => setConfirming(null)}
            disabled={busy}
            className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      )}
      {error && <p className="mt-1.5 text-[0.7rem] text-[var(--color-brick)]">{error}</p>}
    </div>
  );
}

function SavedAttachment({
  propertyId,
  noteId,
  attachment,
  onRemove,
}: {
  propertyId: number;
  noteId: number;
  attachment: NoteAttachment;
  onRemove: () => void;
}) {
  const image = isPreviewableImage(attachment.mime_type);
  const thumb = useAttachmentUrl(propertyId, noteId, attachment.id, image);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const remove = { label: `Remove ${attachment.filename}`, onClick: onRemove };

  if (image && thumb.url) {
    return (
      <FileTile
        name={attachment.filename}
        size={attachment.byte_size}
        thumbnail={thumb.url}
        href={thumb.url}
        remove={remove}
      />
    );
  }
  const download = () => {
    setSaving(true);
    setFailed(false);
    fetchNoteAttachmentBlob(propertyId, noteId, attachment.id)
      .then((blob) => saveBlob(blob, attachment.filename))
      .catch(() => setFailed(true))
      .finally(() => setSaving(false));
  };
  return (
    <FileTile
      name={attachment.filename}
      size={attachment.byte_size}
      thumbnail={null}
      loading={image && !thumb.failed}
      onOpen={download}
      status={saving ? 'Downloading…' : failed ? 'Download failed' : undefined}
      remove={remove}
    />
  );
}

/* An image attachment's bytes as a blob URL, freed on unmount. */
function useAttachmentUrl(
  propertyId: number,
  noteId: number,
  attachmentId: number,
  enabled: boolean,
): { url: string | null; failed: boolean } {
  const [state, setState] = useState<{ url: string | null; failed: boolean }>({
    url: null,
    failed: false,
  });
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let url: string | null = null;
    fetchNoteAttachmentBlob(propertyId, noteId, attachmentId)
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setState({ url, failed: false });
      })
      .catch(() => {
        if (!cancelled) setState({ url: null, failed: true });
      });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [propertyId, noteId, attachmentId, enabled]);
  return state;
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Firefox starts the save asynchronously; revoking at once can cancel it.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/* --- one tile: a thumbnail square, or a file chip ------------------------- */

function FileTile({
  name,
  size,
  thumbnail,
  href,
  onOpen,
  loading = false,
  uploading = false,
  status,
  remove,
}: {
  name: string;
  size: number;
  thumbnail: string | null;
  href?: string;
  onOpen?: () => void;
  loading?: boolean;
  uploading?: boolean;
  status?: string;
  /* `always`: the × shows without hover (a staged file); a saved file's shows on hover, like
   * the note's own edit/delete. */
  remove?: { label: string; onClick: () => void; disabled?: boolean; always?: boolean };
}) {
  const title = `${name} · ${fmtBytes(size)}`;
  const removeButton = remove && (
    <button
      type="button"
      onClick={remove.onClick}
      disabled={remove.disabled}
      aria-label={remove.label}
      title={remove.label}
      className={`absolute -top-1.5 -right-1.5 inline-flex items-center justify-center w-4 h-4 rounded-full border border-[var(--color-rule)] bg-[var(--color-paper)] text-[0.65rem] leading-none text-[var(--color-ink-3)] hover:text-[var(--color-brick)] hover:border-[var(--color-brick)] disabled:opacity-40 transition-opacity ${remove.always ? '' : 'opacity-0 group-hover/tile:opacity-100 focus-visible:opacity-100'}`}
    >
      ×
    </button>
  );

  if (thumbnail) {
    const img = (
      <img src={thumbnail} alt={name} className="h-full w-full object-cover" draggable={false} />
    );
    return (
      <div className="group/tile relative h-14 w-14" title={title}>
        <div className="h-full w-full overflow-hidden rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-inset)]">
          {href ? (
            <a href={href} target="_blank" rel="noopener noreferrer" aria-label={`Open ${name}`}>
              {img}
            </a>
          ) : (
            img
          )}
        </div>
        {removeButton}
      </div>
    );
  }

  const body = (
    <>
      <FileIcon className="h-3.5 w-3.5 shrink-0 text-[var(--color-ink-3)]" />
      <span className="truncate max-w-[13rem] text-[var(--color-ink-2)]">{name}</span>
      <span className="shrink-0 font-mono tabular-nums text-[var(--color-ink-4)]">
        {uploading ? 'uploading…' : loading ? '…' : (status ?? fmtBytes(size))}
      </span>
    </>
  );
  const chip =
    'inline-flex items-center gap-1.5 h-8 pl-2 pr-2.5 text-[0.72rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]';
  return (
    <div className="group/tile relative" title={title}>
      {onOpen ? (
        <button
          type="button"
          onClick={onOpen}
          aria-label={`Download ${name}`}
          className={`${chip} hover:border-[var(--color-copper)] transition-colors`}
        >
          {body}
        </button>
      ) : (
        <span className={`${chip} ${uploading ? 'opacity-60 animate-pulse' : ''}`}>{body}</span>
      )}
      {removeButton}
    </div>
  );
}
