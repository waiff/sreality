/* Field is the one caption-over-control(s) primitive. These pin the two
 * defects its six predecessors carried, in the direction that matters: a
 * caption over a GROUP must not name the first control, and clicking it must
 * not activate anything. */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { expectNoNestedInteractive } from '@/test/a11y';
import { Field, PickButton, Section, Segmented } from './controls';

const OPTS = [
  { value: 'prodej', label: 'Prodej' },
  { value: 'pronajem', label: 'Pronájem' },
  { value: null, label: 'Vše' },
] as const;

function Picker({ onChange }: { onChange: (v: string | null) => void }) {
  const [v, setV] = useState<string | null>(null);
  return (
    <Field label="Nabídka">
      <Segmented
        options={OPTS}
        value={v}
        onChange={(next) => {
          setV(next);
          onChange(next);
        }}
      />
    </Field>
  );
}

describe('<Field as="group"> (the default)', () => {
  it('names the group, and each pill keeps its OWN name', () => {
    render(<Picker onChange={() => {}} />);
    const group = screen.getByRole('group', { name: 'Nabídka' });
    // Before: the first pill announced as "Nabídka Prodej Pronájem Vše".
    expect(within(group).getByRole('button', { name: 'Prodej' })).toHaveAccessibleName('Prodej');
    expect(within(group).getByRole('button', { name: 'Vše' })).toHaveAccessibleName('Vše');
  });

  it('clicking the caption changes NOTHING', () => {
    // A <label> wrap made the caption an activation surface for the first
    // pill: clicking the word "Nabídka" selected "Prodej". HTML label
    // activation is browser-universal, so this is the half a role query
    // cannot see — it has to be asserted as a negative.
    const onChange = vi.fn();
    render(<Picker onChange={onChange} />);
    fireEvent.click(screen.getByText('Nabídka'));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Prodej' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('nests no interactive inside another', () => {
    const { container } = render(<Picker onChange={() => {}} />);
    expectNoNestedInteractive(container);
  });

  it('announces help text through aria-describedby', () => {
    render(
      <Field label="counts" help="the cases that clearly belong">
        <PickButton on={false} onClick={() => {}}>
          x
        </PickButton>
      </Field>,
    );
    expect(screen.getByRole('group', { name: 'counts' })).toHaveAccessibleDescription(
      'the cases that clearly belong',
    );
  });
});

describe('<Field as="control">', () => {
  it('labels exactly one control by wrapping it', () => {
    render(
      <Field label="Název" as="control">
        <input defaultValue="" />
      </Field>,
    );
    expect(screen.getByRole('textbox', { name: 'Název' })).toBeInTheDocument();
    expect(screen.queryByRole('group')).toBeNull();
  });

  it('gives a previously unnamed textarea its name', () => {
    // Settings' "System prompt" used a <div> caption: the textarea had no name.
    render(
      <Field label="System prompt" as="control">
        <textarea defaultValue="" />
      </Field>,
    );
    expect(screen.getByRole('textbox', { name: 'System prompt' })).toBeInTheDocument();
  });
});

describe('<Section> (the sidebar alias)', () => {
  it('is a named group now, with its 18 call sites untouched', () => {
    render(
      <Section label="Dispozice">
        <PickButton on={false} onClick={() => {}}>
          2+kk
        </PickButton>
      </Section>,
    );
    const group = screen.getByRole('group', { name: 'Dispozice' });
    expect(within(group).getByRole('button', { name: '2+kk' })).toHaveAccessibleName('2+kk');
  });
});
