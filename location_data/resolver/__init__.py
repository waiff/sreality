"""The location resolver: BIND -> FILL -> GRADE -> CHECK.

    resolve(claims, ctx, resolver_version=…, registry_version=…) -> Resolution

The resolver is a PURE FUNCTION: same inputs, byte-identical output. Nothing in this
subpackage reads a wall clock, opens a socket or draws a random number —
`tests/location_data/test_resolver_purity.py` enforces that by AST scan.

The only modules here that touch psycopg are the JOBS — `resolve_db` and `drain` — which
load rows, call the pure core, and write the one answer row back. Everything else is
importable and runnable with no database at all.
"""

from location_data.resolver.core import resolve
from location_data.resolver.version import RESOLVER_VERSION

__all__ = ["RESOLVER_VERSION", "resolve"]
