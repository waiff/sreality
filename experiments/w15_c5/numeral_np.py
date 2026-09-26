"""C5(a/d): the engine's own floor grammar (normalize._FLOOR reads '3. NP' as 3) vs the ingest grammar
(scraper.floor.normalize_floor: '3. NP' = 2). How many g15 auto_reject:numeral_conflict pairs carry
K-C-grade photo evidence, and how many of their floor conflicts vanish under the ingest grammar?"""
import gzip, json, sys, re, collections
W = "/home/hejtm/dev/sreality/.claude/worktrees/w15-c5-substrate"
sys.path.insert(0, W)
from autodedup import normalize as N
from scraper.floor import normalize_floor
run = sys.argv[1]; cohort = sys.argv[2]
desc = {}
with gzip.open(cohort, 'rt') as f:
    for line in f:
        if line.startswith('{"t": "image"'):
            break
        if line.startswith('{"t": "listing"'):
            r = json.loads(line); desc[r['id']] = r.get('description') or ''
TOKEN = re.compile(r"(\d{1,2})\s*\.?\s*(?:np\b|n\.\s*p\.|nadzemni\w*\s+podlaz\w*|patr\w*|podlaz\w*)|\bprizemi\w*|\bsuteren\w*")
def ingest_floors(text):
    out = set()
    folded = N.fold(text)
    for m in TOKEN.finditer(folded):
        v = normalize_floor(m.group(0))
        if v is not None:
            out.add(v)
    return out
def engine_floors(text):
    return {v for u, v in N.numeric_facts(text) if u == 'floor'}
c = collections.Counter(); ex = []
with gzip.open(run + '/pairs.jsonl.gz', 'rt') as f:
    for line in f:
        r = json.loads(line)
        if r.get('reason') != 'auto_reject:numeral_conflict':
            continue
        c['numeral_conflict'] += 1
        feats = r['feats']
        tight = feats.get('phash_tight_matches', [0, False])[0]
        seq = feats.get('seq_monotone_ratio', [0, False])[0]
        kc = tight >= 4 and seq >= 0.8
        ea, eb = engine_floors(desc[r['lo']]), engine_floors(desc[r['hi']])
        fl_conf = bool(ea and eb and not (ea & eb))
        ia, ib = ingest_floors(desc[r['lo']]), ingest_floors(desc[r['hi']])
        ing_conf = bool(ia and ib and not (ia & ib))
        c[('engine_floor_conflict', fl_conf)] += 1
        if fl_conf:
            c[('ingest_grammar_still_conflicts', ing_conf)] += 1
            if kc:
                c[('KC_grade', 'ingest_conflict' if ing_conf else 'ingest_agrees')] += 1
                if not ing_conf and len(ex) < 12:
                    ex.append([r['lo'], r['hi'], sorted(ea), sorted(eb), sorted(ia), sorted(ib), tight, r.get('score')])
        if kc:
            c['KC_grade_total'] += 1
print(dict(c)); print(json.dumps(ex))
