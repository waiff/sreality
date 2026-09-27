"""One review page per experiment: a seeded sample of the groups an arm changes, each with its
adverts' stated facts and links, its decided and labelled pairs, and two buttons. The page keeps
the operator's reads in the browser and exports them as JSONL: one group read per line, plus, for
a `same`, the pair-grain bodies of `POST /autodedup/verdict` (the one rulings ledger) that say it.
A `different` stays a group read: flagging a group does not forbid each of its pairs."""

from __future__ import annotations

import html
import json
import random
from typing import Any, Iterable

ZONES = ("undecided", "veto", "reject", "band", "merge")
FACTS = ("category_type", "category_main", "area_m2", "price", "disposition", "floor", "street")


def _ids(card: dict[str, Any]) -> frozenset[int]:
    return frozenset(m["id"] for m in card["members"])


def splits(review: dict[str, Any]) -> list[dict[str, Any]]:
    """The base groups the arm really breaks up: a base group wholly inside a group the arm forms
    was absorbed, and the formed group is the one question to ask about it."""
    formed = [_ids(card) for card in review["groups_gained"]]
    return [card for card in review["groups_lost"] if not any(_ids(card) <= f for f in formed)]


M45_N: int = 40
M45_SEED: int = 20260927


def draw(groups: Iterable[frozenset[int]], n: int, seed: int) -> list[frozenset[int]]:
    """`n` groups drawn with one seed, from an order that does not depend on how they were found:
    the one draw M4 / M5 count reads on and the page shows."""
    ordered = sorted(groups, key=lambda g: tuple(sorted(g)))
    return random.Random(seed).sample(ordered, min(n, len(ordered)))


def read_sample(review: dict[str, Any], n: int = M45_N, seed: int = M45_SEED
                ) -> list[dict[str, Any]]:
    """M4 and M5's cards (GLOBAL_SEARCH 2.2): `n` groups only the arm forms and `n` base groups it
    breaks up, each side drawn on its own with the pre-registered seed."""
    gained = {_ids(card): card for card in review["groups_gained"]}
    lost = {_ids(card): card for card in splits(review)}
    return ([dict(gained[g], side="gained") for g in draw(gained, n, seed)]
            + [dict(lost[g], side="lost") for g in draw(lost, n, seed)])


def sample(review: dict[str, Any], n: int, seed: int) -> list[dict[str, Any]]:
    """`n` changed groups drawn with one seed from the groups the arm forms and the ones it breaks
    up, the two sides in proportion to their sizes."""
    cards = ([dict(card, side="gained") for card in review["groups_gained"]]
             + [dict(card, side="lost") for card in splits(review)])
    rng = random.Random(seed)
    rng.shuffle(cards)
    return cards[:n] if n else cards


def _fmt(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}".replace(",", " ")
    return str(value)


def _card(card: dict[str, Any], k: int) -> str:
    ids = [m["id"] for m in card["members"]]
    rows = "".join(
        f"<tr><td><a href=\"{html.escape(m.get('source_url') or '#')}\" target=\"_blank\" "
        f"rel=\"noopener\">{html.escape(str(m['source']))}</a></td>"
        + "".join(f"<td>{html.escape(_fmt(m.get(f)))}</td>" for f in FACTS) + "</tr>"
        for m in card["members"])
    notes = []
    for p in card["pairs"]:
        labels = [f"operator {p['ruling']}"] if p.get("ruling") else []
        labels += [f"{n} {v}" for n, v in (p.get("judge") or {}).items() if v]
        zone = ZONES[p["zone"]] if p.get("zone") is not None else "not a candidate"
        if labels or zone == "merge":
            carrier = f"{p.get('rung') or ''} {p.get('name') or ''}".strip()
            notes.append(f"<li>{p['lo']}–{p['hi']}: {zone}"
                         f"{' by ' + html.escape(carrier) if carrier else ''}"
                         f"{' · ' + html.escape(', '.join(labels)) if labels else ''}</li>")
    side = "formed by the arm" if card["side"] == "gained" else "broken up by the arm"
    head = "".join(f"<th>{f.replace('_', ' ')}</th>" for f in FACTS)
    return (f"<section class=\"card\" data-key=\"{'-'.join(map(str, ids))}\" "
            f"data-side=\"{card['side']}\">"
            f"<header><span class=\"n\">{k}</span><b>{len(ids)} adverts</b>"
            f"<span class=\"side {card['side']}\">{side}</span></header>"
            f"<div class=\"scroll\"><table><thead><tr><th>portal</th>{head}</tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
            f"<details><summary>{len(notes)} decided or labelled pairs</summary>"
            f"<ul>{''.join(notes)}</ul></details>"
            f"<div class=\"act\"><button data-v=\"same\">One property</button>"
            f"<button data-v=\"different\">Not one property</button></div></section>")


def render(review: dict[str, Any], n: int = 60, seed: int = 1, m45: bool = False) -> str:
    cards = read_sample(review) if m45 else sample(review, n, seed)
    if m45:
        seed = M45_SEED
    broken = len(splits(review))
    meta = {"experiment": review["experiment"], "base": review["base"],
            "cohort": review["cohort"], "seed": seed,
            "changed": len(review["groups_gained"]) + broken,
            "absorbed": len(review["groups_lost"]) - broken}
    title = f"{review['experiment']} on {review['cohort']}"
    body = "".join(_card(card, k + 1) for k, card in enumerate(cards))
    return PAGE.replace("@@TITLE@@", html.escape(title)).replace(
        "@@META@@", json.dumps(meta)).replace("@@CARDS@@", body).replace(
        "@@SUB@@", html.escape(
            f"{len(cards)} of {meta['changed']} changed groups (seed {seed}) against "
            f"{review['base']}; {meta['absorbed']} base groups the arm only absorbed into a "
            f"larger one are asked about through that one. Is each group one property?"))


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Experiment review</title><style>
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e2e2dc;--acc:#2f6f4f;--neg:#9b3b2e;--chip:#eef0ea}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#ecece8;--muted:#a3a39c;--card:#1f1f1d;--line:#34342f;--acc:#7cc39b;--neg:#e0907f;--chip:#2a2c27}}
:root[data-theme="dark"]{--bg:#161615;--fg:#ecece8;--muted:#a3a39c;--card:#1f1f1d;--line:#34342f;--acc:#7cc39b;--neg:#e0907f;--chip:#2a2c27}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
.top{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.top h1{font-size:18px;margin:0}.sub{color:var(--muted);margin:8px 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0}
.card header{display:flex;gap:10px;align-items:center;margin-bottom:8px}
.n{color:var(--muted)}.side{font-size:12px;padding:2px 8px;border-radius:10px;background:var(--chip)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500}a{color:var(--acc)}
details{margin:8px 0;font-size:13px;color:var(--muted)}
.act{display:flex;gap:8px}button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.card[data-read="same"] button[data-v="same"]{background:var(--acc);color:var(--card);border-color:var(--acc)}
.card[data-read="different"] button[data-v="different"]{background:var(--neg);color:var(--card);border-color:var(--neg)}
</style></head><body><main>
<div class="top"><h1>@@TITLE@@</h1><span id="progress"></span><button id="export">Export reads (JSONL)</button></div>
<p class="sub">@@SUB@@</p>@@CARDS@@</main><script>
const META=@@META@@;const KEY="lab-review:"+META.experiment+":"+META.cohort;
let reads={};try{reads=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){reads={}}
const cards=[...document.querySelectorAll(".card")];
function save(){try{localStorage.setItem(KEY,JSON.stringify(reads))}catch(e){}}
function paint(){cards.forEach(c=>{const r=reads[c.dataset.key];if(r)c.dataset.read=r.verdict;else delete c.dataset.read});
document.getElementById("progress").textContent=Object.keys(reads).length+" / "+cards.length+" read"}
cards.forEach(c=>c.querySelectorAll("button").forEach(b=>b.addEventListener("click",()=>{
const k=c.dataset.key;if(reads[k]&&reads[k].verdict===b.dataset.v)delete reads[k];
else reads[k]={verdict:b.dataset.v,side:c.dataset.side,at:new Date().toISOString()};save();paint()})));
document.getElementById("export").addEventListener("click",()=>{
const lines=Object.entries(reads).map(([k,r])=>{const m=k.split("-").map(Number);
const pairs=r.verdict==="same"?m.slice(1).map(x=>({kind:"pair",verdict:"same",listing_lo:Math.min(m[0],x),listing_hi:Math.max(m[0],x)})):[];
return JSON.stringify({kind:"group_read",experiment:META.experiment,cohort:META.cohort,side:r.side,members:m,verdict:r.verdict,at:r.at,verdict_bodies:pairs})});
const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([lines.join("\\n")+"\\n"],{type:"application/x-ndjson"}));
a.download=META.experiment+"_"+META.cohort+"_reads.jsonl";a.click()});paint();
</script></body></html>"""
