/* The exam screen. Every assertion here defends the exam's validity rather than
 * its looks: an anchored operator, a mis-keyed answer, or practice written into
 * the measurement all produce a number that looks fine and means nothing. */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import NewDedupExam from './NewDedupExam';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api');
vi.mock('@/lib/queries');
vi.mock('@/lib/imageUrl', () => ({ imageSrc: () => 'blob:photo' }));

const TAGS = [
  { id: 22, label: 'interier - koupelna' },
  { id: 25, label: 'interier - kuchyně' },
  { id: 46, label: 'podklad - půdorys' },
  { id: 17, label: 'garáž' },
];

const examState = (over: Partial<api.ExamState> = {}): api.ExamState => ({
  cohort: { name: 'exam_v1', sealed: true },
  tags: TAGS,
  progress: { total: 250, answered: 12, remaining: 238 },
  question: { image_id: 555, position: 13, storage_path: 'img/1/555.jpg' },
  ...over,
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><NewDedupExam /></MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getExamState).mockResolvedValue({ data: examState() });
  // No warm-up by default; the warm-up path has its own tests below.
  vi.mocked(api.getExamWarmup).mockResolvedValue({ data: [] });
  vi.mocked(api.getTagDefinitionCard).mockResolvedValue({ data: null });
  vi.mocked(api.answerExamQuestion).mockResolvedValue({
    data: { image_id: 555, cells_written: 4 },
  });
  vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
    new Map([[555, { id: 555, storage_path: 'img/1/555.jpg' } as never]]),
  );
});

describe('<NewDedupExam>', () => {
  it('asks one question about one image', async () => {
    renderPage();
    expect(await screen.findByText(/Which of these, if any/)).toBeInTheDocument();
    expect(await screen.findByAltText('Exam photo')).toBeInTheDocument();
  });

  it('offers a button per routing tag, server-ordered', async () => {
    renderPage();
    await screen.findByText(/Which of these/);
    for (const t of TAGS) {
      expect(screen.getByRole('button', { name: new RegExp(t.label) })).toBeInTheDocument();
    }
  });

  it('never shows a machine suggestion', async () => {
    // An exam the machine helped answer cannot grade the machine, so the
    // screener's guesses are not even requested.
    renderPage();
    await screen.findByText(/Which of these/);
    for (const t of TAGS) {
      expect(screen.getByRole('button', { name: new RegExp(t.label) }))
        .toHaveAttribute('aria-pressed', 'false');
    }
  });

  it('keeps the key legend permanently on screen', async () => {
    // The labeling grid next door binds these same digits to STATES. The legend
    // is the only thing between that habit and a mis-keyed exam row, so it must
    // not be a tooltip.
    renderPage();
    expect(await screen.findByText(/space = none of these/)).toBeInTheDocument();
  });

  it('sends every untouched tag as a negative, not just the picks', async () => {
    // One answer has to measure precision AND recall, which it can only do if
    // the tags you did not touch are recorded as negatives.
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.click(screen.getByRole('button', { name: /interier - koupelna/ }));
    await user.click(screen.getByRole('button', { name: /^Confirm/ }));
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [22], skipped_tag_ids: [], cant_tell: false },
    ));
  });

  it('a second press turns a pick into a per-tag leave-out', async () => {
    // The brief's rule, on screen: subject clearly present but the photo is of
    // something else -> leave it out of THAT head, everything else still answers.
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    const btn = screen.getByRole('button', { name: /interier - kuchyně/ });
    await user.click(btn);
    await user.click(btn);
    expect(screen.getByText('left out')).toBeInTheDocument();
    expect(btn).toHaveAttribute('aria-pressed', 'false');
    await user.click(screen.getByRole('button', { name: /^Confirm/ }));
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [], skipped_tag_ids: [25], cant_tell: false },
    ));
  });

  it('a third press clears the tag back to untouched', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    const btn = screen.getByRole('button', { name: /interier - kuchyně/ });
    await user.click(btn);
    await user.click(btn);
    await user.click(btn);
    expect(screen.queryByText('left out')).toBeNull();
    expect(screen.getByRole('button', { name: /^Confirm/ })).toBeDisabled();
  });

  it('composes a pick and a leave-out in one answer', async () => {
    // The motivating warm-up image exactly: koupelna picked, kuchyně left out,
    // six untouched tags become negatives server-side.
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.click(screen.getByRole('button', { name: /interier - koupelna/ }));
    const kitchen = screen.getByRole('button', { name: /interier - kuchyně/ });
    await user.click(kitchen);
    await user.click(kitchen);
    await user.click(screen.getByRole('button', { name: /^Confirm/ }));
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [22], skipped_tag_ids: [25], cant_tell: false },
    ));
  });

  it('treats "none of these" as a real answer', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.click(screen.getByRole('button', { name: /None of these/ }));
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [], skipped_tag_ids: [], cant_tell: false },
    ));
  });

  it('sends "can\'t tell" as its own verdict rather than a negative', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.click(screen.getByRole('button', { name: /Can’t tell/ }));
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [], skipped_tag_ids: [], cant_tell: true },
    ));
  });

  it('picks a tag with its number key', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.keyboard('2');
    expect(screen.getByRole('button', { name: /interier - kuchyně/ }))
      .toHaveAttribute('aria-pressed', 'true');
  });

  it('answers "none" on space, in one keystroke', async () => {
    // Most random photos are none of the eight, so the commonest answer must be
    // the cheapest one.
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.keyboard(' ');
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [], skipped_tag_ids: [], cant_tell: false },
    ));
  });

  it('cycles pick -> leave-out with the same number key', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.keyboard('2');
    expect(screen.getByRole('button', { name: /interier - kuchyně/ }))
      .toHaveAttribute('aria-pressed', 'true');
    await user.keyboard('2');
    expect(screen.getByText('left out')).toBeInTheDocument();
  });

  it('keeps the leave-out key in the permanent legend', async () => {
    renderPage();
    expect(await screen.findByText(/again = leave it out of that tag/)).toBeInTheDocument();
  });

  it('ignores a digit typed into a text field', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    await user.keyboard('3');
    expect(screen.getByRole('button', { name: /podklad - půdorys/ }))
      .toHaveAttribute('aria-pressed', 'false');
    input.remove();
  });

  it('shows progress over the whole exam', async () => {
    renderPage();
    expect(await screen.findByText(/12 of 250 answered/)).toBeInTheDocument();
  });

  it('says so when the exam is finished rather than showing an empty frame', async () => {
    vi.mocked(api.getExamState).mockResolvedValue({
      data: examState({ question: null, progress: { total: 250, answered: 250, remaining: 0 } }),
    });
    renderPage();
    expect(await screen.findByText(/exam is complete/)).toBeInTheDocument();
  });

  it('warns when the cohort is not sealed', async () => {
    // Answering an unsealed cohort means answering an exam that can still grow.
    vi.mocked(api.getExamState).mockResolvedValue({
      data: examState({ cohort: { name: 'exam_v1', sealed: false } }),
    });
    renderPage();
    expect(await screen.findByText(/cohort not sealed/)).toBeInTheDocument();
  });

  it('never posts an answer during the warm-up', async () => {
    // Practice must not reach the measurement. The server would refuse it, but
    // not sending it keeps that refusal a rail rather than routine traffic.
    vi.mocked(api.getExamWarmup).mockResolvedValue({
      data: [{ image_id: 900, storage_path: 'img/9/900.jpg' }],
    });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[900, { id: 900, storage_path: 'img/9/900.jpg' } as never]]),
    );
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText(/Warm-up 1 of 1/)).toBeInTheDocument();
    await user.keyboard(' ');
    expect(api.answerExamQuestion).not.toHaveBeenCalled();
  });

  it('says the warm-up does not count', async () => {
    vi.mocked(api.getExamWarmup).mockResolvedValue({
      data: [{ image_id: 900, storage_path: 'img/9/900.jpg' }],
    });
    renderPage();
    expect(await screen.findByText(/do not count/)).toBeInTheDocument();
  });
});
