/* MemberText — the advert's own words, and the tokens that tell two units of one
 * building apart.
 *
 * Pins:
 *   * the marks never edit the text: concatenating the segments returns the
 *     input, character for character;
 *   * the four token families are found in Czech prose (storey, unit number,
 *     orientation, area) — a regex that quietly stops matching "přízemí" is
 *     invisible on screen;
 *   * a town is not a compass point ("Jihlava" is not "jih"): a highlight that
 *     cries wolf teaches the operator to ignore the highlights;
 *   * a short advert carries NO toggle, a long one collapses and expands;
 *   * nothing is scrubbed here — the API already did it (E28).
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import MemberText, { isLongText, markUnitTokens } from './MemberText';

const marked = (text: string) =>
  markUnitTokens(text)
    .filter((s) => s.mark)
    .map((s) => s.text);

describe('markUnitTokens', () => {
  it('reproduces the input exactly', () => {
    const text = 'Byt č. 14 ve 4. NP, 68,5 m², orientace na jih. Klidná ulice.';
    expect(markUnitTokens(text).map((s) => s.text).join('')).toBe(text);
  });

  it('finds the storey, however the advert spells it', () => {
    expect(marked('Byt ve 4. patře')).toContain('4. patře');
    expect(marked('umístěný ve 2. NP')).toContain('2. NP');
    expect(marked('Byt v přízemí domu')).toContain('přízemí');
    expect(marked('poslední podlaží')).toContain('podlaží');
  });

  it('finds the unit and house numbers', () => {
    expect(marked('Prodej byt č. 14')).toContain('byt č. 14');
    expect(marked('Bytová jednotka č. 7 ve druhém patře')).toContain('Bytová jednotka č. 7');
    expect(marked('na adrese Sokolská č.p. 1180')).toContain('č.p. 1180');
  });

  it('finds the orientation and the area', () => {
    expect(marked('orientace na jihozápad')).toEqual(
      expect.arrayContaining(['orientace', 'jihozápad']),
    );
    expect(marked('severovýchodní balkon')).toContain('severovýchodní');
    expect(marked('užitná plocha 68,5 m² a sklep 3 m2')).toEqual(
      expect.arrayContaining(['68,5 m²', '3 m2']),
    );
  });

  it('finds the south the way a Czech advert actually spells it', () => {
    /* The regression this pins: "severní" is sever+ní so the compass stems reach
     * it, but "jižní" is NOT jih+ní — and "jižní orientace" / "jižní strana" is
     * the commonest way an advert says south-facing. It went silently unmarked. */
    expect(marked('jižní orientace, velký balkon')).toEqual(
      expect.arrayContaining(['jižní', 'orientace']),
    );
    expect(marked('okna směřují jižně')).toContain('jižně');
    expect(marked('výhled jižním směrem')).toContain('jižním');
    expect(marked('jihozápadní terasa')).toContain('jihozápadní');
  });

  it('does not read a town name — or the adverb "již" — as a compass point', () => {
    expect(marked('Prodej v obci Jihlava')).toEqual([]);
    expect(marked('Severka je restaurace')).toEqual([]);
    /* "již" on its own is "already", which is why that branch demands the suffix. */
    expect(marked('dům je již zrekonstruovaný')).toEqual([]);
  });

  it('is empty for an empty text', () => {
    expect(markUnitTokens('')).toEqual([]);
  });
});

describe('isLongText', () => {
  it('is false for an absent or short advert', () => {
    expect(isLongText(null)).toBe(false);
    expect(isLongText('Byt 2+kk v centru.')).toBe(false);
    expect(isLongText('a'.repeat(400))).toBe(true);
  });
});

describe('<MemberText>', () => {
  it('renders the title and marks the discriminating tokens', () => {
    render(<MemberText title="Prodej bytu 3+kk" text="Byt č. 14 ve 4. patře, 68 m²." />);
    expect(screen.getByText('Prodej bytu 3+kk')).toBeInTheDocument();
    const marks = document.querySelectorAll('mark');
    expect([...marks].map((m) => m.textContent)).toEqual(['Byt č. 14', '4. patře', '68 m²']);
  });

  it('shows no toggle for an advert short enough to read whole', () => {
    render(<MemberText text="Byt 2+kk v centru." />);
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('collapses a long advert and expands it on the toggle', async () => {
    const user = userEvent.setup();
    const long = `Byt č. 14 ve 4. patře. ${'Klidná lokalita, jižní orientace. '.repeat(20)}`;
    render(<MemberText text={long} />);
    const toggle = screen.getByRole('button', { name: /zobrazit celý popis/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    /* The whole text is in the DOM either way — the collapse is a clamp, so the
     * browser's own find-in-page keeps working. What changes is the clamp. */
    const body = document.querySelector('p')!;
    expect(body.className).toContain('line-clamp-6');

    await user.click(toggle);
    expect(screen.getByRole('button', { name: /skrýt/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(document.querySelector('p')!.className).not.toContain('line-clamp-6');
  });

  it('works from the keyboard and keeps the focus on the toggle', async () => {
    /* The dialog is read, not clicked through: the operator tabs down five members.
     * A toggle that loses focus on expand sends them back to the top of the dialog
     * — so this pins a real <button> (Enter AND Space) that survives its own state
     * change, which a div with an onClick would not. */
    const user = userEvent.setup();
    render(<MemberText text={`Byt č. 14 ve 4. patře. ${'Klidná lokalita. '.repeat(30)}`} />);
    const toggle = screen.getByRole('button', { name: /zobrazit celý popis/ });

    await user.tab();
    expect(toggle).toHaveFocus();

    await user.keyboard('{Enter}');
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    await user.keyboard(' ');
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it('renders nothing at all when the listing has no text', () => {
    const { container } = render(<MemberText text={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
