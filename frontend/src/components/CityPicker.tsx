/* City picker for a price-stats dataset: a kraj→okres→obec tree with cascading
 * checkboxes (check a kraj → all its eligible obce) + indeterminate states, and
 * min/max population bounds that constrain which obce are eligible. Modal over
 * the dataset form. Returns the selected obec_ids + the population bounds.
 * Civic-archive tokens. Only obce with a sreality_id are offered (scrapeable). */
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchObecTree, priceStatsKeys, type ObecNode } from '@/lib/priceStats';

const SELECT_CLS =
  'text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-3)] px-2 py-1 text-[var(--color-ink)] w-24';

interface Built {
  kraje: ObecNode[];
  okresyByKraj: Map<number, ObecNode[]>;
  obceByOkres: Map<number, ObecNode[]>;
  directObceByKraj: Map<number, ObecNode[]>; // obce hanging straight off a kraj (Praha)
  obceByKraj: Map<number, ObecNode[]>;       // all obce under a kraj (cascade target)
  obceById: Map<number, ObecNode>;
}

function pushTo(map: Map<number, ObecNode[]>, key: number, val: ObecNode): void {
  const arr = map.get(key);
  if (arr) arr.push(val); else map.set(key, [val]);
}

function buildTree(nodes: ObecNode[]): Built {
  const kraje = nodes.filter((n) => n.level === 'kraj').sort((a, b) => a.name.localeCompare(b.name, 'cs'));
  const okresy = nodes.filter((n) => n.level === 'okres');
  const obce = nodes.filter((n) => n.level === 'obec');
  const okresyByKraj = new Map<number, ObecNode[]>();
  for (const o of okresy) {
    if (o.parent_id != null) pushTo(okresyByKraj, o.parent_id, o);
  }
  const krajIds = new Set(kraje.map((k) => k.id));
  const okresIds = new Set(okresy.map((o) => o.id));
  const obceByOkres = new Map<number, ObecNode[]>();
  const directObceByKraj = new Map<number, ObecNode[]>();
  const obceById = new Map<number, ObecNode>();
  for (const o of obce) {
    obceById.set(o.id, o);
    if (o.parent_id == null) continue;
    if (okresIds.has(o.parent_id)) pushTo(obceByOkres, o.parent_id, o);
    else if (krajIds.has(o.parent_id)) pushTo(directObceByKraj, o.parent_id, o); // Praha
  }
  // obce per kraj = union over its okresy + any obce hanging straight off it.
  const obceByKraj = new Map<number, ObecNode[]>();
  for (const k of kraje) {
    const list: ObecNode[] = [...(directObceByKraj.get(k.id) ?? [])];
    for (const ok of okresyByKraj.get(k.id) ?? []) list.push(...(obceByOkres.get(ok.id) ?? []));
    obceByKraj.set(k.id, list);
  }
  for (const m of [okresyByKraj, obceByOkres, directObceByKraj, obceByKraj].flatMap((x) => [...x.values()])) {
    m.sort((a, b) => a.name.localeCompare(b.name, 'cs'));
  }
  return { kraje, okresyByKraj, obceByOkres, directObceByKraj, obceByKraj, obceById };
}

const eligible = (o: ObecNode, min: number | null, max: number | null): boolean => {
  if (o.population == null) return min == null;       // unknown pop only passes an open lower bound
  return (min == null || o.population >= min) && (max == null || o.population <= max);
};

type Tri = 'on' | 'off' | 'part';

function groupState(obce: ObecNode[], selected: Set<number>, min: number | null, max: number | null): Tri {
  const elig = obce.filter((o) => eligible(o, min, max));
  if (!elig.length) return 'off';
  const on = elig.filter((o) => selected.has(o.id)).length;
  return on === 0 ? 'off' : on === elig.length ? 'on' : 'part';
}

interface Props {
  initialObecIds: number[];
  initialMin: number | null;
  initialMax: number | null;
  onClose: () => void;
  onApply: (obecIds: number[], min: number | null, max: number | null) => void;
}

export default function CityPicker({ initialObecIds, initialMin, initialMax, onClose, onApply }: Props) {
  const treeQ = useQuery({ queryKey: priceStatsKeys.obecTree, queryFn: fetchObecTree, staleTime: Infinity });
  const built = useMemo(() => (treeQ.data ? buildTree(treeQ.data) : null), [treeQ.data]);

  const [selected, setSelected] = useState<Set<number>>(() => new Set(initialObecIds));
  const [min, setMin] = useState<string>(initialMin != null ? String(initialMin) : '');
  const [max, setMax] = useState<string>(initialMax != null ? String(initialMax) : '');
  const [openKraj, setOpenKraj] = useState<Set<number>>(new Set());
  const [openOkres, setOpenOkres] = useState<Set<number>>(new Set());

  const minN = min ? Number(min) : null;
  const maxN = max ? Number(max) : null;

  const toggleGroup = (obce: ObecNode[], on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      for (const o of obce) {
        if (!eligible(o, minN, maxN)) { next.delete(o.id); continue; }
        if (on) next.add(o.id); else next.delete(o.id);
      }
      return next;
    });

  const toggleOne = (o: ObecNode) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(o.id)) next.delete(o.id); else if (eligible(o, minN, maxN)) next.add(o.id);
      return next;
    });

  const selectAllInRange = () => {
    if (!built) return;
    setSelected(new Set([...built.obceById.values()].filter((o) => eligible(o, minN, maxN)).map((o) => o.id)));
  };
  const clearAll = () => setSelected(new Set());

  // Drop now-ineligible obce when bounds tighten.
  const applyBounds = (nextMin: string, nextMax: string) => {
    const lo = nextMin ? Number(nextMin) : null;
    const hi = nextMax ? Number(nextMax) : null;
    setSelected((prev) => {
      if (!built) return prev;
      const next = new Set<number>();
      for (const id of prev) {
        const o = built.obceById.get(id);
        if (o && eligible(o, lo, hi)) next.add(id);
      }
      return next;
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[var(--color-ink)]/30 p-4" onClick={onClose}>
      <div className="w-full max-w-2xl max-h-[85vh] flex flex-col bg-[var(--color-paper)] border border-[var(--color-rule-strong)] rounded-[var(--radius-lg)]"
        onClick={(e) => e.stopPropagation()}>
        <div className="px-5 py-3 border-b border-[var(--color-rule)] flex items-center justify-between">
          <h2 className="font-[family-name:var(--font-display)] text-lg">Select municipalities</h2>
          <span className="text-sm tabular-nums text-[var(--color-copper)]">{selected.size} selected</span>
        </div>

        <div className="px-5 py-3 border-b border-[var(--color-rule)] flex flex-wrap items-center gap-3 text-sm">
          <span className="text-xs uppercase tracking-[0.14em] text-[var(--color-ink-3)]">Population</span>
          <label className="flex items-center gap-1 text-[var(--color-ink-2)]">min
            <input type="number" min={0} value={min}
              onChange={(e) => { setMin(e.target.value); applyBounds(e.target.value, max); }} className={SELECT_CLS} /></label>
          <label className="flex items-center gap-1 text-[var(--color-ink-2)]">max
            <input type="number" min={0} value={max}
              onChange={(e) => { setMax(e.target.value); applyBounds(min, e.target.value); }} className={SELECT_CLS} /></label>
          <div className="ml-auto flex gap-3">
            <button onClick={selectAllInRange} className="text-[var(--color-copper)] hover:text-[var(--color-copper-2)]">Select all in range</button>
            <button onClick={clearAll} className="text-[var(--color-ink-3)] hover:text-[var(--color-ink)]">Clear</button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2 text-sm">
          {treeQ.isLoading || !built ? (
            <p className="px-3 py-6 text-[var(--color-ink-3)]">Loading municipalities…</p>
          ) : (
            built.kraje.map((k) => {
              const kObce = built.obceByKraj.get(k.id) ?? [];
              const kState = groupState(kObce, selected, minN, maxN);
              const isOpen = openKraj.has(k.id);
              return (
                <div key={k.id}>
                  <Row depth={0} state={kState}
                    onToggle={() => toggleGroup(kObce, kState !== 'on')}
                    onExpand={() => setOpenKraj((s) => toggleSet(s, k.id))}
                    expanded={isOpen} label={k.name}
                    meta={`${kObce.filter((o) => eligible(o, minN, maxN)).length} obcí`} />
                  {isOpen && (built.okresyByKraj.get(k.id) ?? []).map((ok) => {
                    const oObce = built.obceByOkres.get(ok.id) ?? [];
                    const oState = groupState(oObce, selected, minN, maxN);
                    const okOpen = openOkres.has(ok.id);
                    return (
                      <div key={ok.id}>
                        <Row depth={1} state={oState}
                          onToggle={() => toggleGroup(oObce, oState !== 'on')}
                          onExpand={() => setOpenOkres((s) => toggleSet(s, ok.id))}
                          expanded={okOpen} label={ok.name}
                          meta={`${oObce.filter((o) => eligible(o, minN, maxN)).length}`} />
                        {okOpen && oObce.map((o) => {
                          const elig = eligible(o, minN, maxN);
                          return (
                            <Row key={o.id} depth={2}
                              state={selected.has(o.id) ? 'on' : 'off'}
                              disabled={!elig}
                              onToggle={() => toggleOne(o)}
                              label={o.name}
                              meta={o.population != null ? o.population.toLocaleString('cs-CZ') : '—'} />
                          );
                        })}
                      </div>
                    );
                  })}
                  {isOpen && (built.directObceByKraj.get(k.id) ?? []).map((o) => {
                    const elig = eligible(o, minN, maxN);
                    return (
                      <Row key={o.id} depth={1}
                        state={selected.has(o.id) ? 'on' : 'off'}
                        disabled={!elig}
                        onToggle={() => toggleOne(o)}
                        label={o.name}
                        meta={o.population != null ? o.population.toLocaleString('cs-CZ') : '—'} />
                    );
                  })}
                </div>
              );
            })
          )}
        </div>

        <div className="px-5 py-3 border-t border-[var(--color-rule)] flex justify-end gap-3">
          <button onClick={onClose} className="text-sm text-[var(--color-ink-3)] hover:text-[var(--color-ink)]">Cancel</button>
          <button onClick={() => onApply([...selected], minN, maxN)}
            className="text-sm rounded-[var(--radius-sm)] px-3 py-1.5 border border-[var(--color-copper)] text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)]">
            Use {selected.size} municipalities
          </button>
        </div>
      </div>
    </div>
  );
}

function toggleSet(s: Set<number>, id: number): Set<number> {
  const next = new Set(s);
  if (next.has(id)) next.delete(id); else next.add(id);
  return next;
}

function Row({
  depth, state, label, meta, expanded, onToggle, onExpand, disabled,
}: {
  depth: number;
  state: Tri;
  label: string;
  meta?: string;
  expanded?: boolean;
  onToggle: () => void;
  onExpand?: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-center gap-2 py-1 pr-2 rounded-[var(--radius-xs)] hover:bg-[var(--color-paper-2)]"
      style={{ paddingLeft: 8 + depth * 18 }}>
      {onExpand ? (
        <button onClick={onExpand} className="w-4 text-[var(--color-ink-3)] hover:text-[var(--color-ink)] text-xs">
          {expanded ? '▾' : '▸'}
        </button>
      ) : <span className="w-4" />}
      <button onClick={disabled ? undefined : onToggle} disabled={disabled}
        className={`flex items-center gap-2 flex-1 text-left ${disabled ? 'text-[var(--color-ink-4)]' : 'text-[var(--color-ink-2)] hover:text-[var(--color-ink)]'}`}>
        <Checkbox state={state} disabled={disabled} />
        <span className={depth === 0 ? 'font-[family-name:var(--font-sans)]' : ''}>{label}</span>
        {meta && <span className="ml-auto text-xs tabular-nums text-[var(--color-ink-3)]">{meta}</span>}
      </button>
    </div>
  );
}

function Checkbox({ state, disabled }: { state: Tri; disabled?: boolean }) {
  const border = disabled ? 'border-[var(--color-rule)]' : 'border-[var(--color-rule-strong)]';
  return (
    <span className={`inline-flex items-center justify-center w-3.5 h-3.5 rounded-[2px] border ${border} ${state !== 'off' ? 'bg-[var(--color-copper)] border-[var(--color-copper)]' : ''}`}>
      {state === 'on' && <span className="text-[var(--color-paper)] text-[0.6rem] leading-none">✓</span>}
      {state === 'part' && <span className="w-2 h-[2px] bg-[var(--color-paper)]" />}
    </span>
  );
}
