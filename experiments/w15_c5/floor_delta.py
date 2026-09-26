"""C5: floor delta between K-C / K-B certificate pairs (floor-blind proofs) per portal pair, per export."""
import gzip, json, sys, collections
cohort, pairs = sys.argv[1], sys.argv[2]
L = {}
with gzip.open(cohort, 'rt') as f:
    for line in f:
        r = json.loads(line)
        if r.get('t') == 'listing':
            L[r['id']] = (r['source'], r.get('floor'), r.get('total_floors'), r.get('category_main'))
d = collections.defaultdict(collections.Counter)
cert = collections.Counter()
with gzip.open(pairs, 'rt') as f:
    for line in f:
        r = json.loads(line)
        c = r.get('certificate')
        if c not in ('K-C',):
            continue
        a, b = L.get(r['lo']), L.get(r['hi'])
        if not a or not b or a[3] != 'byt' or b[3] != 'byt' or a[1] is None or b[1] is None:
            continue
        (sa, fa), (sb, fb) = sorted([(a[0], a[1]), (b[0], b[1])])
        d[(sa, sb)][fa - fb] += 1
        cert[c] += 1
print(cert)
for k in sorted(d, key=lambda k: -sum(d[k].values())):
    c = d[k]; n = sum(c.values())
    if n < 8: continue
    mean = sum(x * y for x, y in c.items()) / n
    print(f"{k[0]:>12}-{k[1]:<12} n={n:5d} mean={mean:+.3f} at0={c[0]/n:.2f} at+1={c[1]/n:.2f} at-1={c[-1]/n:.2f} other={1-(c[0]+c[1]+c[-1])/n:.2f}")
