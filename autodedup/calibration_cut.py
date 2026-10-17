"""E962: the calibration cut, streamed — `Calibration.build`'s six statistics folded one chunk of
adverts at a time, so a seed holds counts rather than every scope advert's facts and fingerprint.

`Calibration.build` (the oracle, in the decision module `incremental`, left as it is so the
engine digest does not move) reads a whole cohort at once: ~37 kB of fingerprint and ~6 kB of
facts an advert held until the end, ~56 kB at its peak — ~18 GB for the 323,128 adverts of the
planned re-seed on a 16 GiB runner. Every statistic it takes is a count over adverts, an exact
order statistic of their prices, or a verdict over a code's carriers, so each folds exactly:

- `price_cuts`: each cohort's prices, kept (8 bytes an advert, in the order `Calibration.build`
  adds them: ascending listing id), cut by `blocking.decile_cuts` once all are in;
- `exploded`: each probe key's posting count (`blocking.posting_keys`), the broker key held under
  the advert's PRICE until the cuts exist and then counted under its decile (`decile_of`);
- `block_docs`, `block_token_df`, `corpus_docs`, `corpus_token_df`, `pin_pop`, `block_attr_df`:
  integer counts, so `FeatureContext.build` and `index_attrs` run on each chunk as they would on
  the cohort and their counts are summed (counts over disjoint chunks add);
- `certifying_codes`: `index_reference_codes` over the adverts that print a code (~7 %), each
  kept as its description, disposition and area — all the verdict reads.

No float is summed anywhere, and the payload is serialised with sorted keys, so the calibration
is the oracle's to the byte; `tests/autodedup/test_calibration_cut.py` holds the two in lockstep.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Any, Mapping

from autodedup.blocking import decile_cuts, decile_of, posting_keys, probe_priority
from autodedup.dataset import Listing
from autodedup.features import FEATURE_VERSION, FeatureContext
from autodedup.fingerprint import Fingerprint
from autodedup.incremental import GENERATION, Calibration, _cut_key, _now, key_token
from autodedup.settings import Settings
from autodedup.text_facts import reference_codes

_Cohort = tuple[str | None, str | None]


@dataclass(slots=True)
class _Carrier:
    """What `index_reference_codes` reads of an advert that prints a code."""

    description: str | None
    disposition: str | None
    area_m2: float | None


def _add(into: dict[Any, int], counts: Mapping[Any, int]) -> None:
    for key, count in counts.items():
        into[key] = into.get(key, 0) + count


class CalibrationCut:
    """Fold chunks of (fingerprint, facts) in ascending listing id, then `build`."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.n_listings = 0
        self._last_id: int | None = None
        self._prices: dict[_Cohort, array] = {}
        self._postings: dict[str, dict[Any, int]] = {probe: {} for probe in
                                                     probe_priority(settings)}
        self.block_docs: dict[str, int] = {}
        self.block_token_df: dict[str, dict[str, int]] = {}
        self.corpus_docs = 0
        self.corpus_token_df: dict[str, int] = {}
        self.block_attr_df: dict[str, dict[tuple[str, str], int]] = {}
        self.pin_pop: dict[str, int] = {}
        self._carriers: dict[int, _Carrier] = {}

    def fold(self, fps: Mapping[int, Fingerprint], listings: Mapping[int, Listing]) -> None:
        """One chunk. Its ids must all lie above every id folded before: the prices then enter
        in the oracle's order, so even ties of -0.0 and 0.0 sort as they do there, and no
        advert is counted twice. A listing without a fingerprint still carries its codes, as
        it does in `Calibration.build`."""
        order = sorted(fps)
        if order and self._last_id is not None and order[0] <= self._last_id:
            raise ValueError(f"CalibrationCut.fold: listing {order[0]} is not above "
                             f"{self._last_id}, the last one folded")
        if order:
            self._last_id = order[-1]
        for listing_id in order:
            fp = fps[listing_id]
            if fp.price is not None:
                self._prices.setdefault((fp.cat_group, fp.category_type),
                                        array("d")).append(float(fp.price))
            for probe, key in posting_keys(fp, self.settings,
                                           fp.price if fp.broker_key else None):
                bucket = self._postings[probe]
                bucket[key] = bucket.get(key, 0) + 1
        ctx = FeatureContext.build(fps, self.settings, None)
        ctx.index_attrs(fps, listings)
        for block, docs in ctx.block_docs.items():
            self.block_docs[block] = self.block_docs.get(block, 0) + docs
        for block, df in ctx.block_token_df.items():
            _add(self.block_token_df.setdefault(block, {}), df)
        _add(self.corpus_token_df, ctx.corpus_token_df)
        self.corpus_docs += ctx.corpus_docs
        for block, bucket in ctx.block_attr_df.items():
            _add(self.block_attr_df.setdefault(block, {}), bucket)
        _add(self.pin_pop, ctx.pin_pop)
        for listing_id, listing in listings.items():
            if reference_codes(listing.description):
                self._carriers[listing_id] = _Carrier(listing.description, listing.disposition,
                                                      listing.area_m2)
        self.n_listings += len(fps)

    def build(self, generation: str = GENERATION) -> Calibration:
        cuts = {cohort: decile_cuts(list(prices)) for cohort, prices in self._prices.items()}
        limit = self.settings.max_block_size
        exploded: dict[str, list[str]] = {}
        for probe, counts in self._postings.items():
            if probe == "broker":
                deciles: dict[Any, int] = {}
                for (broker, cat_group, category_type, price), count in counts.items():
                    key = (broker, cat_group, category_type,
                           decile_of(price, cuts.get((cat_group, category_type))))
                    deciles[key] = deciles.get(key, 0) + count
                counts = deciles
            exploded[probe] = sorted(key_token(key) for key, count in counts.items()
                                     if count > limit)
        codes = FeatureContext(settings=self.settings)
        codes.index_reference_codes(self._carriers)  # type: ignore[arg-type]
        certifying: set[str] = set()
        for found in codes.codes.values():
            certifying |= set(found)
        return Calibration(
            generation=generation,
            feature_version=FEATURE_VERSION,
            built_at=_now(),
            n_listings=self.n_listings,
            price_cuts={_cut_key(*cohort): list(values) for cohort, values in cuts.items()},
            exploded=exploded,
            block_docs=dict(self.block_docs),
            block_token_df={block: dict(df) for block, df in self.block_token_df.items()},
            corpus_docs=self.corpus_docs,
            corpus_token_df=dict(self.corpus_token_df),
            block_attr_df={block: {key_token(pair): count for pair, count in bucket.items()}
                           for block, bucket in self.block_attr_df.items()},
            pin_pop=dict(self.pin_pop),
            certifying_codes=sorted(certifying),
        )
