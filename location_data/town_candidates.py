"""Candidate towns for the LLM location step — portal-agnostic (W2-10, 2026-09-08).

Measured on the candidates probe (#1338, 100 bazos ads, luna): the town an ad names sat
inside the PSČ-okres list 90/90, within 15 km of the portal's pin 90/90 (inside the
town's own polygon on 72/90, p90 1.3 km, max 5.3 km), in the text-matched list 87/90 (the
misses were abbreviations — "Rožnov p. R.", "Mar Lázně"), and in text ∪ pin-radius 90/90.
So the list is built from what EVERY portal has — the ad's text and a pin, even one that
is only a postcode centroid — and from nothing bazos-specific: text-matched registry
names ∪ obce within `DEFAULT_RADIUS_KM` of the pin, capped. A pick outside the list is
invalid by construction; the national list (every current obec) is the one retry for an
abstention, and only when the ad has an anchor at all.

Pure apart from `load_obec_index`, which reads the current obce with their authoritative
centroids ONCE per run: 6,258 points in memory make the radius test a haversine loop
instead of a per-ad geometry query (the probe paid 2.8 s/ad for the SQL form).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from location_data.name_index import normalize_name

DEFAULT_RADIUS_KM = 15.0
MAX_CANDIDATES = 400
_EARTH_RADIUS_KM = 6371.0088

# Czech consonant alternations that reach INTO the stem when a name declines — "Nová Paka"
# → "v Nové Pace", "Praha" → "v Praze" — so the last letter of the compared prefix may swap
# within a pair. The probe's only text-match miss on a plainly named town was Paka/Pace.
_ALTERNATIONS = frozenset({("k", "c"), ("c", "k"), ("h", "z"), ("z", "h"), ("g", "z"), ("z", "g")})

# {first-word key -> [(name, name words)]}: the per-token lookup that keeps matching
# linear in the text, not in the registry.
_KeyIndex = dict[str, list[tuple[str, list[str]]]]


def _word_matches(token: str, word: str) -> bool:
    """Declension-tolerant: a text token matches a name word when they share a prefix at
    least max(3, len(word) - 2) long (its last letter may alternate, see above) and the
    token is at most two characters longer — so "koline" finds "kolin", "praze" finds
    "praha" and "pace" finds "paka", while "boleslavskou" does not find "boleslav". Words of
    three characters or fewer must match exactly ("as", "nad", "u")."""
    if len(word) <= 3:
        return token == word
    if len(token) > len(word) + 2:
        return False
    need = max(3, len(word) - 2)
    if len(token) < need:
        return False
    last = need - 1
    return token[:last] == word[:last] and (
        token[last] == word[last] or (token[last], word[last]) in _ALTERNATIONS)


def _key_index(names: Iterable[str]) -> _KeyIndex:
    by_key: _KeyIndex = {}
    for name in names:
        words = normalize_name(name).split()
        if words:
            by_key.setdefault(words[0][:3], []).append((name, words))
    return by_key


def _match(tokens: list[str], by_key: _KeyIndex) -> list[str]:
    found: set[str] = set()
    for i, token in enumerate(tokens):
        for name, words in by_key.get(token[:3], ()):
            if name in found or i + len(words) > len(tokens):
                continue
            if all(_word_matches(tokens[i + j], w) for j, w in enumerate(words)):
                found.add(name)
    return sorted(found)


def text_match_candidates(text: str, names: Iterable[str]) -> list[str]:
    """Registry names whose words occur, in order, as consecutive tokens of the text.

    Over-generates on purpose (a bike — "kolo" — puts Kolín on the list): the model's job
    is to choose among a few, and a name on the list but not in the text costs nothing.
    Needs no postcode, no pin and no knowledge of the portal. Works for any registry
    vocabulary — obce, části obce, streets.
    """
    tokens = normalize_name(text).split()
    if not tokens:
        return []
    return _match(tokens, _key_index(names))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True, slots=True)
class ObecPoint:
    code: int
    name: str
    lat: float | None
    lon: float | None


class ObecIndex:
    """Every current obec, by name and by place. Built once per run, read per ad."""

    def __init__(self, points: Iterable[ObecPoint]) -> None:
        self._points = tuple(points)
        self.names: list[str] = sorted({p.name for p in self._points})
        self._by_norm: dict[str, list[ObecPoint]] = {}
        for point in self._points:
            self._by_norm.setdefault(normalize_name(point.name), []).append(point)
        self._keys = _key_index(self.names)

    def __len__(self) -> int:
        return len(self._points)

    def text_matches(self, text: str) -> list[str]:
        tokens = normalize_name(text).split()
        return _match(tokens, self._keys) if tokens else []

    def within_km(self, lat: float, lon: float, km: float) -> list[str]:
        """Obce whose centroid lies within `km` of the point; homonyms collapse by name."""
        return sorted({
            p.name for p in self._points
            if p.lat is not None and p.lon is not None
            and haversine_km(lat, lon, p.lat, p.lon) <= km})

    def codes_for_name(self, name: str) -> list[int]:
        return sorted(p.code for p in self._by_norm.get(normalize_name(name), ()))

    def nearest_code(self, name: str, lat: float | None, lon: float | None) -> int | None:
        """The one obec of that name — or, among homonyms, the one nearest the pin. None
        when the name is unknown, or ambiguous with no pin to break the tie."""
        points = self._by_norm.get(normalize_name(name), [])
        if not points:
            return None
        if len(points) == 1:
            return points[0].code
        if lat is None or lon is None:
            return None
        placed = [p for p in points if p.lat is not None and p.lon is not None]
        if not placed:
            return None
        return min(placed, key=lambda p: haversine_km(lat, lon, p.lat, p.lon)).code


def candidate_towns(
    index: ObecIndex, *, text: str, lat: float | None, lon: float | None,
    radius_km: float = DEFAULT_RADIUS_KM, cap: int = MAX_CANDIDATES,
) -> list[str]:
    """text-matched obce ∪ obce within `radius_km` of the pin, sorted, capped."""
    names = set(index.text_matches(text))
    if lat is not None and lon is not None:
        names.update(index.within_km(lat, lon, radius_km))
    return sorted(names)[:cap]


_OBEC_POINTS_SQL = """
    SELECT u.code, u.name,
           CASE WHEN g.centroid_point IS NULL THEN NULL ELSE ST_Y(g.centroid_point) END,
           CASE WHEN g.centroid_point IS NULL THEN NULL ELSE ST_X(g.centroid_point) END
      FROM ruian_admin_units u
      LEFT JOIN ruian_admin_unit_geometries g
        ON g.unit_id = u.id
       AND g.purpose = 'authoritative'
       AND g.registry_version_id = %(version)s
     WHERE u.level::text = 'obec' AND u.valid_to IS NULL
     ORDER BY u.code
"""


def load_obec_index(cur: Any, version_id: int) -> ObecIndex:
    """One read per run, on a cursor the caller already guards with a statement timeout."""
    cur.execute(_OBEC_POINTS_SQL, {"version": version_id})
    return ObecIndex(
        ObecPoint(code=int(code), name=str(name),
                  lat=None if lat is None else float(lat),
                  lon=None if lon is None else float(lon))
        for code, name, lat, lon in cur.fetchall())
