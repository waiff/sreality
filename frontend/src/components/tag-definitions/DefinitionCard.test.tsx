/* The card is the operator's whole view of a definition while labeling, so the
 * assertions here are about what it must NEVER show as much as what it shows. */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import DefinitionCard from './DefinitionCard';
import type { TagHandbookCard } from '@/lib/api';

const card = (over: Partial<TagHandbookCard> = {}): TagHandbookCard => ({
  tag_label: 'interier - koupelna',
  headline: 'Is this photo of interier - koupelna — an image of a bathroom?',
  count_it: ['A bathroom with a shower', 'Public showers'],
  dont_count_it: ['A lone toilet — that is “interier - wc” instead.'],
  cant_tell: ['you cannot tell a bathroom sink-room from a public toilet'],
  ...over,
});

describe('<DefinitionCard>', () => {
  it('never shows the storage field names', () => {
    // The four boxes in the editor are the shape CODE needs. Being shown them is
    // what made definition-writing confusing in the first place; the card exists
    // precisely so a person labeling never meets them.
    const { container } = render(<DefinitionCard card={card()} />);
    const text = container.textContent ?? '';
    for (const name of ['counts', 'does_not_count', 'confusable_with', 'leave_out_when']) {
      expect(text).not.toContain(name);
    }
  });

  it('leads with the question the labeler has to answer', () => {
    render(<DefinitionCard card={card()} />);
    expect(screen.getByRole('heading', { level: 4 })).toHaveTextContent(/^Is this photo of/);
  });

  it('shows every line the renderer produced', () => {
    render(<DefinitionCard card={card()} />);
    expect(screen.getByText('A bathroom with a shower')).toBeInTheDocument();
    expect(screen.getByText(/A lone toilet/)).toBeInTheDocument();
    expect(screen.getByText(/bathroom sink-room/)).toBeInTheDocument();
  });

  it('omits a section that has nothing in it', () => {
    // A heading over an empty list would make "this tag has no exclusions" and
    // "the exclusions failed to load" look identical.
    render(<DefinitionCard card={card({ cant_tell: [] })} />);
    expect(screen.queryByText(/Leave out/)).toBeNull();
    expect(screen.getByText('Count it')).toBeInTheDocument();
  });

  it('says when it is previewing something unsaved', () => {
    render(<DefinitionCard card={card()} draft />);
    expect(screen.getByText('unsaved draft')).toBeInTheDocument();
  });

  it('does not claim to be a draft when it is showing the saved definition', () => {
    render(<DefinitionCard card={card()} />);
    expect(screen.queryByText('unsaved draft')).toBeNull();
  });

  it('explains itself rather than rendering three empty headings', () => {
    render(<DefinitionCard card={card({ count_it: [], dont_count_it: [], cant_tell: [] })} />);
    expect(screen.getByText(/Nothing to show yet/)).toBeInTheDocument();
  });

  it('is findable by what it is for', () => {
    render(<DefinitionCard card={card()} />);
    expect(
      screen.getByRole('region', { name: 'How to label interier - koupelna' }),
    ).toBeInTheDocument();
  });
});
