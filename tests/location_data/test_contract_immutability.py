"""The governed contract bytes are pinned in git, so a drift fails HERE, not in production.

02 §2.1.8: a contract's entries are immutable per `contract_version`, and `project()`
enforces it — it refuses a version already on record whose governed hash moved. The
enforcement is correct and the timing is the problem: the projection is one transaction
for the whole fleet, so the refusal is not scoped to the portal that drifted. It takes the
claim-intake lane down for all nine.

That is not hypothetical. PR #1209 (9929efcf, merged 2026-08-27) rewrote two pieces of
PROSE in `ceskereality.yaml` — `fetch.robots_note` and one `extractions[].notes` — without
bumping `contract_version`. `contract_body_hash` excludes `persistence:` and `shadow:` and
NOTHING else, so a note is hashed exactly like a selector: v3's hash moved from 45db104c…
to 780cd716… and every hourly intake run from 22:32Z died on it. 14 consecutive failures,
all nine portals, over a sentence.

`test_contract_fixture_diff` cannot see this by construction — it gates CLAIMS and says so
("prose and comments are free"). Prose changes no claim, so #1209 passed a green CI and
took the lane down anyway. This is the byte-level complement: the same digest `project()`
compares, recorded in `contracts/portals/contracts.lock.json` and checked on every push.

Both legal moves stay open, and the failure message names them:
  - the edit is reviewed → bump `contract_version` AND regenerate the lockfile, in the
    SAME commit (a bumped yaml with a stale lockfile fails too, on the version assert);
  - the edit was unintended → revert it.

Regenerate: `python -m location_data.contract_lock --write`. Regenerating to make this
suite green is only ever half a fix — without the bump it hands the identical refusal to
`project()`, which reads the DB and cannot be argued with from a lockfile.
"""

from __future__ import annotations

import pytest

from location_data import contract_lock, contracts

_CONTRACTS = {c.source: c for c in contracts.load_all()}
_SOURCES = sorted(_CONTRACTS)
_LOCK = contract_lock.read_lock()

_REMEDY = (
    "Two legal moves, and only two:\n"
    "  1. The edit is intended — bump `contract_version` in the yaml AND run\n"
    f"     `{contract_lock.WRITE_COMMAND}`, committing both in the SAME commit.\n"
    "  2. The edit is not intended — revert it.\n"
    "Updating the lockfile alone is NOT move 1: it makes this suite green and leaves\n"
    "`project()` refusing the same pair at deploy time, which is the failure mode that\n"
    "killed 14 consecutive claim-intake runs on 2026-08-27 (all nine portals)."
)


def test_the_lockfile_covers_exactly_the_contracts_on_disk() -> None:
    missing = [s for s in _SOURCES if s not in _LOCK]
    extra = [s for s in _LOCK if s not in _CONTRACTS]
    assert not missing and not extra, (
        f"{contract_lock.LOCK_PATH.name} is out of step with contracts/portals/: "
        f"missing {missing or 'nothing'}, stale entries {extra or 'none'}.\n{_REMEDY}")


@pytest.mark.parametrize("source", _SOURCES)
def test_the_governed_bytes_match_the_locked_hash(source: str) -> None:
    contract = _CONTRACTS[source]
    entry = _LOCK.get(source)
    if entry is None:
        pytest.fail(f"{source} has no entry in {contract_lock.LOCK_PATH.name}.\n{_REMEDY}",
                    pytrace=False)

    # The version first: a bumped yaml against a stale lockfile would otherwise read as a
    # hash drift, and the operator would be told to bump something already bumped.
    if int(entry["version"]) != contract.version:
        pytest.fail(
            f"contracts/portals/{source}.yaml is at contract_version {contract.version}, "
            f"but {contract_lock.LOCK_PATH.name} still pins version "
            f"{entry['version']}.\n{_REMEDY}", pytrace=False)

    on_disk = contract.sha256.hex()
    if on_disk != entry["sha256"]:
        pytest.fail(
            f"contracts/portals/{source}.yaml: the GOVERNED bytes changed under "
            f"contract_version {contract.version} — locked {entry['sha256']}, on disk "
            f"{on_disk}.\nThe hash covers the whole file MINUS `persistence:` and "
            f"`shadow:`, so prose, comments and `fetch:` all count; only those two blocks "
            f"are free.\n{_REMEDY}", pytrace=False)


def test_the_locked_hash_is_the_one_project_compares() -> None:
    """The lockfile must be the loader's own digest, not a second implementation of it.

    A private re-hash here could disagree with `contract_body_hash` — and the way it would
    show that is a green run followed by exactly the outage this file exists to prevent.
    """
    built = contract_lock.build_lock()
    assert built == _LOCK, (
        f"{contract_lock.LOCK_PATH.name} is stale.\n{_REMEDY}")
    for source, entry in built.items():
        contract = _CONTRACTS[source]
        assert entry["sha256"] == contracts.contract_body_hash(
            contract.path.read_bytes()).hex()
