"""Text normalisation, sketching and fact extraction — the text half of E20, stdlib only.

Everything here is deterministic across processes: `hash()` is salted per interpreter, so
tokens are hashed with blake2b and cached, and every derived sketch (shingles, SimHash,
bands) is built from that stable token hash. A blocking key computed here today must equal
the one `autodedup.listing_fp` computes in production tomorrow.

SimHash is accumulated bit-parallel: each token's 64 set-bit positions are expanded ONCE
into 16-bit lanes of one big integer and cached, so a document is a handful of big-integer
additions rather than 64 Python iterations per token — the difference between ~3 s and
~0.05 s over the cohort.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable

try:  # the location program already owns the canonical deaccent
    from location_data.resolver.normalize import deaccent as _deaccent
except ImportError:  # pragma: no cover - the engine must stay importable standalone
    def _deaccent(value: str) -> str:
        return "".join(
            ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
        )

MASK64: int = (1 << 64) - 1
_FNV_OFFSET: int = 0xCBF29CE484222325
_FNV_PRIME: int = 0x100000001B3

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_WS = re.compile(r"\s+")

# Fact patterns run on deaccented lowercase text with punctuation PRESERVED: "4. patro" and
# "č. bytu 12" both carry their meaning in the punctuation.
_M2 = re.compile(r"(\d+(?:[.,]\d+)?)\s*m2\b")
# Dot-grouped thousands ("10.000 Kc") are ordinary on the portals, so the grouped form is
# tried FIRST: without it the match would start at the last triple and fabricate ("kc", 0.0).
_KC = re.compile(
    r"(\d{1,3}(?:[. ]\d{3})+(?:,\d{1,2})?|\d[\d ]*(?:,\d{1,2})?)\s*(?:kc|czk)\b"
)
_FLOOR = re.compile(r"(\d{1,2})\s*\.?\s*(?:np\b|patr\w*|podlaz\w*)")
_GROUND_FLOOR = re.compile(r"\bprizemi\w*\b")
_ROOMS = re.compile(r"\b(\d)\s*\+\s*(?:kk|1)\b")
_UNIT = re.compile(
    r"(?:jednotk\w*|byt\w*\s+c\.?|c\.\s*bytu|cislo\s*bytu)\s*(?:c\.?\s*)?(\d{1,5})\b"
)
_THOUSANDS = re.compile(r"^\d{1,3}(?:\.\d{3})+$")

# The scrapers' own vocabulary is `(\d)\+(kk|\d)`: a stored "3+2" must stay a known value,
# else E5's disposition veto and the K1 dispo arm silently skip the row.
_DISPO = re.compile(r"(\d)\s*\+\s*(kk|\d)\b")
_STUDIO = ("garsonier", "garsonk", "garzon", "studio")

_token_hashes: dict[str, int] = {}
_token_lanes: dict[str, int] = {}


def deaccent(value: str) -> str:
    """Diacritics folded away; `Kč` -> `Kc`, `m²` -> `m2` (NFKD is a compatibility fold)."""
    return _deaccent(value)


def fold(s: str | None) -> str:
    """lower(deaccent(s)) — the one expensive step both normalisations start from."""
    return deaccent(s).lower() if s else ""


def normalize_text(s: str | None) -> str:
    """lower(deaccent(s)) with every non-alphanumeric run folded to one space."""
    return normalize_folded(fold(s))


def normalize_folded(folded: str) -> str:
    """`normalize_text` over already-folded text — one deaccent can serve both forms."""
    return _NON_ALNUM.sub(" ", folded).strip() if folded else ""


def fact_text(s: str | None) -> str:
    """Lowercase, deaccented, whitespace-collapsed — punctuation kept, for `numeric_facts`."""
    return fact_text_folded(fold(s))


def fact_text_folded(folded: str) -> str:
    """`fact_text` over already-folded text."""
    return _WS.sub(" ", folded.replace(" ", " ")).strip() if folded else ""


def tokens(s: str) -> list[str]:
    """Whitespace tokens of `normalize_text(s)`; idempotent on already-normalised input."""
    return normalize_text(s).split()


def token_hash(token: str) -> int:
    """Stable unsigned 64-bit hash of one token, memoised across the whole run."""
    cached = _token_hashes.get(token)
    if cached is not None:
        return cached
    value = int.from_bytes(
        hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big"
    )
    _token_hashes[token] = value
    return value


def shingles(toks: list[str], k: int = 3) -> set[int]:
    """Hashed k-grams of the token list; a shorter list yields its single whole-list gram."""
    if k <= 0:
        raise ValueError(f"shingle size must be positive: {k}")
    if not toks:
        return set()
    if len(toks) < k:
        window = range(1)
        k = len(toks)
    else:
        window = range(len(toks) - k + 1)
    out: set[int] = set()
    for start in window:
        acc = _FNV_OFFSET
        for token in toks[start:start + k]:
            acc = ((acc ^ token_hash(token)) * _FNV_PRIME) & MASK64
        out.add(acc)
    return out


def _lanes(token: str) -> int:
    """The token's 64 set bits spread into 16-bit lanes, so a document sums in big-int adds."""
    cached = _token_lanes.get(token)
    if cached is not None:
        return cached
    value = token_hash(token)
    lanes = 0
    while value:
        low = value & -value
        lanes |= 1 << (16 * low.bit_length() - 16)
        value ^= low
    _token_lanes[token] = lanes
    return lanes


_LANE_CAPACITY: int = 1 << 16


def _simhash64_wide(toks: list[str]) -> int:
    """The per-bit reference accumulation, for documents too long for the 16-bit lanes."""
    counts = [0] * 64
    for token in toks:
        value = token_hash(token)
        for bit in range(64):
            if (value >> bit) & 1:
                counts[bit] += 1
    n = len(toks)
    out = 0
    for bit, count in enumerate(counts):
        if count * 2 > n:
            out |= 1 << bit
    return out - (1 << 64) if out >> 63 else out


def simhash64(toks: list[str]) -> int:
    """SimHash of the token list as a SIGNED 64-bit int — the same shape as `images.phash`."""
    if not toks:
        return 0
    if len(toks) >= _LANE_CAPACITY:  # a per-bit count would carry into the neighbouring lane
        return _simhash64_wide(toks)
    total = 0
    for token in toks:
        total += _lanes(token)
    n = len(toks)
    out = 0
    for bit in range(64):
        if ((total >> (16 * bit)) & 0xFFFF) * 2 > n:
            out |= 1 << bit
    return out - (1 << 64) if out >> 63 else out


def simhash_bands(h: int, n: int = 4, bits: int = 16) -> list[tuple[int, int]]:
    """`n` low-to-high slices of `bits` each, as `(band_no, band_val)` — the LSH probe keys."""
    if n <= 0 or bits <= 0 or n * bits > 64:
        raise ValueError(f"cannot split 64 bits into {n} bands of {bits}")
    unsigned = h & MASK64
    mask = (1 << bits) - 1
    return [(band, (unsigned >> (band * bits)) & mask) for band in range(n)]


def _to_float(raw: str) -> float | None:
    text = raw.strip().replace(" ", "")
    head, comma, tail = text.partition(",")
    if _THOUSANDS.fullmatch(head):
        head = head.replace(".", "")
    try:
        return float(f"{head}.{tail}" if comma else head)
    except ValueError:
        return None


def numeric_facts(s: str) -> set[tuple[str, float]]:
    """`(unit, value)` facts in Czech listing text: m2, kc, floor, rooms, unit number."""
    return numeric_facts_folded(fact_text(s))


def numeric_facts_folded(text: str) -> set[tuple[str, float]]:
    """`numeric_facts` over a string already through `fact_text` — skips a second deaccent."""
    if not text:
        return set()
    out: set[tuple[str, float]] = set()
    for unit, pattern in (("m2", _M2), ("kc", _KC), ("floor", _FLOOR),
                          ("rooms", _ROOMS), ("unit", _UNIT)):
        for match in pattern.finditer(text):
            value = _to_float(match.group(1))
            if value is not None:
                out.add((unit, value))
    if _GROUND_FLOOR.search(text):
        out.add(("floor", 0.0))
    return out


def disposition_norm(s: str | None) -> str | None:
    """`3 + KK` -> `3+kk`, `garsoniéra` -> `1+kk`; unrecognised shapes stay `None`."""
    if not s:
        return None
    text = fact_text(s)  # normalize_text would fold the '+' away and lose the disposition
    if not text:
        return None
    if any(word in text for word in _STUDIO):
        return "1+kk"
    match = _DISPO.search(text)
    if match:
        return f"{match.group(1)}+{match.group(2)}"
    if "atypic" in text or "atypik" in text:
        return "atypicky"
    if "pokoj" in text:
        return "pokoj"
    return None


def rare_tokens(toks: Iterable[str], df: dict[str, int], max_df: int) -> set[str]:
    """Tokens whose in-block document frequency is at most `max_df` (E20's discriminators)."""
    return {token for token in set(toks) if df.get(token, 1) <= max_df}
