/* These cells were extracted from eight hand-rolled tables that had to keep
 * rendering byte-for-byte what they rendered before. The class strings below
 * are the ones the old local copies emitted; a "harmless" reorder or a dropped
 * utility here silently restyles a page nobody opened during the change. */

import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';

import { StatTd, StatTh, Th } from './table';

function head(ui: React.ReactNode) {
  const { container } = render(
    <table>
      <thead>
        <tr>{ui}</tr>
      </thead>
    </table>,
  );
  return container.querySelector('th') as HTMLTableCellElement;
}

function body(ui: React.ReactNode) {
  const { container } = render(
    <table>
      <tbody>
        <tr>{ui}</tr>
      </tbody>
    </table>,
  );
  return container.querySelector('td') as HTMLTableCellElement;
}

const TH_TAIL = 'tracking-[0.14em] uppercase font-medium text-[var(--color-ink-3)]';

describe('<Th>', () => {
  it('defaults to the page-table size, left-aligned, and is a column header', () => {
    const th = head(<Th>ID</Th>);
    expect(th).toHaveAttribute('scope', 'col');
    expect(th.getAttribute('class')).toBe(`px-4 py-2.5 text-[0.7rem] ${TH_TAIL} text-left`);
  });

  it('emits exactly one padding/size set per variant', () => {
    expect(head(<Th align="right">n</Th>).getAttribute('class')).toBe(
      `px-4 py-2.5 text-[0.7rem] ${TH_TAIL} text-right`,
    );
    expect(head(<Th size="sm">n</Th>).getAttribute('class')).toBe(
      `px-3 py-2.5 text-[0.7rem] ${TH_TAIL} text-left`,
    );
    expect(head(<Th size="xs" align="right">n</Th>).getAttribute('class')).toBe(
      `px-3 py-2 text-[0.65rem] ${TH_TAIL} text-right`,
    );
  });
});

describe('<StatTh> / <StatTd>', () => {
  it('keeps the numeric-summary header right-aligned with its tighter tracking', () => {
    const th = head(<StatTh>median</StatTh>);
    expect(th.getAttribute('class')).toBe(
      'px-3 py-2 font-medium text-right text-[var(--color-ink-3)] tracking-wide uppercase text-[0.65rem]',
    );
    /* Unlike <Th>, both originals omitted scope; adding it restyles nothing but
     * changes the DOM of two live tables. */
    expect(th).not.toHaveAttribute('scope');
  });

  it('marks the emphasised cell with ink, the rest with ink-2', () => {
    expect(body(<StatTd>12</StatTd>).getAttribute('class')).toBe(
      'px-3 py-1.5 text-right text-[var(--color-ink-2)]',
    );
    expect(body(<StatTd bold>12</StatTd>).getAttribute('class')).toBe(
      'px-3 py-1.5 text-right font-medium text-[var(--color-ink)]',
    );
  });
});
