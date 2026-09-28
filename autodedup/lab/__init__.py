"""The engine lab: every mechanic a registered rung, every experiment one JSON, every result one
leaderboard row — decided in seconds over a cohort's evidence (G3, w15 global search; B-a).

`cache.py` READS a cohort from the artefact `harness run --evidence` writes (`autodedup/evidence.py`:
the one path's pairs, features, decisions and per-rung engine signals) — the lab has no retrieval of
its own; `board.py` holds the rung and group registries and runs a ladder config over it;
`verify.py` proves the reference ladder reproduces the run row for row, group for group and rung
for rung; `metrics.py` scores an arm as GLOBAL_SEARCH 2.2's M1-M9 and keeps the leaderboard;
`rules.py` is rule 6.1 (a), keep and cut. Nothing here writes to the database or decides a live
merge."""
