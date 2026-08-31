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
  set: 'routing',
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
  vi.mocked(api.getExamCohorts).mockResolvedValue({ data: [] });
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

  it('a suggestion is a mark, never a pre-filled verdict', async () => {
    // The operator's 2026-08-30 ruling reversed the no-suggestion posture, but
    // the line that keeps the exam auditable holds: the machine marks a button,
    // it never presses one. The verdict state — and therefore what gets
    // written — starts untouched regardless of what the machine thinks.
    vi.mocked(api.getExamState).mockResolvedValue({
      data: examState({
        question: {
          image_id: 555, position: 13, storage_path: 'img/1/555.jpg',
          suggested_tag_ids: [22],
        },
      }),
    });
    renderPage();
    await screen.findByText(/Which of these/);
    expect(screen.getByTestId('suggested-22')).toBeInTheDocument();
    for (const t of TAGS) {
      expect(screen.getByRole('button', { name: new RegExp(t.label) }))
        .toHaveAttribute('aria-pressed', 'false');
    }
    expect(screen.getByRole('button', { name: /^Confirm/ })).toBeDisabled();
  });

  it('an empty suggestion marks "none of these" — silence would be ambiguous', async () => {
    // [] = the machine genuinely says none; null = nothing worth showing. The
    // two must render differently or "no dot" could mean either.
    vi.mocked(api.getExamState).mockResolvedValue({
      data: examState({
        question: {
          image_id: 555, position: 13, storage_path: 'img/1/555.jpg',
          suggested_tag_ids: [],
        },
      }),
    });
    renderPage();
    await screen.findByText(/Which of these/);
    expect(screen.getByTestId('suggested-none')).toBeInTheDocument();
  });

  it('shows no mark at all when no suggestion was computed', async () => {
    renderPage();
    await screen.findByText(/Which of these/);
    expect(screen.queryByTestId('suggested-22')).toBeNull();
    expect(screen.queryByTestId('suggested-none')).toBeNull();
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

  it('picks a tag with its letter key', async () => {
    // Letters, not digits (operator's ruling): on Czech QWERTZ the digit row is
    // shifted, and letters cannot collide with the labeling grid's digit habit.
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.keyboard('e');
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

  it('cycles pick -> leave-out with the same letter key', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.keyboard('e');
    expect(screen.getByRole('button', { name: /interier - kuchyně/ }))
      .toHaveAttribute('aria-pressed', 'true');
    await user.keyboard('e');
    expect(screen.getByText('left out')).toBeInTheDocument();
  });

  it('keeps the leave-out key in the permanent legend', async () => {
    renderPage();
    expect(await screen.findByText(/again = leave it out of that tag/)).toBeInTheDocument();
  });

  it('ignores a key typed into a text field', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    await user.keyboard('i');
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
    vi.mocked(api.getExamState).mockResolvedValue({
      data: examState({ progress: { total: 250, answered: 0, remaining: 250 } }),
    });
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
    vi.mocked(api.getExamState).mockResolvedValue({
      data: examState({ progress: { total: 250, answered: 0, remaining: 250 } }),
    });
    vi.mocked(api.getExamWarmup).mockResolvedValue({
      data: [{ image_id: 900, storage_path: 'img/9/900.jpg' }],
    });
    renderPage();
    expect(await screen.findByText(/do not count/)).toBeInTheDocument();
  });

  it('skips the warm-up entirely once the sitting has answers', async () => {
    // The warm-up settles a COLD hand. Replaying ten practice images on every
    // reload of a sitting 44 answers deep read as "my progress is gone" — the
    // real, resumed question was waiting right behind it.
    vi.mocked(api.getExamWarmup).mockResolvedValue({
      data: [{ image_id: 900, storage_path: 'img/9/900.jpg' }],
    });
    renderPage(); // default examState: 12 of 250 answered
    expect(await screen.findByText(/Which of these/)).toBeInTheDocument();
    expect(screen.queryByText(/Warm-up/)).toBeNull();
    expect(screen.getByText(/12 of 250 answered/)).toBeInTheDocument();
    expect(api.getExamWarmup).not.toHaveBeenCalled();
  });
});

describe('<NewDedupExam> iterations (sets)', () => {
  it('sits the set named in the URL and stamps answers with it', async () => {
    // Iteration 2 runs on a NEW question list over the SAME 250 images — the
    // server resolves ?set= for both question and answer, so the two can never
    // disagree about which columns a sitting is writing.
    vi.mocked(api.getExamState).mockResolvedValue({
      data: examState({
        set: 'set_2',
        tags: [
          { id: 28, label: 'interier - obývací pokoj' },
          { id: 20, label: 'interier - jídelna' },
          { id: 27, label: 'interier - nezařízená místnost' },
        ],
      }),
    });
    const user = userEvent.setup();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/new-dedup/exam?cohort=exam_v1&set=set_2']}>
          <NewDedupExam />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText(/Which of these/);
    expect(api.getExamState).toHaveBeenCalledWith('exam_v1', 'set_2');
    expect(screen.getByText('set_2')).toBeInTheDocument();
    // Three buttons, the set's own order — not tag-id order.
    const kbds = screen.getAllByRole('button').filter((b) => /interier/.test(b.textContent ?? ''));
    expect(kbds.map((b) => b.textContent)).toEqual([
      expect.stringContaining('obývací'),
      expect.stringContaining('jídelna'),
      expect.stringContaining('nezařízená'),
    ]);
    await user.keyboard('e');
    await user.keyboard(' ');
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [], skipped_tag_ids: [], cant_tell: false, set: 'set_2' },
    ));
  });

  it('a bare URL still sits the routing set, so the current sitting never moved', async () => {
    renderPage();
    await screen.findByText(/Which of these/);
    expect(api.getExamState).toHaveBeenCalledWith('exam_v1', undefined);
    expect(screen.queryByText('routing')).toBeNull();
  });

  it('the letter grid reaches all twelve buttons of a full set', async () => {
    // Twelve positions, keyboard-shaped: w e i o / s d k l / y x n m. Sets are
    // capped at 12 (migration 461) because this is where the keys run out.
    vi.mocked(api.getExamState).mockResolvedValue({
      data: examState({
        tags: Array.from({ length: 12 }, (_, i) => ({ id: 100 + i, label: `tag ${i + 1}` })),
      }),
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.keyboard('x');
    expect(screen.getByRole('button', { name: /tag 10/ }))
      .toHaveAttribute('aria-pressed', 'true');
    await user.keyboard('m');
    expect(screen.getByRole('button', { name: /tag 12/ }))
      .toHaveAttribute('aria-pressed', 'true');
  });
});

describe('<NewDedupExam> abandoned picks', () => {
  it('"none of these" after picking sends none, not the abandoned picks', async () => {
    // The regression the set_2 test exposed: advance() read verdicts from a stale
    // closure, so pick -> change mind -> space submitted the picks anyway.
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Which of these/);
    await user.click(screen.getByRole('button', { name: /interier - koupelna/ }));
    await user.click(screen.getByRole('button', { name: /None of these/ }));
    await waitFor(() => expect(api.answerExamQuestion).toHaveBeenCalledWith(
      'exam_v1',
      { image_id: 555, picked_tag_ids: [], skipped_tag_ids: [], cant_tell: false },
    ));
  });
});

describe('<NewDedupExam> suggestions and the warm-up', () => {
  it('never marks a suggestion on a warm-up image', async () => {
    // The served suggestion belongs to the NEXT REAL question, not the practice
    // image on screen — marking it there would be an answer to the wrong photo.
    vi.mocked(api.getExamState).mockResolvedValue({
      data: examState({
        progress: { total: 250, answered: 0, remaining: 250 },
        question: {
          image_id: 555, position: 13, storage_path: 'img/1/555.jpg',
          suggested_tag_ids: [22],
        },
      }),
    });
    vi.mocked(api.getExamWarmup).mockResolvedValue({
      data: [{ image_id: 900, storage_path: 'img/9/900.jpg' }],
    });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[900, { id: 900, storage_path: 'img/9/900.jpg' } as never]]),
    );
    renderPage();
    expect(await screen.findByText(/Warm-up 1 of 1/)).toBeInTheDocument();
    expect(screen.queryByTestId('suggested-22')).toBeNull();
    expect(screen.queryByTestId('suggested-none')).toBeNull();
  });
});

describe('<NewDedupExam> cohort bar', () => {
  it('offers every cohort with its purpose, preserving the sitting set', async () => {
    // Since migration 464 the exam is plural: the sealed holdout yardstick plus
    // curated re-label cohorts. Switching must not mean hand-editing a URL.
    vi.mocked(api.getExamCohorts).mockResolvedValue({
      data: [
        { name: 'exam_v1', purpose: 'holdout', sealed: true, members: 250 },
        { name: 'gold_v1', purpose: 'curated', sealed: true, members: 300 },
      ],
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/new-dedup/exam?cohort=exam_v1&set=set_2']}>
          <NewDedupExam />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText(/Which of these/);
    const gold = await screen.findByRole('link', { name: /gold_v1/ });
    expect(gold).toHaveAttribute('href', '/new-dedup/exam?cohort=gold_v1&set=set_2');
    expect(gold.textContent).toContain('curated');
    expect(screen.getByRole('link', { name: /exam_v1/ }).textContent).toContain('holdout');
  });

  it('shows no bar while there is only one cohort', async () => {
    vi.mocked(api.getExamCohorts).mockResolvedValue({
      data: [{ name: 'exam_v1', purpose: 'holdout', sealed: true, members: 250 }],
    });
    renderPage();
    await screen.findByText(/Which of these/);
    expect(screen.queryByRole('link', { name: /exam_v1/ })).toBeNull();
  });
});
