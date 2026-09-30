/* AUTODEDUP · the engine's view of two adverts in words — one map and one line
 * for every page that sets a ruling or the judge beside the engine. */

import type { RulingEngineView, RulingPairRow } from '@/lib/api';

export const ENGINE_VIEW: Record<RulingEngineView, string> = {
  together: 'jedna skupina',
  apart: 'odděleně',
  unseen: 'inzeráty neviděl',
};

/* The grouping, then the stored pair — the certificate when one decided it, the
 * decision's name on a merge — and why it was not merged when the server says
 * (never for two adverts in one group). No stored row is said, not left blank:
 * the live stream keeps no machine reject (decision 7). A reason needs no stored
 * row: a `same` whose closure the invariants dissolved has one either way (E925). */
export function engineLine(
  row: Pick<RulingPairRow, 'engine_view' | 'zone' | 'score' | 'certificate' | 'decision' | 'why_not_merged'>,
): string {
  const why = row.why_not_merged ? ` — ${row.why_not_merged}` : '';
  let pair = 'pár bez uloženého řádku';
  if (row.zone) {
    pair = `pár: ${row.zone}${row.score != null ? ` ${row.score.toFixed(2)}` : ''}`;
    if (row.certificate) pair += ` · certifikát ${row.certificate}`;
    else if (row.zone === 'merge' && row.decision) pair += ` · ${row.decision}`;
  }
  return `${ENGINE_VIEW[row.engine_view]} · ${pair}${why}`;
}
