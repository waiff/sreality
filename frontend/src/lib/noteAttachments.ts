/* What a note may carry — the file picker's filter and the pre-check that saves a doomed upload.
 * The API decides (api/note_attachments.py); these lists mirror it, held in step by
 * tests/test_note_attachment_rules_parity.py. A named extension decides; only a file without
 * one falls back to the type the browser declared. Nothing active (HTML, SVG, script): the bytes
 * come back as same-origin blob URLs. */

export const NOTE_ATTACHMENT_EXTENSIONS = [
  '.jpg', '.jpeg', '.png', '.webp', '.gif', '.heic',
  '.pdf', '.txt', '.csv',
  '.doc', '.docx', '.xls', '.xlsx', '.odt', '.ods',
  '.zip',
] as const;

export const NOTE_ATTACHMENT_MIME_TYPES = [
  'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/heic',
  'application/pdf', 'text/plain', 'text/csv',
  'application/msword',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.ms-excel',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.oasis.opendocument.text',
  'application/vnd.oasis.opendocument.spreadsheet',
  'application/zip',
] as const;

export const NOTE_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
export const NOTE_ATTACHMENT_MAX_FILES = 20;

/* The <input type="file"> filter. */
export const NOTE_ATTACHMENT_ACCEPT = NOTE_ATTACHMENT_EXTENSIONS.join(',');

const extensionOf = (name: string): string => {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot).toLowerCase() : '';
};

/* Why this file cannot be attached, or null when it can. */
export function attachmentProblem(file: File): string | null {
  const ext = extensionOf(file.name);
  const allowed = ext
    ? (NOTE_ATTACHMENT_EXTENSIONS as readonly string[]).includes(ext)
    : (NOTE_ATTACHMENT_MIME_TYPES as readonly string[]).includes(file.type);
  if (!allowed) return `${file.name}: this file type cannot be attached`;
  if (file.size === 0) return `${file.name}: the file is empty`;
  if (file.size > NOTE_ATTACHMENT_MAX_BYTES) {
    return `${file.name}: over ${NOTE_ATTACHMENT_MAX_BYTES / (1024 * 1024)} MB`;
  }
  return null;
}

/* Split a drop into what can go and why the rest cannot, keeping the note under its cap. */
export function sortAttachments(
  files: readonly File[],
  room: number,
): { ok: File[]; problems: string[] } {
  const ok: File[] = [];
  const problems: string[] = [];
  for (const file of files) {
    const problem = attachmentProblem(file);
    if (problem) problems.push(problem);
    else if (ok.length >= room) {
      problems.push(`${file.name}: a note holds at most ${NOTE_ATTACHMENT_MAX_FILES} files`);
    } else ok.push(file);
  }
  return { ok, problems };
}

/* Shown inline as a thumbnail; everything else (HEIC included — most browsers cannot draw
 * it) is a file chip. */
export const isPreviewableImage = (mime: string): boolean =>
  mime === 'image/jpeg' || mime === 'image/png' || mime === 'image/webp' || mime === 'image/gif';

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}
