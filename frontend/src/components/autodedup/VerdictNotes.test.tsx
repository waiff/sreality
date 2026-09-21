/* VerdictNotes — the reason chips and the note (migration 533).
 *
 * The wiring per surface is pinned on the pages themselves (Residual, Pair,
 * Groups). What is pinned HERE is the component's own contract, because five
 * surfaces depend on it: the chips are a multi-select of aria-pressed buttons,
 * the note is one line, an empty note travels as null rather than '', the two
 * comparisons that decide whether "Uložit poznámku" is armed — and that the
 * whole thing is COLLAPSED and OPTIONAL: neither half is ever required (D39).
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import VerdictNotes, {
  annotationInput,
  sameAnnotation,
  storedAnnotation,
  type VerdictAnnotation,
} from './VerdictNotes';
import * as api from '@/lib/api';
import type { AutodedupVerdictRow } from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, getAutodedupVerdictReasons: vi.fn() };
});

const REASONS = [
  { code: 'floor_plan_differs', label: 'Jiný půdorys' },
  { code: 'broker', label: 'Stejný makléř' },
];

function renderNotes(value: VerdictAnnotation, over: Record<string, unknown> = {}) {
  vi.mocked(api.getAutodedupVerdictReasons).mockResolvedValue(REASONS);
  const onChange = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <VerdictNotes defaultOpen value={value} onChange={onChange} {...over} />
    </QueryClientProvider>,
  );
  return onChange;
}

const EMPTY: VerdictAnnotation = { reasons: [], note: '' };

describe('<VerdictNotes>', () => {
  it('toggles a chip on and off, keeping the click order', async () => {
    const user = userEvent.setup();
    const onChange = renderNotes({ reasons: ['broker'], note: '' });
    const chip = await screen.findByRole('button', { name: 'Jiný půdorys' });
    expect(chip).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: 'Stejný makléř' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await user.click(chip);
    /* Appended, not sorted: the order the operator clicked is the order the
     * chips read back in. */
    expect(onChange).toHaveBeenCalledWith({ reasons: ['broker', 'floor_plan_differs'], note: '' });
  });

  it('un-picks a chip that is already on', async () => {
    const user = userEvent.setup();
    const onChange = renderNotes({ reasons: ['broker'], note: 'x' });
    await user.click(await screen.findByRole('button', { name: 'Stejný makléř' }));
    expect(onChange).toHaveBeenCalledWith({ reasons: [], note: 'x' });
  });

  it('shows the save button only when the annotation is dirty', async () => {
    renderNotes(EMPTY);
    await screen.findByRole('button', { name: 'Jiný půdorys' });
    expect(screen.queryByRole('button', { name: 'Uložit poznámku' })).toBeNull();
  });

  it('offers the save button once the caller says the stored row disagrees', async () => {
    const onSave = vi.fn();
    renderNotes({ reasons: ['broker'], note: '' }, { dirty: true, onSave });
    await screen.findByRole('button', { name: 'Uložit poznámku' });
  });

  it('is COLLAPSED by default, and nothing about it is required', async () => {
    vi.mocked(api.getAutodedupVerdictReasons).mockResolvedValue(REASONS);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <VerdictNotes value={EMPTY} onChange={vi.fn()} />
      </QueryClientProvider>,
    );
    /* One dotted toggle and nothing else: the operator annotates sometimes, so
     * an open picker per row would be a question asked on every scroll. */
    expect(screen.getByRole('button', { name: '+ důvod / poznámka' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Jiný půdorys' })).toBeNull();
    expect(screen.queryByLabelText('Poznámka')).toBeNull();
  });

  it('renders the note input even before the registry lands', async () => {
    vi.mocked(api.getAutodedupVerdictReasons).mockRejectedValue(new Error('offline'));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <VerdictNotes defaultOpen value={EMPTY} onChange={vi.fn()} />
      </QueryClientProvider>,
    );
    /* A vocabulary the page could not read must not take the note down with it. */
    expect(screen.getByLabelText('Poznámka')).toBeInTheDocument();
  });
});

describe('the annotation helpers', () => {
  it('reads a stored verdict, treating an absent reasons array as none', () => {
    const row = { verdict: 'same', note: 'ok' } as AutodedupVerdictRow;
    expect(storedAnnotation(row)).toEqual({ reasons: [], note: 'ok' });
    expect(storedAnnotation(null)).toEqual({ reasons: [], note: '' });
  });

  it('sends an empty note as null, never as the empty string', () => {
    expect(annotationInput({ reasons: [], note: '   ' })).toEqual({ reasons: [], note: null });
    expect(annotationInput({ reasons: ['broker'], note: ' a ' })).toEqual({
      reasons: ['broker'],
      note: 'a',
    });
  });

  it('compares order-sensitively, and ignores whitespace around the note', () => {
    expect(sameAnnotation({ reasons: ['a', 'b'], note: 'x ' }, { reasons: ['a', 'b'], note: 'x' }))
      .toBe(true);
    expect(sameAnnotation({ reasons: ['a', 'b'], note: '' }, { reasons: ['b', 'a'], note: '' }))
      .toBe(false);
  });
});
