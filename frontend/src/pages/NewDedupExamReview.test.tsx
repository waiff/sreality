/* The exam review list. What matters here is write-path fidelity: every click
 * must re-answer the WHOLE image through the same endpoint the exam uses, in
 * the exam's own vocabulary — never a per-cell patch that could produce a row
 * shape the exam could not. */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import NewDedupExamReview from './NewDedupExamReview';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api');
vi.mock('@/lib/queries');
vi.mock('@/lib/imageUrl', () => ({ imageSrc: () => 'blob:photo' }));

const TAGS = [
  { id: 22, label: 'interier - koupelna' },
  { id: 25, label: 'interier - kuchyně' },
];

function renderPage(entries = ['/new-dedup/exam/review']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={entries}><NewDedupExamReview /></MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getExamAnswers).mockResolvedValue({
    data: {
      set: 'set_1',
      tags: TAGS,
      rows: [
        { image_id: 555, position: 13, picked_tag_ids: [22], skipped_tag_ids: [], cant_tell: false },
        { image_id: 777, position: 14, picked_tag_ids: [], skipped_tag_ids: [25], cant_tell: false },
      ],
    },
  });
  vi.mocked(api.answerExamQuestion).mockResolvedValue({
    data: { image_id: 555, cells_written: 2 },
  });
  vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
    new Map([
      [555, { id: 555, storage_path: 'img/1/555.jpg' } as never],
      [777, { id: 777, storage_path: 'img/1/777.jpg' } as never],
    ]),
  );
});

describe('<NewDedupExamReview>', () => {
  it('lists every answered image with its current verdicts', async () => {
    renderPage();
    expect(await screen.findByText(/2 answered/)).toBeInTheDocument();
    const rows = screen.getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    // Row one shows koupelna as picked, row two shows kuchyně left out.
    const picked = screen.getAllByRole('button', { name: /interier - koupelna/ })[0];
    expect(picked).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('left out')).toBeInTheDocument();
  });

  it('a click re-answers the WHOLE image through the exam endpoint', async () => {
    // Picked -> second state is the per-tag leave-out, and the post carries the
    // row's other verdicts unchanged — one write path, full-image semantics.
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/2 answered/);
    await user.click(screen.getAllByRole('button', { name: /interier - koupelna/ })[0]);
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [], skipped_tag_ids: [22], cant_tell: false },
    ));
    expect(screen.getAllByText('left out')).toHaveLength(2);
  });

  it('cycles leave-out back to untouched, which saves as a negative', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/2 answered/);
    await user.click(screen.getAllByRole('button', { name: /interier - kuchyně/ })[1]);
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 777, picked_tag_ids: [], skipped_tag_ids: [], cant_tell: false },
    ));
  });

  it('carries the sitting set on every correction', async () => {
    const user = userEvent.setup();
    renderPage(['/new-dedup/exam/review?cohort=exam_v1&set=set_2']);
    await screen.findByText(/2 answered/);
    await user.click(screen.getAllByRole('button', { name: /interier - kuchyně/ })[0]);
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      expect.objectContaining({ image_id: 555, set: 'set_2' }),
    ));
    expect(api.getExamAnswers).toHaveBeenCalledWith('exam_v1', 'set_2');
  });

  it('"can\'t tell" clears the row; a tag click on such a row starts over', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/2 answered/);
    await user.click(screen.getAllByRole('button', { name: /can’t tell/ })[0]);
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [], skipped_tag_ids: [], cant_tell: true },
    ));
    // "Actually I can tell": the click IS the new answer — a single pick,
    // everything else back to untouched.
    await user.click(screen.getAllByRole('button', { name: /interier - kuchyně/ })[0]);
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenLastCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [25], skipped_tag_ids: [], cant_tell: false },
    ));
  });

  it('a failed save reverts only that row', async () => {
    vi.mocked(api.answerExamQuestion).mockRejectedValue(new Error('boom'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/2 answered/);
    const btn = screen.getAllByRole('button', { name: /interier - koupelna/ })[0];
    await user.click(btn);
    // Optimistic leave-out, then back to the server's picked once the save fails.
    await waitFor(() => expect(btn).toHaveAttribute('aria-pressed', 'true'));
  });

  it('says so when nothing is answered yet', async () => {
    vi.mocked(api.getExamAnswers).mockResolvedValue({
      data: { set: 'set_1', tags: TAGS, rows: [] },
    });
    renderPage();
    expect(await screen.findByText(/Nothing answered in this sitting yet/)).toBeInTheDocument();
  });
});
