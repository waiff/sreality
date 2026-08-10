"""RÚIAN daily VFR change files (C2) — fetch and chain verification ONLY.

What is implemented and sound:
  * the URL grammar `{YYYYMMDD}_ST_ZZSZ.xml.zip` (verified live: 2026-08-09 = 1,774,366 B);
  * the header contract — `VerzeVFR`, `TypDavky`, `TypSouboru`, `Datum`, `TransakceOd/Do`,
    `PredchoziSoubor` — quoted verbatim in the recon corpus;
  * gap-provable chain verification: `PredchoziSoubor` must name the last applied file,
    and a mismatch is a HARD STOP, never a warning;
  * the weekend rule: a 404 or a ~2 KB file on Sat/Sun is measured-normal behaviour and
    must not alert (the single most likely source of alert fatigue in the ops calendar).

What is NOT implemented, deliberately: APPLYING the deltas. The recon corpus verifies one
`AdresniMisto` sample element and the existence of `ZaniklyPrvek` entries with
`TypPrvkuKod` + `PrvekId`, but it does NOT establish (a) the full element schema of
`ST_ZZSZ` — the sample carries no `CisloOrientacni`, no `ZnakCisla`, no obec pointer, so a
naive apply would null out NOT NULL mirror columns on every touched row — nor (b) the
`TypPrvkuKod` code values needed to route a retirement to the right table. Applying on
those unknowns would silently corrupt the identity spine, which is exactly the failure
class this subsystem exists to prevent. The lane therefore FAILS LOUDLY (exit 3) rather
than pretending to be a freshness lane; the mirror stays on the monthly C1 baseline, which
is the design's own documented free degradation path.

CLI:  python -m location_data.vfr_delta [--date YYYYMMDD]
"""

from __future__ import annotations

import argparse
import datetime
import io
import logging
import re
import sys
import zipfile
from dataclasses import dataclass

import psycopg
import requests

from location_data import loader_db, ruian_csv

LOG = logging.getLogger("location_data.vfr_delta")

VFR_BASE = "https://vdp.cuzk.gov.cz/vymenny_format/soucasna"
ZZSZ_URL = VFR_BASE + "/{date}_ST_ZZSZ.xml.zip"
ZKSH_URL = VFR_BASE + "/{date}_ST_ZKSH.xml.zip"

SUPPORTED_VFR_VERSION = "3.1"
# Measured: 2026-08-02 (Sun) ST_ZZSZ = 1,943 B, 2026-08-08 (Sat) = 129,599 B. Anything at
# or under this on a weekend means "no changes", not a failure.
WEEKEND_EMPTY_BYTES = 200_000

_FIELD = {
    "vfr_version": r"<vf:VerzeVFR>([^<]+)</vf:VerzeVFR>",
    "record_type": r"<vf:TypZaznamu>([^<]+)</vf:TypZaznamu>",
    "batch_type": r"<vf:TypDavky>([^<]+)</vf:TypDavky>",
    "file_type": r"<vf:TypSouboru>([^<]+)</vf:TypSouboru>",
    "stamp": r"<vf:Datum>([^<]+)</vf:Datum>",
    "previous_file": r"<vf:PredchoziSoubor>([^<]+)</vf:PredchoziSoubor>",
}
_TRANSACTION = {
    "transaction_from": r"<vf:TransakceOd>\s*<com:Id>(\d+)</com:Id>",
    "transaction_to": r"<vf:TransakceDo>\s*<com:Id>(\d+)</com:Id>",
}


class ChainBreak(RuntimeError):
    """`PredchoziSoubor` does not name the last applied file — apply nothing further."""


class SchemaDrift(RuntimeError):
    """`VerzeVFR` moved away from the contracted version."""


class DeltaApplyNotImplemented(NotImplementedError):
    """The delta lane can verify a file but must not mutate the mirror yet."""


@dataclass(frozen=True, slots=True)
class DeltaHeader:
    vfr_version: str
    record_type: str | None
    batch_type: str | None
    file_type: str | None
    stamp: str | None
    previous_file: str | None
    transaction_from: int | None
    transaction_to: int | None


def parse_header(xml: str) -> DeltaHeader:
    """Read the six header scalars. Regex rather than a parser: the whole point is to read
    the first element of a multi-megabyte document without materializing a tree."""
    values: dict[str, str | None] = {}
    head = xml[:20_000]
    for key, pattern in _FIELD.items():
        match = re.search(pattern, head)
        values[key] = match.group(1).strip() if match else None
    numbers: dict[str, int | None] = {}
    for key, pattern in _TRANSACTION.items():
        match = re.search(pattern, head)
        numbers[key] = int(match.group(1)) if match else None
    if not values.get("vfr_version"):
        raise SchemaDrift("no <vf:VerzeVFR> in the change file header")
    return DeltaHeader(
        vfr_version=values["vfr_version"] or "",
        record_type=values.get("record_type"),
        batch_type=values.get("batch_type"),
        file_type=values.get("file_type"),
        stamp=values.get("stamp"),
        previous_file=values.get("previous_file"),
        transaction_from=numbers.get("transaction_from"),
        transaction_to=numbers.get("transaction_to"),
    )


def assert_supported(header: DeltaHeader) -> None:
    if header.vfr_version != SUPPORTED_VFR_VERSION:
        raise SchemaDrift(
            f"VerzeVFR={header.vfr_version}, contracted {SUPPORTED_VFR_VERSION} — stop and page"
        )


def verify_chain(header: DeltaHeader, last_applied_file: str | None) -> None:
    """A mirror must be able to PROVE it missed no day, not assume it."""
    if last_applied_file is None:
        return
    if header.previous_file != last_applied_file:
        raise ChainBreak(
            f"PredchoziSoubor={header.previous_file!r} but the last applied file is "
            f"{last_applied_file!r} — a day is missing. Fetch it if still inside the "
            "~90-day retention window, otherwise re-baseline; apply nothing meanwhile."
        )


def is_weekend_empty(when: datetime.date, size_bytes: int) -> bool:
    return when.weekday() >= 5 and size_bytes <= WEEKEND_EMPTY_BYTES


def last_applied_file(conn: psycopg.Connection) -> str | None:
    url = loader_db.scalar(
        conn,
        """
        SELECT artifact_urls ->> 'vfr_delta'
          FROM registry_versions
         WHERE kind = 'delta' AND artifact_urls ? 'vfr_delta'
         ORDER BY id DESC LIMIT 1
        """,
    )
    return None if url is None else str(url).rsplit("/", 1)[-1]


def fetch(sess: requests.Session, when: datetime.date) -> tuple[bytes, str] | None:
    """Download one day's basic change file; None when ČÚZK published none."""
    url = ZZSZ_URL.format(date=when.strftime("%Y%m%d"))
    resp = sess.get(url, timeout=(30, 300))
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content, url


def read_xml(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        member = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
        if member is None:
            raise SchemaDrift("change file contains no XML member")
        return zf.read(member).decode("utf-8", errors="replace")


def summarize(xml: str) -> dict[str, int]:
    return {
        "address_points": xml.count("<vf:AdresniMisto"),
        "retirements": xml.count("<vf:ZaniklyPrvek"),
    }


def apply_delta(conn: psycopg.Connection, header: DeltaHeader, xml: str) -> None:
    raise DeltaApplyNotImplemented(
        "VFR delta application is NOT implemented. Missing before it can be written "
        "soundly: (1) the complete ST_ZZSZ AdresniMisto element schema — the verified "
        "sample carries Kod/CisloDomovni/Psc/StavebniObjekt/Ulice/VOKod/PlatiOd/"
        "IdTransakce and a negative EPSG:5514 point, but no cislo orientacni, no znak and "
        "no obec pointer, so applying it would null NOT NULL mirror columns; (2) the "
        "TypPrvkuKod vocabulary needed to route a ZaniklyPrvek retirement to the right "
        "registry table. Until both are pinned down, freshness comes from the monthly C1 "
        "baseline (the design's documented free degradation path)."
    )


def run(*, when: datetime.date, apply: bool) -> int:
    sess = ruian_csv.session()
    fetched = fetch(sess, when)
    if fetched is None:
        if when.weekday() >= 5:
            LOG.info("VFR %s: no file published (weekend) — normal", when)
            return 0
        LOG.warning("VFR %s: no file published on a weekday — retry next cycle", when)
        return 0
    blob, url = fetched
    if is_weekend_empty(when, len(blob)):
        LOG.info("VFR %s: %d bytes (weekend near-empty) — normal", when, len(blob))
        return 0

    xml = read_xml(blob)
    header = parse_header(xml)
    assert_supported(header)
    counts = summarize(xml)
    LOG.info(
        "VFR %s type=%s batch=%s previous=%s transactions=%s..%s address_points=%d retirements=%d",
        when, header.file_type, header.batch_type, header.previous_file,
        header.transaction_from, header.transaction_to,
        counts["address_points"], counts["retirements"],
    )

    with loader_db.open_loader_connection() as conn:
        verify_chain(header, last_applied_file(conn))
        LOG.info("VFR chain verified against %s", url)
        if not apply:
            return 0
        apply_delta(conn, header, xml)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYYMMDD (default: yesterday — the file dated D "
                                       "covers D's transactions)")
    parser.add_argument("--verify-only", action="store_true",
                        help="fetch + verify the chain and exit 0 without attempting to apply")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    when = (
        datetime.datetime.strptime(args.date, "%Y%m%d").date()
        if args.date
        else datetime.date.today() - datetime.timedelta(days=1)
    )
    try:
        return run(when=when, apply=not args.verify_only)
    except DeltaApplyNotImplemented as exc:
        LOG.error("VFR %s", exc)
        return 3
    except (ChainBreak, SchemaDrift) as exc:
        LOG.error("VFR hard stop: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
