"""S1-S9 of the location resolution pipeline (design 03).

The resolver is a PURE FUNCTION (03 §3.0):

    resolve(claims, ctx, resolver_version=…, registry_version_id=…,
            policy_version=…, collision_epoch_id=…) -> Resolution

Same inputs, byte-identical output. Nothing in this subpackage reads a wall clock
(`as_of` is `max(observed_at)` over the consumed claims), opens a socket, or draws a
random number — `tests/location_data/test_resolver_purity.py` enforces that by AST scan.

The only modules here that touch psycopg are the JOBS — `resolve_db`, `drain`,
`epoch_job` — which load rows, call the pure core, and write the results back. Everything
else is importable and runnable with no database at all, which is what makes the
deterministic replay gate (06 §6.4 W1) testable in the normal pytest job.
"""

from location_data.resolver.core import resolve
from location_data.resolver.version import (
    POLICY_VERSION_DEFAULT,
    RECONCILER_VERSION,
    RESOLVER_VERSION,
)

__all__ = [
    "POLICY_VERSION_DEFAULT",
    "RECONCILER_VERSION",
    "RESOLVER_VERSION",
    "resolve",
]
