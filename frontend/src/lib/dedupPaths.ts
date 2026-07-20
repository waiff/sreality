/* Dedup-eligibility pivot — turns the API's joint-distribution buckets into the
 * portal × property-type matrix on /location-audit, and turns any cell back into
 * the row filter that reproduces exactly the listings it counted.
 *
 * The DIVISION OF LABOUR matters. The server owns "is this listing eligible?" —
 * `elig_street` / `elig_geo` / `elig_byt_geo` are the dedup engine's own predicates
 * (toolkit.publication), so nothing here re-implements eligibility. This file only
 * derives the REASON: given a listing the engine cannot reach, which required input
 * is it missing? That is a pure function of the per-input booleans in each bucket.
 *
 * Consequence to respect: `elig_*` is THREE-VALUED. A NULL category_main makes both
 * category-gated arms NULL, so `!b.elig_geo` would count such a row as ineligible for
 * a pass whose domain it isn't even in. Always test `=== true`, never `!`.
 */

import type { DedupPathKey, EligibilityBucket } from './api';

export type { DedupPathKey };

/** Matrix scope: which listings the whole table is computed over. */
export type MatrixScope = 'active' | 'all';

/** A single required input of a pass. Each is one boolean on the bucket AND one
 *  reproducible row filter, which is what makes every cell drillable. */
export interface PathInput {
  key: string;
  /** Cell-sized label (fits a narrow column). */
  label: string;
  /** Tooltip — what the input is and why its absence blocks the pass. */
  tip: string;
  ok: (b: EligibilityBucket) => boolean;
  /** Presence-chip key on the row filter below; null = expressed via the state tab. */
  presenceKey: string | null;
}

const INPUT: Record<string, PathInput> = {
  street: {
    key: 'street',
    label: 'bez ulice',
    tip: 'street je prázdná — bez ní neexistuje blokovací klíč ulice+dispozice, takže se listing s nikým neporovná touto cestou.',
    ok: (b) => b.has_street,
    presenceKey: 'street',
  },
  disposition: {
    key: 'disposition',
    label: 'bez dispozice',
    tip: 'disposition je NULL. U bytu je to povinný rozlišovač (jeden dům = mnoho jednotek na jedné souřadnici). Jednodomové rodiny (dům/pozemek/komerce) ji nemají téměř nikdy — proto pro ně existuje geo cesta.',
    ok: (b) => b.has_disposition,
    presenceKey: 'disposition',
  },
  geom: {
    key: 'geom',
    label: 'bez souřadnic',
    tip: 'geom je NULL — portál nedodal pin a geokódování neproběhlo nebo selhalo. Bez souřadnice nevznikne geo_cell_key ani admin hierarchie.',
    ok: (b) => b.has_geom,
    presenceKey: 'geom',
  },
  obec: {
    key: 'obec',
    label: 'bez obce',
    tip: 'obec_id je NULL, přestože souřadnice existuje. obec_id se odvozuje PIP dotazem souřadnice do admin_boundaries — prázdné tedy znamená, že pin leží MIMO české administrativní hranice (typicky zahraniční nabídky). Obě geo cesty obec_id vyžadují.',
    ok: (b) => b.has_obec,
    presenceKey: 'obec_id',
  },
  area: {
    key: 'area',
    label: 'bez plochy',
    tip: 'coalesce(area_m2, estate_area, usable_area) je NULL — geo cesty bez plochy nemají čím omezit shodu, tak listing vůbec nenačtou.',
    ok: (b) => b.has_area,
    presenceKey: 'area',
  },
  active: {
    key: 'active',
    label: 'neaktivní',
    tip: 'Geo cesty jsou z principu jen pro aktivní listingy. Neaktivní řádek zůstává dosažitelný pouze cestou ulice+dispozice (ta gate na aktivitu nemá — historii ceny je třeba dopočítat i po stažení).',
    ok: (b) => b.is_active,
    presenceKey: null,
  },
};

export interface PathSpec {
  key: DedupPathKey;
  /** Tab label. */
  label: string;
  /** What the pass is and which listings it is meant to reach. */
  explain: string;
  /** Categories the pass covers; null = every category (the street pass has no gate). */
  domain: string[] | null;
  activeOnly: boolean;
  /** Required inputs, in display order. */
  inputs: PathInput[];
}

/* The three passes, mirroring toolkit.publication's predicates. `domain` + `activeOnly`
 * are overwritten from the API's `paths` payload at pivot time (the server derives them
 * from the same constants it renders into SQL) — the values here are the fallback that
 * keeps the module usable in tests without a fetch. */
export const PATHS: PathSpec[] = [
  {
    key: 'street',
    label: 'Ulice + dispozice',
    explain:
      'Původní cesta enginu: listingy se blokují podle ulice a dispozice. Nemá kategorijní gate — engine načte cokoliv, co má obojí — ale dispozici v praxi nese jen byt, takže je to de facto bytová cesta. Jako jediná platí i pro neaktivní listingy.',
    domain: null,
    activeOnly: false,
    inputs: [INPUT.street, INPUT.disposition],
  },
  {
    key: 'geo',
    label: 'Geo + plocha',
    explain:
      'Cesta pro jednodomé rodiny (dům, pozemek, komerce, ostatní), které dispozici nemají: blokuje se podle geo buňky ze souřadnic, omezeno plochou. Vyžaduje souřadnici, obec_id i plochu — a jen aktivní listingy.',
    domain: ['dum', 'pozemek', 'komercni', 'ostatni'],
    activeOnly: true,
    inputs: [INPUT.geom, INPUT.obec, INPUT.area],
  },
  {
    key: 'byt_geo',
    label: 'Byt-geo (byt bez ulice)',
    explain:
      'Záchranná příčka pro byty, u kterých portál nedodá ulici: geo buňka + plocha, ale dispozice zůstává povinná (buňka se při načtení shleduje podle třídy dispozice). Jen aktivní listingy.',
    domain: ['byt'],
    activeOnly: true,
    inputs: [INPUT.geom, INPUT.obec, INPUT.area, INPUT.disposition],
  },
];

export const PATH_BY_KEY: Record<DedupPathKey, PathSpec> = Object.fromEntries(
  PATHS.map((p) => [p.key, p]),
) as Record<DedupPathKey, PathSpec>;

/* Which passes can reach a category at all, in "intended path first" order. Used ONLY
 * by the summary view, to attribute an unreachable listing to its cheapest fix: a
 * pozemek missing a disposition is not a disposition problem (it will never have one) —
 * its real gap is whatever the geo pass is missing, so geo ranks first for that family. */
export function applicablePaths(category: string | null): DedupPathKey[] {
  if (category === 'byt') return ['street', 'byt_geo'];
  if (category && PATH_BY_KEY.geo.domain?.includes(category)) return ['geo', 'street'];
  return ['street'];
}

/** The reason a bucket falls out of a pass: which of its required inputs are absent. */
export function missingInputs(spec: PathSpec, b: EligibilityBucket, scope: MatrixScope): string[] {
  return inputsInScope(spec, scope)
    .filter((i) => !i.ok(b))
    .map((i) => i.key);
}

/* An active-only pass gains `active` as a required input only when the matrix itself
 * includes inactive listings — under scope='active' it is satisfied by construction and
 * would just be a dead row in every cell. */
export function inputsInScope(spec: PathSpec, scope: MatrixScope): PathInput[] {
  return spec.activeOnly && scope === 'all' ? [...spec.inputs, INPUT.active] : spec.inputs;
}

export interface Cell {
  scope: number;
  eligible: number;
  ineligible: number;
  /** Exactly ONE required input missing, keyed by input key. */
  reasons: Record<string, number>;
  /** Two or more missing at once — one fix would not be enough. */
  multi: number;
  /** Ineligible with nothing missing: impossible unless this file's input list has
   *  drifted from the server predicate. Surfaced, never folded away. */
  unexplained: number;
}

const emptyCell = (): Cell => ({
  scope: 0,
  eligible: 0,
  ineligible: 0,
  reasons: {},
  multi: 0,
  unexplained: 0,
});

function inDomain(domain: string[] | null, category: string | null): boolean {
  return domain === null ? true : category !== null && domain.includes(category);
}

function eligOf(b: EligibilityBucket, key: DedupPathKey): boolean {
  // `=== true` on purpose: the category-gated arms are NULL for a NULL category_main.
  if (key === 'street') return b.elig_street === true;
  if (key === 'geo') return b.elig_geo === true;
  return b.elig_byt_geo === true;
}

function addTo(cell: Cell, b: EligibilityBucket, missing: string[], eligible: boolean): void {
  cell.scope += b.n;
  if (eligible) {
    cell.eligible += b.n;
    return;
  }
  cell.ineligible += b.n;
  if (missing.length === 1) cell.reasons[missing[0]] = (cell.reasons[missing[0]] ?? 0) + b.n;
  else if (missing.length > 1) cell.multi += b.n;
  else cell.unexplained += b.n;
}

export interface MatrixView {
  /** cells[category][source]; the '' key on either axis is that axis's total. */
  cells: Record<string, Record<string, Cell>>;
  categories: string[];
  sources: string[];
  /** The inputs this view breaks ineligibility down by (scope-dependent). */
  inputs: PathInput[];
}

/* Pivot the buckets for ONE pass. Rows = categories in the pass's domain, columns =
 * portals; '' on either axis accumulates that axis's total, so a total is summed from
 * the same buckets as its cells rather than added up from rounded parts. */
export function pivotPath(
  buckets: EligibilityBucket[],
  spec: PathSpec,
  scope: MatrixScope,
): MatrixView {
  const cells: Record<string, Record<string, Cell>> = {};
  const categories = new Set<string>();
  const sources = new Set<string>();
  const inputs = inputsInScope(spec, scope);

  const put = (cat: string, src: string, b: EligibilityBucket, miss: string[], ok: boolean) => {
    cells[cat] ??= {};
    cells[cat][src] ??= emptyCell();
    addTo(cells[cat][src], b, miss, ok);
  };

  for (const b of buckets) {
    if (scope === 'active' && !b.is_active) continue;
    if (!inDomain(spec.domain, b.category_main)) continue;
    const cat = b.category_main ?? '(bez typu)';
    const missing = inputs.filter((i) => !i.ok(b)).map((i) => i.key);
    const ok = eligOf(b, spec.key);
    categories.add(cat);
    sources.add(b.source);
    put(cat, b.source, b, missing, ok);
    put(cat, '', b, missing, ok);
    put('', b.source, b, missing, ok);
    put('', '', b, missing, ok);
  }

  return {
    cells,
    categories: [...categories].sort(),
    sources: [...sources].sort(),
    inputs,
  };
}

/* Pivot the BOTTOM LINE: listings no pass can reach. A row is unreachable only if every
 * arm says so, and its reason is attributed to the pass that came CLOSEST — the fewest
 * missing inputs, ties going to the family's intended pass. That answers the question
 * the operator actually has ("what one field would unlock these?"), which a per-pass
 * view cannot: a pozemek fails the street pass by definition, and saying so is noise. */
export function pivotSummary(buckets: EligibilityBucket[], scope: MatrixScope): MatrixView {
  const cells: Record<string, Record<string, Cell>> = {};
  const categories = new Set<string>();
  const sources = new Set<string>();
  const seen = new Set<string>();

  const put = (cat: string, src: string, b: EligibilityBucket, miss: string[], ok: boolean) => {
    cells[cat] ??= {};
    cells[cat][src] ??= emptyCell();
    addTo(cells[cat][src], b, miss, ok);
  };

  for (const b of buckets) {
    if (scope === 'active' && !b.is_active) continue;
    const cat = b.category_main ?? '(bez typu)';
    const reachable =
      eligOf(b, 'street') || eligOf(b, 'geo') || eligOf(b, 'byt_geo');

    let missing: string[] = [];
    if (!reachable) {
      // Cheapest fix across the passes that could ever apply to this category.
      let best: string[] | null = null;
      for (const key of applicablePaths(b.category_main)) {
        const m = missingInputs(PATH_BY_KEY[key], b, scope);
        if (best === null || m.length < best.length) best = m;
      }
      missing = best ?? [];
      missing.forEach((k) => seen.add(k));
    }
    categories.add(cat);
    sources.add(b.source);
    put(cat, b.source, b, missing, reachable);
    put(cat, '', b, missing, reachable);
    put('', b.source, b, missing, reachable);
    put('', '', b, missing, reachable);
  }

  return {
    cells,
    categories: [...categories].sort(),
    sources: [...sources].sort(),
    // Only the inputs that actually explain something here — the summary spans passes
    // with different requirements, so a fixed list would show permanent zeros.
    inputs: Object.values(INPUT).filter((i) => seen.has(i.key)),
  };
}

/* ------------------------------------------------------------------ */
/* Cell -> row filter. Every number in the matrix is a link into the list below, and
 * these are the params that reproduce EXACTLY the listings that number counted. */

export interface CellFilter {
  source?: string;
  category_main?: string;
  active?: 'active' | 'inactive';
  dedup?: 'reachable' | 'unreachable';
  path?: DedupPathKey;
  path_state?: 'eligible' | 'ineligible';
  has: string[];
  missing: string[];
}

export type CellPart = 'scope' | 'eligible' | 'ineligible' | { reason: string } | 'multi';

export function cellFilter(
  spec: PathSpec | null,
  scope: MatrixScope,
  category: string,
  source: string,
  part: CellPart,
): CellFilter {
  const f: CellFilter = { has: [], missing: [] };
  if (source) f.source = source;
  // '(bez typu)' is the display stand-in for a NULL category_main, which no
  // category_main= value can express — leave the type filter open for it.
  if (category && category !== '(bez typu)') f.category_main = category;
  if (scope === 'active') f.active = 'active';

  if (spec === null) {
    // Summary view: reachability is the whole-engine verdict, not one pass's, so it
    // filters on `dedup` instead of a single pass's state. Note the reason drill-down
    // is a SUPERSET, not an identity: the count attributes each row to its closest
    // pass, which no row filter can express — "unreachable AND missing X" is the
    // honest, reproducible neighbourhood of it (see summaryDrillIsExact).
    if (part === 'eligible') f.dedup = 'reachable';
    else if (part !== 'scope') f.dedup = 'unreachable';
    if (typeof part === 'object') {
      const input = INPUT[part.reason];
      if (input?.presenceKey) f.missing.push(input.presenceKey);
      else if (part.reason === 'active') f.active = 'inactive';
    }
    return f;
  }

  f.path = spec.key;
  if (part === 'scope') return f;
  if (part === 'eligible') {
    f.path_state = 'eligible';
    return f;
  }
  f.path_state = 'ineligible';
  if (part === 'ineligible' || part === 'multi') return f;

  // A single-reason cell: that input absent, every OTHER required input present —
  // which is exactly the bucket the number was summed from.
  const inputs = inputsInScope(spec, scope);
  for (const i of inputs) {
    const missing = i.key === part.reason;
    if (i.presenceKey === null) {
      f.active = missing ? 'inactive' : 'active';
    } else if (missing) {
      f.missing.push(i.presenceKey);
    } else {
      f.has.push(i.presenceKey);
    }
  }
  return f;
}

/* Does clicking this part land on EXACTLY the listings it counted? True everywhere on a
 * per-pass tab. False for the summary tab's reason rows only, where the count attributes
 * each row to its closest pass — a judgement no WHERE clause can restate. The UI says so
 * rather than implying an identity it does not have. */
export function drillIsExact(spec: PathSpec | null, part: CellPart): boolean {
  if (spec !== null) return true;
  return typeof part !== 'object' && part !== 'multi';
}
