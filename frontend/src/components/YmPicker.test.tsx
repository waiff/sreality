/* THE year/month picker. Three copies used to ship two <select>s with an
 * accessible name of "" (the first computed "Scrape from 01" from a sibling
 * caption, the second the empty string). One caption, two distinct names. */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { expectNoNestedInteractive } from '@/test/a11y';
import { YmPicker, YM_PARTS } from './YmPicker';

describe('<YmPicker>', () => {
  it('names the group and BOTH selects, Czech by default', () => {
    render(<YmPicker label="Od" value="2023-05" />);
    expect(screen.getByRole('group', { name: 'Od' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Od – rok' })).toHaveValue('2023');
    expect(screen.getByRole('combobox', { name: 'Od – měsíc' })).toHaveValue('05');
  });

  it('takes the English part words for the English surfaces', () => {
    render(<YmPicker label="Scrape from" value="2021-11" parts={YM_PARTS.en} size="md" />);
    expect(screen.getByRole('combobox', { name: 'Scrape from – year' })).toHaveValue('2021');
    expect(screen.getByRole('combobox', { name: 'Scrape from – month' })).toHaveValue('11');
  });

  it('keeps two pickers in one row distinguishable', () => {
    render(
      <>
        <YmPicker label="Od" value="2020-01" />
        <YmPicker label="Do" value="2024-12" />
      </>,
    );
    expect(screen.getByRole('combobox', { name: 'Od – rok' })).toHaveValue('2020');
    expect(screen.getByRole('combobox', { name: 'Do – rok' })).toHaveValue('2024');
  });

  it('composes YYYY-MM on change, from either select', () => {
    const onChange = vi.fn();
    render(<YmPicker label="Od" value="2023-05" onChange={onChange} />);
    fireEvent.change(screen.getByRole('combobox', { name: 'Od – rok' }), { target: { value: '2019' } });
    expect(onChange).toHaveBeenLastCalledWith('2019-05');
    fireEvent.change(screen.getByRole('combobox', { name: 'Od – měsíc' }), { target: { value: '12' } });
    expect(onChange).toHaveBeenLastCalledWith('2023-12');
  });

  it('falls back to the first archive month on an empty value', () => {
    render(<YmPicker label="Od" value="" />);
    expect(screen.getByRole('combobox', { name: 'Od – rok' })).toHaveValue('2015');
    expect(screen.getByRole('combobox', { name: 'Od – měsíc' })).toHaveValue('01');
  });

  it('nests no interactive inside another', () => {
    const { container } = render(<YmPicker label="Od" value="2023-05" />);
    expectNoNestedInteractive(container);
  });
});
