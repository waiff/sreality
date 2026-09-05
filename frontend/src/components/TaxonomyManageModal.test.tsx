/* TaxonomyManageModal — the "Modify labels" dialog for the NEW DEDUP taxonomy.
 *
 * Props-only, so the harness is the component itself. Pins the two fields that
 * a placeholder used to "name": the add field (a visible "New label" caption)
 * and the inline rename field, which is anonymous exactly when it autofocuses
 * because it REPLACES the label text it would otherwise be named by. The
 * rename field reuses TagDefinitionList's wording so one query names the
 * affordance on both surfaces.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import TaxonomyManageModal from './TaxonomyManageModal';
import type { TaxonomyManageModalProps } from './TaxonomyManageModal';
import type { NewDedupTag } from '@/lib/api';

const tag = (over: Partial<NewDedupTag> = {}): NewDedupTag => ({
  id: 1,
  label: 'interier - kuchyne',
  priority: false,
  ready_for_training: false,
  positive_count: 4,
  negative_count: 2,
  excluded_count: 0,
  ...over,
} as NewDedupTag);

const props = (over: Partial<TaxonomyManageModalProps> = {}): TaxonomyManageModalProps => ({
  labels: [tag()],
  onClose: vi.fn(),
  newLabelText: '',
  onNewLabelTextChange: vi.fn(),
  onAdd: vi.fn(),
  addPending: false,
  onRename: vi.fn(),
  renamePending: false,
  onRemove: vi.fn(),
  removePending: false,
  onSetFlags: vi.fn(),
  flagsPending: false,
  ...over,
});

describe('<TaxonomyManageModal>', () => {
  it('names the add field "New label" and keeps the Add button its own name', () => {
    render(<TaxonomyManageModal {...props()} />);
    expect(screen.getByRole('textbox', { name: 'New label' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add label' })).toBeInTheDocument();
  });

  it('still routes typing in the named add field to onNewLabelTextChange', () => {
    const onNewLabelTextChange = vi.fn();
    render(<TaxonomyManageModal {...props({ onNewLabelTextChange })} />);
    fireEvent.change(screen.getByRole('textbox', { name: 'New label' }), {
      target: { value: 'interier - koupelna' },
    });
    expect(onNewLabelTextChange).toHaveBeenCalledWith('interier - koupelna');
  });

  it('names the inline rename field, which replaces the row label it would be named by', () => {
    render(<TaxonomyManageModal {...props()} />);
    fireEvent.click(screen.getByRole('button', { name: 'rename' }));
    const field = screen.getByRole('textbox', { name: 'New tag label' });
    expect(field).toHaveAccessibleName('New tag label');
    expect(field).toHaveValue('interier - kuchyne');
  });
});
