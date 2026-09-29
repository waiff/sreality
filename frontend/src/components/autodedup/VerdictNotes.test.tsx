/* VerdictNotes — the operator's note beside a verdict.
 *
 * The wiring per surface is pinned on the pages themselves (Residual, Pair,
 * Groups, Soudce). What is pinned HERE is the component's own contract, because
 * every review surface depends on it: the note is one line, an empty note
 * travels as null rather than '', the comparison that decides whether "Uložit
 * poznámku" is armed — and that it is COLLAPSED and OPTIONAL (D39). There is no
 * reason picker any more: binary verdicts, and a note in the operator's words.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import VerdictNotes, {
  annotationInput,
  sameAnnotation,
  storedAnnotation,
  type VerdictAnnotation,
} from './VerdictNotes';
import type { AutodedupVerdictRow } from '@/lib/api';

function renderNotes(value: VerdictAnnotation, over: Record<string, unknown> = {}) {
  const onChange = vi.fn();
  render(<VerdictNotes defaultOpen value={value} onChange={onChange} {...over} />);
  return onChange;
}

const EMPTY: VerdictAnnotation = { reasons: [], note: '' };

describe('<VerdictNotes>', () => {
  it('edits the note and keeps any stored reason codes untouched', async () => {
    const user = userEvent.setup();
    const onChange = renderNotes({ reasons: ['broker'], note: '' });
    await user.type(screen.getByLabelText('Poznámka'), 'x');
    expect(onChange).toHaveBeenCalledWith({ reasons: ['broker'], note: 'x' });
    /* No chip to pick: the page asks two answers and a note. */
    expect(screen.queryByRole('button', { name: 'Jiný půdorys' })).toBeNull();
  });

  it('shows the save button only when the annotation is dirty', () => {
    renderNotes(EMPTY);
    expect(screen.getByLabelText('Poznámka')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Uložit poznámku' })).toBeNull();
  });

  it('offers the save button once the caller says the stored row disagrees', async () => {
    const onSave = vi.fn();
    renderNotes({ reasons: [], note: 'x' }, { dirty: true, onSave });
    await screen.findByRole('button', { name: 'Uložit poznámku' });
  });

  it('is COLLAPSED by default, and nothing about it is required', () => {
    render(<VerdictNotes value={EMPTY} onChange={vi.fn()} />);
    /* One dotted toggle and nothing else: the operator annotates sometimes, so
     * an open note box per row would be a question asked on every scroll. */
    expect(screen.getByRole('button', { name: '+ poznámka' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Poznámka')).toBeNull();
  });

  it('opens itself on a row that arrives with a note', () => {
    render(<VerdictNotes value={{ reasons: [], note: 'jiné patro' }} onChange={vi.fn()} />);
    expect(screen.getByLabelText('Poznámka')).toHaveValue('jiné patro');
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
