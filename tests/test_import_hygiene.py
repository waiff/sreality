"""Import-time dependency hygiene guard.

The toolkit package must be importable under a *slim* install — psycopg +
requests only — without the optional cloud/vision wheels (boto3, Pillow,
anthropic, google-genai). Many scheduled workflows install just that slim set
and import one lightweight toolkit module (e.g. `toolkit.bazos_enrichment`);
the toolkit package __init__ must not drag a heavy, optional dependency in at
import time.

This pins the fix where `scraper.image_storage` imports boto3 lazily (inside
`R2Client.__init__`) rather than at module top. If anyone re-adds a top-level
`import boto3` to a module on the toolkit import path, this test fails loudly —
catching the exact regression that left `enrich_bazos.yml` broken for 3 weeks.

Run in a subprocess so a real, clean interpreter resolves the imports (the test
process itself has boto3 installed via the [dev,api,geo] CI extras).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

# Modules that a slim (psycopg + requests) install does NOT provide. Importing
# the toolkit package must not require any of these at import time.
_OPTIONAL_HEAVY = ("boto3", "botocore", "PIL", "anthropic")

_PROBE = textwrap.dedent(
    """
    import sys
    import importlib.abc

    BLOCKED = {blocked!r}

    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ModuleNotFoundError(f"No module named {{name!r}}")
            return None

    # Drop anything already cached, then make the heavy wheels look uninstalled.
    for _m in list(sys.modules):
        if _m.split(".")[0] in BLOCKED:
            del sys.modules[_m]
    sys.meta_path.insert(0, _Blocker())

    # Prove the blocker actually bites, so a future "boto3 always importable"
    # environment can't make this test vacuously pass.
    for _name in BLOCKED:
        try:
            __import__(_name)
        except ModuleNotFoundError:
            pass
        else:
            raise SystemExit(f"blocker failed: {{_name}} was importable")

    # The package __init__ runs first on ANY `from toolkit.X import ...`, so a
    # heavy top-level import anywhere in the eager closure would fail here.
    import toolkit  # noqa: F401
    from toolkit.bazos_enrichment import enrich_listing_description  # noqa: F401

    print("OK")
    """
)


def _run_probe(blocked: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _PROBE.format(blocked=blocked)],
        capture_output=True,
        text=True,
    )


def test_toolkit_imports_without_optional_heavy_wheels() -> None:
    result = _run_probe(_OPTIONAL_HEAVY)
    assert result.returncode == 0, (
        "Importing `toolkit` requires an optional heavy wheel at import time.\n"
        "A module on the toolkit import path top-level-imports one of "
        f"{_OPTIONAL_HEAVY} — make that import lazy (inside the function/method "
        "that uses it), like scraper.image_storage does for boto3.\n\n"
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_boto3_specifically_is_not_import_time() -> None:
    """The exact dependency that broke enrich_bazos for 3 weeks."""
    result = _run_probe(("boto3", "botocore"))
    assert result.returncode == 0, (
        "boto3 is imported at toolkit import time again — regression of the "
        "enrich_bazos outage.\n\n"
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
