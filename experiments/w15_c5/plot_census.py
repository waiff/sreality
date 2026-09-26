"""C5(a): is PLOT_TRUNCATING_SOURCES (features.py:122) still true of the exports? Share of plot values >= 1000 per (source, carrier)."""
import gzip, json, sys, collections
paths = sys.argv[1:]
c = collections.defaultdict(lambda: [0, 0])
for p in paths:
    with gzip.open(p, 'rt') as f:
        for line in f:
            if line.startswith('{"t": "image"'):
                break
            if not line.startswith('{"t": "listing"'):
                continue
            r = json.loads(line)
            a = r.get('attrs') or {}
            v, carrier = a.get('estate_area'), 'estate'
            if v is None and r.get('category_main') == 'pozemek':
                v, carrier = r.get('area_m2'), 'headline'
            if v is None:
                continue
            try:
                v = float(v)
            except Exception:
                continue
            if v < 10:
                continue
            k = (r['source'], carrier)
            c[k][0] += 1
            c[k][1] += v >= 1000
for k, (n, big) in sorted(c.items(), key=lambda x: -x[1][0]):
    print(f"{k[0]:>12} {k[1]:<8} n={n:5d} >=1000: {big:5d} ({big / n:.0%})")
