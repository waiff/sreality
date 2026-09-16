/* AUTODEDUP · the BLOCK filter, as a vocabulary rather than a text field.
 *
 * WHAT WAS WRONG. `block` is `autodedup.clusters.block_key` — a RUIAN code, a
 * bigint. The control was a free-text input, so typing the thing an operator
 * actually knows ("Jablonec nad Nisou") produced NaN, which the query layer
 * drops: the filter silently did nothing and the queue answered with the
 * unfiltered list. A filter that cannot fail visibly is worse than no filter.
 *
 * WHAT IT IS NOW. The blocks THIS generation clustered, named off
 * `listing_location`, each with how many groups sit in it — so the choice is
 * made from what exists, in the operator's own words, and the empty options are
 * not offered at all.
 *
 * A BLOCK IS A CODE AND A GRAIN. `o563510` is a town, `c490245` a quarter
 * (migration 529) and the two vocabularies share their NUMBER SPACE, so the code
 * alone can name two different blocks — the exact conflation that migration
 * added `block_grain` to end. The grain is therefore part of the VALUE, not just
 * the label: the option is `c490245`, the query carries `block` + `block_grain`,
 * and the url says which of the two a shared link meant. A bare code (a link
 * written before the grain existed) still filters, grain-blind, as it always did.
 *
 * FAILURE IS QUIET, NOT FATAL. If the vocabulary read fails the select still
 * renders with "all blocks" (and any block already in the URL kept as its own
 * option) — losing the names must never cost the operator the filter they had.
 */

import { useQuery } from '@tanstack/react-query';

import { getAutodedupBlocks, type AutodedupBlock } from '@/lib/api';
import { fmtCount } from '@/lib/format';

const GRAIN_WORDS: Record<string, string> = { o: 'obec', c: 'část obce' };

/* The one spelling of a block as a single URL-safe token: grain letter + code,
 * the same shape the engine itself keys blocks with (`fingerprint.block_key_of`).
 * One key in the url, two parameters on the wire. */
export const blockValue = (b: AutodedupBlock): string => `${b.block_grain ?? ''}${b.block_key}`;

export interface BlockFilterValue {
  block: number | null;
  block_grain: string | null;
}

/* `c490245` -> the pair; `563510` -> the code with no grain, which the server
 * reads as "either" — and anything unparseable is no filter at all rather than
 * a `?block=NaN` the API answers with a 422 nobody can read. */
export function parseBlockValue(value: string): BlockFilterValue {
  const raw = value.trim();
  if (raw === '') return { block: null, block_grain: null };
  const grain = raw[0] in GRAIN_WORDS ? raw[0] : null;
  const digits = grain ? raw.slice(1) : raw;
  if (digits === '' || !/^\d+$/.test(digits)) return { block: null, block_grain: null };
  return { block: Number(digits), block_grain: grain };
}

/* "Jablonec nad Nisou (563510) · 412 skupin". The code stays visible next to the
 * name: it is what the API filters on and what an operator pastes into a psql
 * session, and a picker that hid it would make the two surfaces untranslatable. */
export function blockLabel(block: AutodedupBlock): string {
  const grain = block.block_grain ? GRAIN_WORDS[block.block_grain] : null;
  const head = block.name
    ? `${block.name} (${block.block_key})`
    : /* No resolved name: the code alone, never a guessed town. */
      `#${block.block_key}`;
  const parts = [head];
  if (grain) parts.push(grain);
  parts.push(`${fmtCount(block.n_clusters)} skupin`);
  return parts.join(' · ');
}

export default function BlockSelect({
  value,
  onChange,
  generation,
  label = 'Block',
  labelClassName,
  controlClassName,
}: {
  /* The block key as it travels in the URL and on the query — '' means "all". */
  value: string;
  onChange: (next: string) => void;
  generation: string;
  label?: string;
  labelClassName?: string;
  controlClassName?: string;
}) {
  const q = useQuery({
    queryKey: ['autodedup', 'blocks', generation],
    queryFn: () => getAutodedupBlocks(generation),
    /* The blocks of a generation change only when the lane rebuilds it. */
    staleTime: 5 * 60_000,
    retry: false,
  });
  const blocks = q.data?.data?.items ?? [];
  /* A block the URL carries that the vocabulary does not (yet) list — a link
   * from another generation, or a read that failed — stays selectable rather
   * than snapping the operator back to "all" behind their back. */
  const unknown = value !== '' && !blocks.some((b) => blockValue(b) === value) ? value : null;
  const unknownCode = parseBlockValue(unknown ?? '').block;

  return (
    <label className="block">
      <span className={labelClassName}>{label}</span>
      <select
        className={controlClassName}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">
          {q.isPending ? 'vše (načítám…)' : `vše (${fmtCount(blocks.length)} bloků)`}
        </option>
        {unknown && <option value={unknown}>#{unknownCode ?? unknown}</option>}
        {blocks.map((b) => (
          <option key={blockValue(b)} value={blockValue(b)}>
            {blockLabel(b)}
          </option>
        ))}
      </select>
    </label>
  );
}
