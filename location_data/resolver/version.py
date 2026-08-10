"""The resolver's version constants — code constants, bumped per shipped rule.

`resolver_version` is one of the FIVE version inputs in the resolution's unique key
(01 §6.1 + 00 §10.3: claim_set_hash, resolver_version, registry_version_id,
policy_version, collision_epoch_id). Bumping it does not rewrite anything: it mints new
rows through the campaign runner and leaves the old ones intact (03 §3.14.2).

Bump `RESOLVER_VERSION` whenever a rule that can change an output changes — a new rung, a
different score, a changed cap, a changed normalization. `normalizer_version` is part of
`resolver_version` by construction (03 §3.3), so there is deliberately no second knob.

`RECONCILER_VERSION` is bumped once per shipped contradiction rule (03 §3.16); that is
routine, and it is exactly why dispositions key on the version-free `dedupe_key`
(00 §8.2) rather than on a detection id.
"""

from __future__ import annotations

# v1 = S1-S7 as specified in 03 §3.3-§3.9, first shipped implementation.
RESOLVER_VERSION = "resolver:v1"

# v1 = the cheap structural rule set of 03 §3.11.1 (no LLM, no geometry beyond distances).
RECONCILER_VERSION = "reconciler:v1"

# The policy rows seeded by migration 383. Passed in explicitly everywhere; this constant
# only names the default the jobs use when the operator does not choose one.
POLICY_VERSION_DEFAULT = "v1"
