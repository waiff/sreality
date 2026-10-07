/* The pre-check mirrors the API's rule: a named extension decides; a file without one goes by
 * its declared type; the note's cap counts what it already holds. */
import { describe, expect, it } from 'vitest';
import { attachmentProblem, fmtBytes, sortAttachments } from './noteAttachments';

const file = (name: string, type = '', size = 10) =>
  new File([new Uint8Array(size)], name, { type });

describe('noteAttachments', () => {
  it('lets a named extension decide, whatever type the browser declared', () => {
    expect(attachmentProblem(file('plan.PDF'))).toBeNull();
    expect(attachmentProblem(file('page.html', 'text/plain'))).toMatch(/cannot be attached/);
    expect(attachmentProblem(file('scan', 'image/jpeg'))).toBeNull();
    expect(attachmentProblem(file('scan', 'text/html'))).toMatch(/cannot be attached/);
  });

  it('refuses an empty file', () => {
    expect(attachmentProblem(file('a.txt', 'text/plain', 0))).toMatch(/empty/);
  });

  it('keeps a note under its cap and says which files did not fit', () => {
    const { ok, problems } = sortAttachments([file('a.pdf'), file('b.pdf'), file('c.svg')], 1);
    expect(ok.map((f) => f.name)).toEqual(['a.pdf']);
    expect(problems).toEqual([
      'b.pdf: a note holds at most 20 files',
      'c.svg: this file type cannot be attached',
    ]);
  });

  it('formats sizes', () => {
    expect([fmtBytes(512), fmtBytes(2048), fmtBytes(3.5 * 1024 * 1024), fmtBytes(12 * 1024 * 1024)])
      .toEqual(['512 B', '2 KB', '3.5 MB', '12 MB']);
  });
});
