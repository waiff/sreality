"""The engine lab: every mechanic a registered rung, every experiment one JSON, every result one
leaderboard row — decided in seconds over a cohort's evidence cache (G3, w15 global search).

`cache.py` computes a cohort's pairs, features and per-rung signals ONCE with the engine's own
functions; `board.py` holds the rung and group registries and runs a ladder config over the cache;
`metrics.py` scores an arm against the operator rulings and the judged sample and keeps the
leaderboard. Nothing here writes to the database or decides a live merge."""
