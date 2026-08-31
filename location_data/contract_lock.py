"""The pre-merge half of contract immutability: the governed hash, pinned in git.

`project()` refuses a contract whose GOVERNED bytes moved under a version already on
record (02 §2.1.8) — correctly, and at the worst possible moment. The projection is one
transaction for the whole fleet, so the refusal is not scoped to the portal that drifted:
it takes the claim-intake lane down for all nine. That is what happened on 2026-08-27,
when PR #1209 rewrote two prose notes in `ceskereality.yaml` without bumping
`contract_version` and 14 consecutive hourly runs died on `ceskereality@3`.

Nothing in `test.yml` could have caught it. `test_contract_fixture_diff` gates CLAIMS and
says so in its own docstring ("prose and comments are free") — a note is hashed like a
selector but extracts nothing, so it passes that gate and moves the hash anyway. This
lockfile is the byte-level complement: `{source: {version, sha256}}` for every contract on
disk, recomputed and compared by `tests/location_data/test_contract_immutability.py`, so
an edit-without-a-bump fails on the push that writes it rather than in production an hour
later.

The hash is NOT recomputed here. `build_lock` calls `contracts.load_all`, which is the same
function the loader's `--check`/`--load` path calls, and reads `PortalContract.sha256` off
the object `project()` itself hashes and compares — one source of truth for both the
exclusion rule (`contract_body_hash`: minus `shadow:`, minus `persistence:`) and the
parse. A second implementation here could disagree with the loader, and the way it would
show that is a green CI run followed by the outage this file exists to prevent.

PyYAML needs no guard in this module: it never imports it. `contracts.parse_contract` does
the lazy dev/CI-only import, so this inherits that boundary unchanged.

CLI:
    python -m location_data.contract_lock --check   # exit 1 if the lockfile is stale
    python -m location_data.contract_lock --write   # regenerate after a reviewed bump
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from location_data.contracts import CONTRACT_DIR, load_all

LOCK_PATH = CONTRACT_DIR / "contracts.lock.json"

WRITE_COMMAND = "python -m location_data.contract_lock --write"


def build_lock(directory: Path = CONTRACT_DIR) -> dict[str, dict[str, Any]]:
    """`{source: {"version": N, "sha256": hex}}` for every contract under `directory`.

    `sha256` is `PortalContract.sha256` — the digest `project()` compares against
    `portal_contracts.contract_sha256` — rendered the way `project()` renders it (hex).
    """
    return {
        contract.source: {
            "version": contract.version,
            "sha256": contract.sha256.hex(),
        }
        for contract in sorted(load_all(directory), key=lambda c: c.source)
    }


def render(lock: dict[str, dict[str, Any]]) -> str:
    return json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def read_lock(path: Path = LOCK_PATH) -> dict[str, dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_lock(directory: Path = CONTRACT_DIR, path: Path = LOCK_PATH) -> Path:
    path.write_text(render(build_lock(directory)), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pin the governed contract hashes.")
    parser.add_argument("--dir", type=Path, default=CONTRACT_DIR)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--write", action="store_true",
                        help="Regenerate the lockfile from the contracts on disk.")
    parser.add_argument("--check", action="store_true",
                        help="Exit non-zero if the lockfile does not match disk.")
    args = parser.parse_args(argv)

    content = render(build_lock(args.dir))
    if args.check or not args.write:
        if not args.lock.exists():
            print(f"ERROR: {args.lock} is missing; run `{WRITE_COMMAND}`.",
                  file=sys.stderr)
            return 1
        if args.lock.read_text(encoding="utf-8") != content:
            print(f"ERROR: {args.lock} is stale. Either a contract's governed bytes "
                  f"moved without a `contract_version` bump (revert the edit), or the "
                  f"bump is reviewed and the lockfile has not caught up (run "
                  f"`{WRITE_COMMAND}` and commit it in the SAME commit).",
                  file=sys.stderr)
            return 1
        print(f"OK: {args.lock} matches the contracts on disk.")
        return 0

    args.lock.write_text(content, encoding="utf-8")
    print(f"Wrote {args.lock} ({len(json.loads(content))} contracts)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
