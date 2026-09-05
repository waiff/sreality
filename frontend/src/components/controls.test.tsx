/* CollapsibleGroup's accessible name.
 *
 * The copper "there are filters set under here" dot used to carry
 * aria-label="has active filters" on a bare <span> INSIDE the accordion
 * trigger, so the button announced "Essentials has active filters" — the
 * state was folded into the name and `getByRole('button', {name})` stopped
 * matching the band as soon as a filter was set. The dot is decorative now;
 * the state rides on aria-describedby instead. */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { CollapsibleGroup } from './controls';

describe('<CollapsibleGroup>', () => {
  it('is named by its band title alone, with or without active filters', () => {
    const { rerender } = render(
      <CollapsibleGroup title="Essentials">
        <p>panel</p>
      </CollapsibleGroup>,
    );
    expect(screen.getByRole('button', { name: 'Essentials' })).toHaveAttribute(
      'aria-expanded',
      'false',
    );

    rerender(
      <CollapsibleGroup title="Essentials" active>
        <p>panel</p>
      </CollapsibleGroup>,
    );
    const trigger = screen.getByRole('button', { name: 'Essentials' });
    expect(trigger).toHaveAccessibleName('Essentials');
    // Nothing is lost: the state is still announced, as a description.
    expect(trigger).toHaveAccessibleDescription('has active filters');
  });
});
