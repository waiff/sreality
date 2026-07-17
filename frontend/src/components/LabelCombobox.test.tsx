/* LabelCombobox — searchable/creatable single-select over {value,label} options.
 *
 * Behaviour pinned:
 *   - focusing a prefilled field shows the FULL option list (not filtered down to
 *     the one option matching its own prefilled text — the reported bug)
 *   - typing filters by label, matching case-insensitively
 *   - picking an option commits its canonical VALUE, not the displayed label
 *   - typing free text with no matching option offers "Create", which commits the
 *     raw text as both value and label (open vocabulary)
 *   - typing text that case-insensitively matches an existing option's label does
 *     NOT offer "Create" (resolves back to the existing option instead)
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import LabelCombobox, { type LabelOption } from './LabelCombobox';

const OPTIONS: LabelOption[] = [
  { value: 'hallway', label: 'chodba' },
  { value: 'kitchen', label: 'kuchyně' },
  { value: 'bathroom', label: 'koupelna' },
];

describe('<LabelCombobox>', () => {
  it('shows every option on focus even though the field is prefilled with one of them', () => {
    render(<LabelCombobox value="hallway" onChange={vi.fn()} options={OPTIONS} />);
    fireEvent.focus(screen.getByRole('textbox'));
    const items = screen.getAllByRole('option');
    expect(items).toHaveLength(3);
  });

  it('filters options by label as the operator types', () => {
    render(<LabelCombobox value="hallway" onChange={vi.fn()} options={OPTIONS} />);
    const input = screen.getByRole('textbox');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'kuch' } });
    // "kuch" matches only "kuchyně" among real options; a "Create" row also renders
    // (the text isn't an exact match) — assert on the real-option label, not the count.
    expect(screen.getByText('kuchyně')).toBeInTheDocument();
    expect(screen.queryByText('chodba')).not.toBeInTheDocument();
    expect(screen.queryByText('koupelna')).not.toBeInTheDocument();
  });

  it('commits the canonical value (not the label) when an option is picked', () => {
    const onChange = vi.fn();
    render(<LabelCombobox value="hallway" onChange={onChange} options={OPTIONS} />);
    const input = screen.getByRole('textbox');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'kuch' } });
    fireEvent.click(screen.getByText('kuchyně'));
    expect(onChange).toHaveBeenCalledWith('kitchen');
  });

  it('offers Create for free text matching no existing option, and commits it raw on click', () => {
    const onChange = vi.fn();
    render(<LabelCombobox value="hallway" onChange={onChange} options={OPTIONS} />);
    const input = screen.getByRole('textbox');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'vestavěná skříň' } });
    const create = screen.getByText('Create “vestavěná skříň”');
    fireEvent.click(create);
    expect(onChange).toHaveBeenCalledWith('vestavěná skříň');
  });

  it('does not offer Create when the typed text case-insensitively matches an option', () => {
    render(<LabelCombobox value="hallway" onChange={vi.fn()} options={OPTIONS} />);
    const input = screen.getByRole('textbox');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'KUCHYNĚ' } });
    expect(screen.queryByText(/Create/)).not.toBeInTheDocument();
  });

  it('commits typed free text on blur (e.g. clicking a sibling Train button)', () => {
    const onChange = vi.fn();
    render(<LabelCombobox value="hallway" onChange={onChange} options={OPTIONS} />);
    const input = screen.getByRole('textbox');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'nová místnost' } });
    fireEvent.blur(input);
    expect(onChange).toHaveBeenCalledWith('nová místnost');
  });

  it('resolves typed text matching an option to that option on blur, not the raw text', () => {
    const onChange = vi.fn();
    render(<LabelCombobox value="hallway" onChange={onChange} options={OPTIONS} />);
    const input = screen.getByRole('textbox');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'koupelna' } });
    fireEvent.blur(input);
    expect(onChange).toHaveBeenCalledWith('bathroom');
  });
});
