"""C5(a): FALSE_BY_OMISSION (features.py:158) - per (source, slot) true/false/absent counts in the exports."""
import gzip, json, sys, collections
SLOTS = ("cellar", "terrace", "has_balcony", "has_lift", "has_parking", "garage")
c = collections.defaultdict(collections.Counter)
for p in sys.argv[1:]:
    with gzip.open(p, 'rt') as f:
        for line in f:
            if line.startswith('{"t": "image"'):
                break
            if not line.startswith('{"t": "listing"'):
                continue
            r = json.loads(line)
            a = r.get('attrs') or {}
            for s in SLOTS:
                v = a.get(s)
                c[(r['source'], s)]['absent' if v is None else str(bool(v))] += 1
for k in sorted(c):
    v = c[k]
    print(f"{k[0]:>12} {k[1]:<12} True={v['True']:6d} False={v['False']:6d} absent={v['absent']:6d}")
