/* Query helpers will live here as Parts B–E are built. Kept as a single
 * file initially per the prompt's scaffolding plan; will split if it grows
 * past ~300 lines. */

import { supabase } from './supabase';

export const ping = async (): Promise<{ ok: boolean; count: number | null }> => {
  const { count, error } = await supabase
    .from('listings_public')
    .select('*', { count: 'exact', head: true });
  return { ok: !error, count: count ?? null };
};
