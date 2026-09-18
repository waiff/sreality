/* AUTODEDUP · the advert's own words, for the one question the photos cannot
 * answer.
 *
 * A developer project is the hard cell of this engine: five units in one
 * building share the render set, the price band, the disposition and the floor
 * count, so a card of photos and an attribute row look identical five times
 * over. What differs is the TEXT — "byt č. 14", "ve 4. NP", "orientace na jih",
 * "68 m²". This component puts that text on the group dialog and on the pair
 * page, and marks those tokens so the operator finds them without reading two
 * thousand characters of estate-agent prose twice.
 *
 * THE HIGHLIGHT IS A FINDING AID, NOT A CLAIM. It never says two adverts agree
 * or disagree — it only points at the sentences worth comparing. `markUnitTokens`
 * is pure and tested on its own, because a regex that quietly stops matching
 * "přízemí" is invisible on screen: the page still renders, just without the
 * one word the operator opened it for.
 *
 * The text arrives already PII-scrubbed by the API (E28, `judge.scrubbed_text`);
 * nothing here scrubs, and nothing here should — a second copy of those rules is
 * how a leak ships.
 */

import { useState } from 'react';

/* A Czech letter, for the word boundaries `\w` gets wrong: `\w` stops dead at
 * ř, so `patr\w*` never matches "patře" — the very word being hunted. */
const L = 'a-zA-Z\\u00C0-\\u017F';
const NOT_L = `(?<![${L}])`;

/* The tokens that tell two units of one building apart, best first. Every branch
 * is anchored on a non-letter so "seber" is not an orientation and "bytost" is
 * not a unit number. */
const TOKEN_SOURCE: string = [
  /* an area: a number with its unit, "68 m²" / "68,5 m2" */
  String.raw`\d+(?:[.,]\d+)?\s*m(?:²|2|\^2)(?![${L}])`,
  /* a storey, with the ordinal in front of it when there is one. `pat[rř]` and not
   * `patr`: the locative every Czech advert actually uses is "ve 4. patře", with
   * a ř — a prefix of "patro" matches the nominative only. */
  String.raw`${NOT_L}(?:\d+\.?\s*)?(?:nadzemní\s+|podzemní\s+)?(?:mezi)?(?:pat[rř][${L}]*|podlaž[${L}]*|poschod[${L}]*)`,
  String.raw`${NOT_L}\d+\.\s*(?:NP|PP)(?![${L}])`,
  String.raw`${NOT_L}(?:přízem|suterén|podkrov)[${L}]*`,
  /* a unit or house number: "byt č. 14", "jednotka 4", "č. p. 1180" */
  String.raw`${NOT_L}(?:bytov[${L}]*\s+jednotk[${L}]*|jednotk[${L}]*|byt[${L}]*)\s*(?:č\.?|čís(?:lo)?\.?)\s*\d+[${L}]*`,
  String.raw`${NOT_L}č\.\s*(?:p\.|or\.)?\s*\d+`,
  /* which way it faces. The compass words carry a TRAILING boundary of their own:
   * "jih" is also the first three letters of Jihlava, and a highlighted town
   * name teaches the operator to stop trusting the marks. */
  String.raw`${NOT_L}(?:orientac[${L}]*|orientovan[${L}]*)`,
  String.raw`${NOT_L}(?:sever|jih|východ|západ)(?:o(?:zápa|výcho)d)?(?:n[íě][${L}]*|u|em)?(?![${L}])`,
  /* "jižní" / "jižně" — the h→ž the adjective makes, which the stems above cannot
   * reach: "severní" is sever+ní but "jižní" is NOT jih+ní. Its own branch and not
   * a fifth stem, because the suffix has to be MANDATORY here: bare `již` is the
   * adverb "already", the commonest word in an advert that is not a compass point
   * at all. The compounds keep matching above ("jihozápadní" is jih+o+západ+ní). */
  String.raw`${NOT_L}jižn[íě][${L}]*`,
].join('|');

export interface TextSegment {
  text: string;
  /* True when this run is one of the discriminating tokens above. */
  mark: boolean;
}

/* Long enough that collapsing it actually saves the operator something. Six
 * lines of ~60 characters, so a short advert is simply shown whole and carries
 * no toggle at all — a control that does nothing is worse than no control.
 * A character count and not a measured height on purpose: it answers the same
 * in jsdom, in a hidden dialog and on screen, and it is the thing tests pin. */
export const COLLAPSE_OVER_CHARS = 360;

export function isLongText(text: string | null | undefined): boolean {
  return (text ?? '').length > COLLAPSE_OVER_CHARS;
}

/* The advert split into plain runs and token runs, in order. Concatenating
 * `segment.text` reproduces the input exactly — the marks never edit the text. */
export function markUnitTokens(text: string): TextSegment[] {
  if (!text) return [];
  const re = new RegExp(TOKEN_SOURCE, 'giu');
  const out: TextSegment[] = [];
  let at = 0;
  for (const match of text.matchAll(re)) {
    const start = match.index ?? 0;
    if (!match[0]) continue;
    if (start > at) out.push({ text: text.slice(at, start), mark: false });
    out.push({ text: match[0], mark: true });
    at = start + match[0].length;
  }
  if (at < text.length) out.push({ text: text.slice(at), mark: false });
  return out;
}

export interface MemberTextProps {
  /* The advert's headline, when the surface has one. */
  title?: string | null;
  text: string | null | undefined;
  /* The heading level the surrounding surface expects; the dialog's members sit
   * under an h2 and the pair page's panels under an h3. */
  headingLevel?: 3 | 4;
  /* Distinguishes the toggles when several members are on screen. */
  label?: string;
}

export default function MemberText({
  title,
  text,
  headingLevel = 4,
  label,
}: MemberTextProps) {
  /* Per COMPONENT, which is per member: the dialog keeps its members mounted, so
   * an advert the operator opened stays open while they read the next one, and
   * closing the dialog is what forgets it. */
  const [expanded, setExpanded] = useState(false);
  const body = text ?? '';
  if (!title && !body) return null;

  const long = isLongText(body);
  const collapsed = long && !expanded;
  const Heading = headingLevel === 3 ? 'h3' : 'h4';

  return (
    <div className="mt-1 max-w-[60ch] space-y-1">
      {title && (
        <Heading className="text-[0.78rem] leading-snug text-[var(--color-ink)]">
          {title}
        </Heading>
      )}
      {body && (
        <p
          className={
            'text-[0.72rem] leading-relaxed text-[var(--color-ink-2)] whitespace-pre-line ' +
            (collapsed ? 'line-clamp-6' : '')
          }
        >
          {markUnitTokens(body).map((segment, index) =>
            segment.mark ? (
              <mark
                key={index}
                className="rounded-[2px] bg-[var(--color-ochre-soft)] px-[1px] text-[var(--color-ink)]"
              >
                {segment.text}
              </mark>
            ) : (
              <span key={index}>{segment.text}</span>
            ),
          )}
        </p>
      )}
      {long && (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((open) => !open)}
          className="text-[0.7rem] text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
        >
          {expanded ? 'skrýt' : 'zobrazit celý popis'}
          {label && <span className="sr-only"> {label}</span>}
        </button>
      )}
    </div>
  );
}
