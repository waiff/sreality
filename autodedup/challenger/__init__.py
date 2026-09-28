"""The model-first challenger (GLOBAL_SEARCH 1.4): facts, one learned score, one constrained union.

LAB-ONLY until it replaces the merge-side modules: the lab's rungs `facts`, `mf_score` and the group
step `mf_union` call it, and no lane module imports it (tests/autodedup/test_challenger.py proves
that incremental_lane, reconcile and the harness import nothing from here). Stdlib only: this is the
code R2 would ship into a worker image that has no numpy and no scikit-learn."""
