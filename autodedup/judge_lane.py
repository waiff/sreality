"""`mode=judge` — run the W3 LLM judge over a stratified sample of one engine pass.

The whole pass in one sentence: download the W1 cohort artifact from a finished `export` run,
re-run the engine over it in-process, draw a deterministic stratified sample of pairs, and ask
the judge for a four-way verdict on each — text-only, with images, or three deep votes.

Three invariants this lane owns, all of them paid for the hard way elsewhere in this repo:

  * `--max-usd` is REQUIRED and the lane refuses to start without it (E31), the budget binds in
    the worker BEFORE the call under a shared lock (checking afterwards on a parallel lane means
    discovering the overspend once every in-flight call is billed), and the summary reports
    `done`, not merely `drawn` — five 3,000-image batches once refused themselves against a
    pre-flight estimate, spent nothing, reported only their draw line, and nobody noticed.
  * Each worker opens its OWN connection and `LLMClient`: psycopg connections are not
    thread-safe and every call writes one `llm_calls` row, so a shared client would interleave
    writes and corrupt the cost ledger.
  * PII never crosses the wire (E28). The artifact's descriptions are already scrubbed by
    `autodedup.export.scrub_description`; this lane runs that scrub AGAIN over the text it is
    about to send and truncates to 1,200 characters, because a defensive second pass costs
    microseconds and the failure it prevents is a broker's mobile number sitting in a
    third-party processor's logs.

Cost is an ESTIMATE only until the first pass lands; from then on E32 binds and every number
is re-read from `llm_calls` (`JUDGEMENT_COST_SQL`), never extrapolated from token prices again.

The judge's prompts, digests, tool schema and verdict parsing live in `autodedup/judge.py`; this
module is the plumbing around them and imports it lazily, so the lane registry stays importable
while that module is still being built.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from autodedup import harness
from autodedup.dataset import Image, Listing, load
from autodedup.features import (
    STREET_GRAIN_RANK,
    attribute_conflicts,
    haversine_m,
    uncertainty_radius_m,
)
from autodedup.judge_sql import (
    GOLD_PAIRS_SQL,
    JUDGEMENT_CACHED_SQL,
    JUDGEMENT_COST_SQL,
    JUDGEMENT_POD_COST_SQL,
    JUDGEMENT_UPSERT_SQL,
)
from autodedup.model import LogisticModel, hand_initialised
from autodedup.settings import Settings
from toolkit.vision_batch import is_fatal

TIERS: tuple[str, ...] = ("smoke", "text", "vision", "gold", "oss")

MODEL_TEXT: str = "gpt-5.6-luna"
MODEL_VISION: str = "gpt-5-mini"
MODEL_GOLD_THIRD: str = "qwen3-vl-30b-a3b-instruct"
MODEL_OSS: str = "Qwen/Qwen2.5-VL-7B-Instruct"

# The `oss` tier is the VISION prompt, unchanged, served by a model this program rents by the
# hour instead of buying by the token — so it reuses migration 527's `autodedup_judge_vision`
# rather than minting a fourth `called_for` value that would split the same question's spend
# across two ledger keys. What tells the two arms apart is `judgements.model`, which carries
# the `oss:` prefix, and the tier itself.
CALLED_FOR: dict[str, str] = {
    "text": "autodedup_judge_text",
    "vision": "autodedup_judge_vision",
    "gold": "autodedup_judge_gold",
    "oss": "autodedup_judge_vision",
}

OSS_PREFIX: str = "oss:"
OSS_PROVIDER: str = "oss"
OSS_BASE_URL_ENV: str = "OSS_LLM_BASE_URL"

MAX_TOKENS: int = 4096
IMAGES_VISION: int = 4
IMAGES_GOLD: int = 6
SMOKE_PAIRS: int = 10
GOLD_VOTES: int = 3

# D2, wired into the flag rather than left to the dispatcher's fingers: a lane under $10 runs
# autonomously, $25 is the hard per-run cap, $200 is the program total.
HARD_CAP_USD: float = 25.0

# Pre-flight estimates ONLY, and PER CALL — the budget's look-ahead and the `dry_run` readout.
# E32: after the first pass every published cost comes from `llm_calls`. Derived in the JUDGE
# SPEC §7 with reasoning tokens budgeted, which is the term every naive count drops. The spec
# quotes gold PER PAIR; the budget binds PER CALL, so the three gold rows are split out and
# pinned to that headline by a test — charging the pair price three times once truncated a $10
# gold pass at a third of the pairs it had paid headroom for.
EST_COST_USD: dict[str, float] = {
    "text": 0.00172,
    "vision": 0.00568,
    "gold_mini": 0.00705,
    "gold_qwen": 0.00444,
}
GOLD_PER_PAIR_USD: float = 0.018
EST_TOKENS_PER_IMAGE: int = 1100
EST_CHARS_PER_TOKEN: int = 4

# The `oss` tier's money is WALL CLOCK on a rented GPU, not tokens, so none of the table above
# applies to it. `OSS_MIN_USD` is the floor `max_usd` must clear: a pod that boots, loads
# weights and answers one pair cannot be had for less, and a budget under it would rent the
# machine and stop before the first verdict — E31 wearing a GPU.
OSS_MIN_USD: float = 0.25
# Pre-flight only, and per PAIR per WORKER: the look-ahead that decides whether `n` pairs fit
# the budget, and the margin the in-run gate leaves so the pod is torn down BEFORE the cap
# rather than one pair after it. Measured numbers replace it the moment the first pass lands.
OSS_EST_S_PER_PAIR: float = 12.0

# A 429 is the steady state at six workers pushing eight images a call, and it is not a verdict:
# retry it before it shrinks the sample (or, on gold, silently downgrades the independent model
# family to a gpt-5-mini self-consistency vote). Three retries at 2/4/8s: a burst limit clears
# inside fourteen seconds, and anything that does not is no longer the burst this sleep was
# sized for — the gold tier takes its weaker fallback rather than hold a worker any longer.
RETRY_BACKOFF_S: tuple[float, ...] = (2.0, 4.0, 8.0)
RETRY_SLEEP: Callable[[float], None] = time.sleep

# The pod's meter: WALL clock, because that is what RunPod bills and what `oss_pod.PodHandle`
# stamps itself with — a monotonic reading could not be compared against `started_at` at all.
# A seam because every test about what a pass cost has to be able to move it.
CLOCK: Callable[[], float] = time.time

# Set on an exception that a BILLED response raised while being parsed.
JUDGE_BILLED: str = "judge_billed"

DESCRIPTION_MAX: int = 1200
ARTIFACT_PREFIX: str = "autodedup-export-"
COHORT_FILE: str = "cohort.jsonl.gz"
GH_TIMEOUT_S: int = 900

SAMPLE_FILE: str = "sample.json"
RUN_FILE: str = "run.json"
JUDGEMENTS_FILE: str = "judgements.jsonl"
SUMMARY_FILE: str = "judge.json"

_JUDGE: Any = None

def load_engine_settings(raw: str | None) -> Settings:
    """`settings=<name>` under autodedup/settings/ (or an existing file path); absent = defaults."""
    if not raw:
        return Settings()
    from pathlib import Path

    if Path(raw).is_file():
        return Settings.from_json(raw)
    from autodedup.score_lane import SETTINGS_DIR, repo_path

    return Settings.from_json(str(repo_path(raw, SETTINGS_DIR)))


def load_engine_model(raw: str | None) -> LogisticModel:
    """`model=<name>` under autodedup/models/ scores the cohort; absent = the hand prior."""
    if not raw:
        return hand_initialised()
    from autodedup.score_lane import MODELS_DIR, repo_path

    path = repo_path(raw, MODELS_DIR)
    return LogisticModel.from_json(json.loads(path.read_text(encoding="utf-8")))



def judge_module() -> Any:
    """`autodedup.judge`, imported on first use so the lane registry never depends on it."""
    global _JUDGE
    if _JUDGE is None:
        from autodedup import judge as module

        _JUDGE = module
    return _JUDGE


# --- arguments -------------------------------------------------------------------------


@dataclass(slots=True)
class JudgeArgs:
    export_run: str
    tier: str
    n: int
    max_usd: float
    seed: int
    workers: int
    judge_version: str | None
    strata: tuple[str, ...]
    dry_run: bool
    refresh: bool
    cohort: str | None
    oss_model: str
    # Space-separated so one `k=v` arg can hold a whole preference list (`args` splits on
    # commas). Empty = the module's own default ranking.
    oss_gpu: tuple[str, ...]
    oss_cloud: tuple[str, ...]
    oss_image: str | None
    oss_tool_parser: str | None
    oss_dtype: str | None
    oss_disk_gb: int
    oss_max_model_len: int
    oss_max_usd_hr: float
    oss_min_gpu_gb: float
    oss_s_per_pair: float
    pairs_from: str | None
    # Which judge_version's gold rows `pairs_from=gold` reads. Defaults to the run's own
    # version, and must be set explicitly to compare a NEW presentation against the gold labels
    # a previous one produced: the labels are a property of the PAIR, not of the prompt.
    gold_version: str | None
    # The engine row the lane scores AND digests with. One row, or the prompt describes a
    # different engine than the one that drew the sample.
    settings: str | None
    model: str | None


def _int_arg(args: dict[str, str], name: str, default: int) -> int:
    raw = (args.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _flag(args: dict[str, str], name: str) -> bool:
    return (args.get(name) or "0").strip().lower() in ("1", "true", "yes", "on")


def _float_arg(args: dict[str, str], name: str, default: float) -> float:
    raw = (args.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be positive, got {value}")
    return value


def parse_args(args: dict[str, str]) -> JudgeArgs:
    """E31 in one function: no `max_usd`, no run — before a single byte is downloaded."""
    raw_budget = (args.get("max_usd") or "").strip()
    if not raw_budget:
        raise SystemExit(
            "max_usd is required on every paid lane (E31): "
            "--args 'export_run=...,tier=text,n=400,max_usd=5'"
        )
    try:
        max_usd = float(raw_budget)
    except ValueError as exc:
        raise SystemExit(f"max_usd must be a number, got {raw_budget!r}") from exc
    if max_usd <= 0:
        raise SystemExit(f"max_usd must be positive, got {max_usd}")
    if max_usd > HARD_CAP_USD:
        raise SystemExit(
            f"max_usd {max_usd} exceeds the D2 hard per-run cap of ${HARD_CAP_USD:.2f} "
            "(a program ruling, not a typo guard) — split the pass into several runs"
        )

    tier = (args.get("tier") or "smoke").strip()
    if tier not in TIERS:
        raise SystemExit(f"unknown tier {tier!r}; known tiers: {', '.join(TIERS)}")
    floor_usd = min_budget_usd(tier)
    if max_usd < floor_usd:
        raise SystemExit(
            f"max_usd {max_usd} cannot pay for a single {tier} pair (~${floor_usd:.5f}): "
            "the lane would draw its sample, refuse every call and report a green zero"
        )

    cohort = (args.get("cohort") or "").strip() or None
    export_run = (args.get("export_run") or "").strip()
    if not export_run and not cohort:
        raise SystemExit("export_run (the finished `export` lane run id) is required")
    if export_run and not export_run.isdigit():
        raise SystemExit(f"export_run must be a GitHub run id, got {export_run!r}")

    workers = _int_arg(args, "workers", 6)
    if workers < 1:
        raise SystemExit(f"workers must be at least 1, got {workers}")
    n = _int_arg(args, "n", SMOKE_PAIRS if tier == "smoke" else 400)
    if n < 1:
        raise SystemExit(f"n must be at least 1, got {n}")

    pairs_from = (args.get("pairs_from") or "").strip() or None
    if pairs_from not in (None, "gold"):
        raise SystemExit(f"pairs_from accepts only 'gold', got {pairs_from!r}")

    return JudgeArgs(
        export_run=export_run,
        tier=tier,
        n=n,
        max_usd=max_usd,
        seed=_int_arg(args, "seed", harness.SAMPLE_SEED),
        workers=workers,
        judge_version=(args.get("judge_version") or "").strip() or None,
        strata=tuple(part for part in (args.get("strata") or "").split() if part),
        dry_run=_flag(args, "dry_run"),
        refresh=_flag(args, "refresh"),
        cohort=cohort,
        oss_model=(args.get("oss_model") or "").strip() or MODEL_OSS,
        oss_gpu=tuple(part for part in (args.get("oss_gpu") or "").split() if part),
        oss_cloud=tuple(
            part.upper() for part in (args.get("oss_cloud") or "").split() if part
        ),
        oss_image=(args.get("oss_image") or "").strip() or None,
        oss_tool_parser=(args.get("oss_tool_parser") or "").strip() or None,
        oss_dtype=(args.get("oss_dtype") or "").strip() or None,
        oss_disk_gb=_int_arg(args, "oss_disk_gb", 0),
        oss_max_model_len=_int_arg(args, "oss_max_model_len", 0),
        oss_max_usd_hr=_float_arg(args, "oss_max_usd_hr", 0.0),
        oss_min_gpu_gb=_float_arg(args, "oss_min_gpu_gb", 0.0),
        oss_s_per_pair=_float_arg(args, "oss_s_per_pair", OSS_EST_S_PER_PAIR),
        pairs_from=pairs_from,
        gold_version=(args.get("gold_version") or "").strip() or None,
        settings=(args.get("settings") or "").strip() or None,
        model=(args.get("model") or "").strip() or None,
    )


# --- the cohort artifact ---------------------------------------------------------------


def download_cohort(export_run: str, dest: Path) -> Path:
    """`gh run download <id> -n autodedup-export-<id>` — the artifact IS the whole input."""
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        raise SystemExit(
            "GH_TOKEN (or GITHUB_TOKEN) must be set to download the export artifact"
        )
    dest.mkdir(parents=True, exist_ok=True)
    name = f"{ARTIFACT_PREFIX}{export_run}"
    proc = subprocess.run(
        ["gh", "run", "download", export_run, "-n", name, "--dir", str(dest)],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise SystemExit(f"gh run download {export_run} ({name}) failed: {detail}")
    found = sorted(dest.rglob(COHORT_FILE))
    if not found:
        raise SystemExit(f"artifact {name} carries no {COHORT_FILE}")
    return found[0]


# --- one pair's call plan --------------------------------------------------------------


@dataclass(slots=True)
class Vote:
    """One model call: which model, which `called_for`, how it picks the images."""

    tier: str
    model: str
    called_for: str
    n_images: int
    strategy: str
    est_usd: float
    weaker: bool = False
    shuffle: bool = False
    # Set only where the model id does not name its own backend: `oss:<repo>/<name>` is
    # namespaced precisely so it cannot route by vendor prefix to that vendor's PAID API, and
    # naming the provider outright keeps this lane right whatever `provider_for_model` does
    # next. `api.providers.oss` strips the prefix before the id goes on the wire.
    provider: str | None = None
    # The tier whose PROMPT this vote sends, when it differs from the tier it is stored
    # under. `oss` is the vision prompt to the character — `judge.TIERS` knows three tiers
    # and this arm must not become a fourth prompt, or the comparison measures two changes.
    prompt_tier: str | None = None


@dataclass(slots=True)
class PairJob:
    lo: int
    hi: int
    row: dict[str, Any]
    stratum: str
    votes: tuple[Vote, ...]
    # Position in the DRAWN sample, stamped once at build. The number the summary publishes when
    # an arm goes down mid-pass, so "it died at pair 598 of 2,240" is a fact about the draw and
    # not about whichever of six workers happened to notice first.
    index: int = 0


def vote_plan(tier: str, oss_model: str = MODEL_OSS) -> tuple[Vote, ...]:
    """The tiers of E26 and the three gold votes of the JUDGE SPEC §2, as data."""
    text = Vote("text", MODEL_TEXT, CALLED_FOR["text"], 0, "matched_first",
                EST_COST_USD["text"])
    vision = Vote("vision", MODEL_VISION, CALLED_FOR["vision"], IMAGES_VISION, "matched_first",
                  EST_COST_USD["vision"])
    if tier == "text":
        return (text,)
    if tier == "vision":
        return (vision,)
    if tier == "oss":
        # Byte-for-byte the vision vote — same prompt, same 4+4 image selection, same strategy.
        # An arm that is measured against the paid judge and differs from it in ANY input but
        # the model is measuring two things at once. `est_usd` is 0 because this tier's budget
        # is the pod's wall clock, held by `PodBudget`, not a per-call estimate.
        return (Vote("oss", f"{OSS_PREFIX}{oss_model}", CALLED_FOR["oss"], IMAGES_VISION,
                     "matched_first", 0.0, provider=OSS_PROVIDER, prompt_tier="vision"),)
    if tier == "smoke":
        return (text, vision)
    return (
        Vote("gold", MODEL_VISION, CALLED_FOR["gold"], IMAGES_GOLD, "matched_first",
             EST_COST_USD["gold_mini"]),
        Vote("gold", MODEL_VISION, CALLED_FOR["gold"], IMAGES_GOLD, "sequence_first",
             EST_COST_USD["gold_mini"]),
        Vote("gold", MODEL_GOLD_THIRD, CALLED_FOR["gold"], IMAGES_GOLD, "matched_first",
             EST_COST_USD["gold_qwen"]),
    )


def min_budget_usd(tier: str) -> float:
    """One pair of this tier at the pre-flight estimate — the budget below which the lane can
    only draw a sample and refuse every call, which is the E31 failure spelled backwards."""
    if tier == "oss":
        return OSS_MIN_USD
    return sum(vote.est_usd for vote in vote_plan(tier))


def oss_est_usd(usd_per_hr: float, pairs: int, workers: int, s_per_pair: float) -> float:
    """What renting the pod for this draw should cost: `n` pairs spread over `workers`,
    at `s_per_pair` each, billed by the hour. The pre-flight number AND the in-run margin."""
    seconds = max(1.0, pairs) * max(0.1, s_per_pair) / max(1, workers)
    return usd_per_hr * seconds / 3600.0


# A third gpt-5-mini vote is self-consistency, not independence: it is taken only when the
# genuinely different model family is unavailable, and is flagged weaker wherever it lands. Its
# only difference from G2 is the presentation order, reversed deterministically in `_one_call` —
# `select_images` owns WHICH frames are shown and offers no shuffled strategy of its own.
GOLD_FALLBACK = Vote(
    "gold", MODEL_VISION, CALLED_FOR["gold"], IMAGES_GOLD, "sequence_first",
    EST_COST_USD["gold_mini"], weaker=True, shuffle=True,
)


# --- inputs ----------------------------------------------------------------------------


def feats_of(row: dict[str, Any]) -> dict[str, tuple[float, bool]]:
    """The stored `{name: [value, present]}` bag back into the engine's `(value, present)`."""
    out: dict[str, tuple[float, bool]] = {}
    for name, entry in (row.get("feats") or {}).items():
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            try:
                out[str(name)] = (float(entry[0]), bool(entry[1]))
            except (TypeError, ValueError):
                continue
    return out


def scrubbed(listing: Listing) -> Listing:
    """E28's defensive second pass — scrub, THEN truncate, so a half-cut phone cannot survive."""
    from autodedup.export import scrub_description

    text = scrub_description(listing.description)
    if text and len(text) > DESCRIPTION_MAX:
        text = text[:DESCRIPTION_MAX]
    return replace(listing, description=text)


def captioned_blocks(
    judge: Any, r2: Any, images: list[Image], side: str, max_edge: int
) -> list[tuple[str, dict[str, Any]]]:
    """`("A-1 interior photo (kitchen), gallery position 2", <image block>)` per frame — an
    unlabelled facade is exactly how a shared building becomes a claimed unit match."""
    from toolkit.vision_images import image_block

    usable = [image for image in images if image.storage_path]
    captions = judge.image_captions(usable, side)
    return [
        (caption, image_block(r2, image.storage_path or "", max_edge))
        for caption, image in zip(captions, usable)
    ]


# --- budget ------------------------------------------------------------------------------


class Budget:
    """Shared spend gate. `reserve` is the pre-call check (E31), `settle` takes the real cost.

    A reservation HOLDS its estimate until the call settles. Checking only what is already
    billed lets every worker pass the gate on the same stale total, so the cap overshoots by
    `workers x cost_per_call` — which the first dry pass duly reproduced, reporting $1.0068
    spent against a $1.00 cap. A cap the run can exceed is a number nobody trusts twice."""

    def __init__(self, max_usd: float) -> None:
        self._lock = threading.Lock()
        self.max_usd = max_usd
        self.spent = 0.0
        self.committed = 0.0
        self.stopped = False
        self.fatal: str | None = None

    def reserve(self, estimate: float) -> bool:
        with self._lock:
            if self.stopped or self.fatal:
                return False
            if self.spent + self.committed + estimate > self.max_usd:
                self.stopped = True
                return False
            self.committed += estimate
            return True

    def settle(self, estimate: float, cost: float) -> None:
        with self._lock:
            self.committed = max(0.0, self.committed - estimate)
            self.spent += float(cost or 0.0)

    def release(self, estimate: float) -> None:
        """A reservation that bought nothing — the estimate goes back to the budget."""
        with self._lock:
            self.committed = max(0.0, self.committed - estimate)

    def abort(self, message: str) -> None:
        with self._lock:
            if self.fatal is None:
                self.fatal = message[:200]


class PodBudget(Budget):
    """The same gate for a machine billed by WALL CLOCK.

    A rented pod costs the same whether it is answering or idle, so a per-call estimate is the
    wrong meter entirely: `reserve` asks what the pod has cost SO FAR plus one more pair's
    worth, and stops the pass while that still fits. `spent` is never an accumulation of call
    prices — it is the clock, re-read on every settle, which is also why an `oss` judgement
    carries no cost of its own until the pod is terminated and the bill can be divided."""

    def __init__(self, max_usd: float, usd_per_hr: float, margin_s: float,
                 started: float | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        super().__init__(max_usd)
        self.usd_per_hr = float(usd_per_hr)
        self.margin_s = max(0.0, float(margin_s))
        self._clock = clock or CLOCK
        self.started = self._clock() if started is None else float(started)

    def pod_hours(self) -> float:
        return max(0.0, self._clock() - self.started) / 3600.0

    def pod_cost_usd(self, extra_s: float = 0.0) -> float:
        return (self.pod_hours() + max(0.0, extra_s) / 3600.0) * self.usd_per_hr

    def reserve(self, estimate: float) -> bool:
        with self._lock:
            if self.stopped or self.fatal:
                return False
            if self.pod_cost_usd(self.margin_s) > self.max_usd:
                self.stopped = True
                return False
            self.spent = self.pod_cost_usd()
            return True

    def settle(self, estimate: float, cost: float) -> None:
        with self._lock:
            self.spent = self.pod_cost_usd()

    def release(self, estimate: float) -> None:
        with self._lock:
            self.spent = self.pod_cost_usd()


# --- provider errors: four kinds, three different answers --------------------------------

ERROR_QUOTA: str = "quota"
ERROR_RATE_LIMIT: str = "rate_limit"
ERROR_FATAL: str = "fatal"
ERROR_TRANSIENT: str = "transient"
ERROR_UNKNOWN: str = "unknown"

# Phrases that mean "this account is out of allowance" whatever status carries them. DashScope
# answers an exhausted plan in OpenAI's own wording ("You exceeded your current quota, please
# check your plan and billing details"), which `toolkit.vision_batch.FATAL_MARKERS` classes as
# fatal — right for a single-provider image pass, fatal-to-the-wrong-thing for a three-vote gold
# judge whose other model family is answering perfectly well.
QUOTA_MARKERS: tuple[str, ...] = (
    "exceeded your current quota",
    "insufficient_quota",
    "insufficient_user_quota",
    "insufficient balance",
    "no credits remaining",
    "out of credits",
    "allocated quota",
    "arrearage",
    "billing details",
)
# The same meaning, but only once the STATUS already says money: "quota" turns up in plain
# rate-limit wording too ("quota exceeded for requests per minute"), and a burst limit must be
# waited out, not written off for the rest of the run.
QUOTA_BODY_MARKERS: tuple[str, ...] = (
    "quota", "billing", "insufficient", "credit", "payment", "balance",
)
AUTH_MARKERS: tuple[str, ...] = (
    "invalid_api_key", "invalid api key", "incorrect api key", "unauthorized",
    "authentication", "api_key is not set", "api key is not set",
    "model_not_found", "model not found", "unknown model",
)
RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "rate limit", "rate_limit", "too many requests", "throttl", "slow down",
)
TRANSIENT_MARKERS: tuple[str, ...] = (
    "timeout", "timed out", "connection", "temporarily unavailable", "overloaded",
    # `requests.raise_for_status()` wording, which carries no "HTTP nnn" for the regex to read.
    "server error", "bad gateway", "service unavailable",
)

_HTTP_STATUS = re.compile(r"http[ _/]?(\d{3})")


def http_status(message: str) -> int | None:
    """The provider's status code, read out of the message the providers deliberately put it in
    (`openai_compatible.complete`: `"{name} call failed: HTTP {status} {body}"`). A regex, not a
    substring test — a body that happens to contain "429" is not a rate limit."""
    match = _HTTP_STATUS.search(message.lower())
    return int(match.group(1)) if match else None


def classify_provider_error(exc: Exception | str) -> str:
    """Which of four failures this is — the whole point being that they are NOT one thing.

      quota       the account is out of allowance; every remaining call fails identically
      rate_limit  a burst limit; the same call succeeds after a wait
      fatal       a dead key or a model that does not exist; nothing here improves with time
      transient   5xx, timeout, dropped connection

    Only the caller knows what to do with each, because that depends on whether the arm that
    failed has a fallback: the gold tier's third vote does, the primary model does not.
    """
    low = str(exc or "").lower()
    status = http_status(low)
    if any(marker in low for marker in QUOTA_MARKERS):
        return ERROR_QUOTA
    if status == 402:
        return ERROR_QUOTA
    if status in (403, 429) and any(marker in low for marker in QUOTA_BODY_MARKERS):
        return ERROR_QUOTA
    if status in (401, 403, 404) or any(marker in low for marker in AUTH_MARKERS):
        return ERROR_FATAL
    if status == 429 or any(marker in low for marker in RATE_LIMIT_MARKERS):
        return ERROR_RATE_LIMIT
    if status is not None and 500 <= status <= 599:
        return ERROR_TRANSIENT
    if any(marker in low for marker in TRANSIENT_MARKERS):
        return ERROR_TRANSIENT
    return ERROR_UNKNOWN


# Worth another call after a sleep.
RETRYABLE: frozenset[str] = frozenset({ERROR_RATE_LIMIT, ERROR_TRANSIENT})
# Permanent for the REST OF THE RUN, not merely for this call: an arm that hits either of these
# hits it again on every remaining pair, so the answer is a switch, not a retry.
ARM_KILLING: frozenset[str] = frozenset({ERROR_QUOTA, ERROR_FATAL})


class SecondaryArm:
    """The gold tier's independent-family vote as a run-scoped switch.

    Judge run 35146813906 stopped a 2,240-call gold pass after 598 verdicts because DashScope
    answered "You exceeded your current quota" and the lane's fatal marker treats that as the
    PROVIDER failing — while `gpt-5-mini` was answering every call and the tier already carried
    a fallback vote (`GOLD_FALLBACK`) for precisely this. An exhausted account does not recover
    inside one pass: the arm goes down ONCE, every remaining pair takes the weaker vote, and the
    summary says so. Thread-safe because six workers hit the same 429 within the same second and
    the pair index the operator wants is the FIRST one, not the last thread to notice.
    """

    def __init__(self, name: str, model: str) -> None:
        self._lock = threading.Lock()
        self.name = name
        self.model = model
        self.disabled = False
        self.reason: str | None = None
        self.at_pair: int | None = None

    def is_disabled(self) -> bool:
        with self._lock:
            return self.disabled

    def disable(self, reason: str, at_pair: int | None) -> bool:
        """True only for the caller that actually put the arm down."""
        with self._lock:
            if self.disabled:
                return False
            self.disabled = True
            self.reason = str(reason)[:200]
            self.at_pair = at_pair
            return True

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            return {
                f"{self.name}_arm_disabled": self.disabled,
                f"{self.name}_arm_disabled_reason": self.reason,
                f"{self.name}_arm_disabled_at_pair": self.at_pair,
            }


# --- verdict -> row ----------------------------------------------------------------------


def _text_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    try:
        return [str(item) for item in value]
    except TypeError:
        return None


def judgement_params(
    *,
    lo: int,
    hi: int,
    judge_version: str,
    tier: str,
    model: str,
    verdict: Any,
    llm_call_id: int | None,
    cost_usd: float | None,
) -> dict[str, Any]:
    """Migration 528's `autodedup.judgements` columns, read off the judge's verdict object.

    Read by `getattr` rather than by field: `Verdict` and `GoldVerdict` carry the same seven
    answer fields under one contract, and a gold aggregate legitimately lacks some of them."""
    confidence = getattr(verdict, "confidence", None)
    return {
        "listing_lo": int(lo),
        "listing_hi": int(hi),
        "judge_version": judge_version,
        "tier": tier,
        "model": model,
        "verdict": str(getattr(verdict, "verdict", "") or ""),
        "confidence": float(confidence) if confidence is not None else None,
        "unit_discriminator": getattr(verdict, "unit_discriminator", None),
        "key_evidence": _text_list(getattr(verdict, "key_evidence", None)),
        "contradicting_evidence": _text_list(
            getattr(verdict, "contradicting_evidence", None)
        ),
        "developer_project_suspected": getattr(
            verdict, "developer_project_suspected", None
        ),
        "llm_call_id": int(llm_call_id) if llm_call_id is not None else None,
        "cost_usd": round(float(cost_usd), 6) if cost_usd is not None else None,
    }


# --- dependency seams (monkeypatched in tests; real in Actions) --------------------------


def llm_client(conn: Any) -> Any:
    from api.llm_client import LLMClient
    from api.providers.openai import OpenAIProvider
    from api.providers.qwen import QwenProvider

    return LLMClient(conn, providers={"openai": OpenAIProvider(), "qwen": QwenProvider()})


def oss_llm_client(conn: Any) -> Any:
    """The same `LLMClient`, plus the pod-backed provider. Constructed per worker and ALWAYS
    after `OSS_BASE_URL_ENV` is set — the provider reads the base URL at construction, and a
    client built before the pod exists would talk to nothing for the whole pass."""
    from api.llm_client import LLMClient
    from api.providers.oss import OssProvider

    return LLMClient(conn, providers={OSS_PROVIDER: OssProvider()})


def pod_module() -> Any:
    """`autodedup.oss_pod`, imported on first use so nothing but an `oss` run needs RunPod."""
    from autodedup import oss_pod as module

    return module


def runpod_client() -> Any:
    from scripts.runpod_client import RunPodClient

    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        raise SystemExit("RUNPOD_API_KEY must be set to rent a pod for tier oss")
    return RunPodClient(key)


def pod_start(parsed: JudgeArgs, out_dir: Path | None = None) -> Any:
    """Rent the GPU, wait for it to SERVE, and prove it with one forced-tool vision call.

    The smoke is not ceremony: a pod that boots but cannot turn a reply into a tool call fails
    every pair identically, and finding that out after 400 of them costs the whole rental. A
    failure anywhere in here tears the pod down ON THE WAY OUT — the lane's own `finally` only
    knows about a pod this function returned.

    Everything the model changes travels with it: a 30B model needs a bigger card, a bigger
    container disk for its weights and a vLLM image new enough to know its architecture, and
    each of those left at the 7B default is a rental that boots into a failure."""
    module = pod_module()
    client = runpod_client()
    # NAMED gpus are a PIN, not a hint: an operator names types for their price and their
    # memory, and the widened fallback rung on a type that is out of capacity is a 6x change
    # on an hourly-billed arm whose whole point is cost. Unpinned, the default preference
    # stays a ranking and any eligible box is fine.
    options: dict[str, Any] = (
        {"gpu_preference": parsed.oss_gpu, "strict_gpu": True} if parsed.oss_gpu else {}
    )
    if parsed.oss_cloud:
        options["cloud_types"] = parsed.oss_cloud
    if parsed.oss_image:
        options["image"] = parsed.oss_image
    if parsed.oss_tool_parser:
        options["tool_call_parser"] = parsed.oss_tool_parser
    if parsed.oss_dtype:
        options["dtype"] = parsed.oss_dtype
    if parsed.oss_disk_gb:
        options["container_disk_gb"] = parsed.oss_disk_gb
    if parsed.oss_max_model_len:
        options["max_model_len"] = parsed.oss_max_model_len
    if parsed.oss_max_usd_hr:
        options["max_price_per_hr"] = parsed.oss_max_usd_hr
    if parsed.oss_min_gpu_gb:
        options["min_memory_gb"] = parsed.oss_min_gpu_gb
    handle = module.launch_vllm_pod(client, model_id=parsed.oss_model, **options)
    if out_dir is not None:
        # Before the wait, not after it: a job killed from OUTSIDE runs no `finally` at all,
        # and the readiness wait is the longest window in the lane. The receipt is what the
        # `if: always()` cleanup step reaps.
        module.write_receipt(handle, Path(out_dir))
    try:
        module.wait_ready(handle, client=client)
        module.smoke_chat(handle)
    except BaseException:
        # BaseException, not Exception: a Ctrl-C or an Actions cancellation inside the smoke
        # window is not an `Exception`, and the lane's own `finally` only knows about a pod
        # this function RETURNED — so an interrupt here would walk out past both guards and
        # leave the pod billing. (`wait_ready` guards its own window the same way; a second
        # terminate is a 404, which `terminate_pod` treats as success.)
        module.terminate(handle, client=client)
        raise
    return handle


def pod_stop(pod: Any, out_dir: Path | None = None) -> None:
    """Terminate — the pod bills by the second until it is gone — and PROVE it did.

    `oss_pod.terminate` never raises by design (a teardown error must not mask the error that
    sent us here), so on its own it cannot tell this lane whether the machine actually died:
    the leak would show as one ERROR line in the Actions log while summary.json reported a
    clean pass. The second DELETE is the verification — `terminate_pod` treats an already-gone
    pod as success (404), so it returns quietly when the teardown worked and RAISES when the
    pod is still rented, which is the only way `pod_terminate_failed` reaches the artifact."""
    client = runpod_client()
    pod_module().terminate(pod, client=client)
    if pod_field(pod, "dry_run", False):
        return
    client.terminate_pod(str(pod_field(pod, "pod_id", "")))
    if out_dir is not None:
        # Only after the verifying DELETE returned: a receipt left behind on a pod that is
        # actually gone teaches the operator to ignore the reaper, which is the one signal
        # that costs money to miss.
        pod_module().clear_receipt(Path(out_dir))


def openai_base_url(base_url: str) -> str:
    """The pod handle carries the pod's HTTP ROOT (`…proxy.runpod.net`, which is where
    `oss_pod.wait_ready` polls `/v1/models`); an OpenAI-compatible client posts to
    `{base}/chat/completions` and therefore needs the API root. Normalising here — idempotent,
    so a handle that already ends in `/v1` is left alone — keeps the two ends of the seam from
    having to agree on which of them carries the suffix."""
    trimmed = base_url.rstrip("/")
    return trimmed if trimmed.endswith("/v1") else f"{trimmed}/v1"


def pod_field(pod: Any, name: str, default: Any = None) -> Any:
    value = pod.get(name) if isinstance(pod, dict) else getattr(pod, name, None)
    return default if value is None else value


def image_store() -> Any:
    from scraper import image_storage

    if not image_storage.is_configured():
        return None
    return image_storage.R2Client.from_env()


# --- the lane ----------------------------------------------------------------------------


@dataclass(slots=True)
class Counters:
    """Pairs and calls are counted SEPARATELY: one gold pair is three calls, so `done` alone
    can never tell whether every drawn pair was actually judged."""

    drawn: int = 0
    attempted: int = 0
    done: int = 0
    failed: int = 0
    skipped_budget: int = 0
    skipped_fatal: int = 0
    skipped_cached: int = 0
    pairs_processed: int = 0
    pairs_failed: int = 0
    gold_incomplete: int = 0
    gold_unstarted: int = 0
    billed_unparsed: int = 0
    persist_attempted: int = 0
    persist_failed: int = 0
    calls: dict[str, int] = field(default_factory=dict)
    verdicts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    call_ids: list[int] = field(default_factory=list)
    gold_flagged: int = 0
    # Fallback votes actually taken: how much of this pass's "independence" is really
    # gpt-5-mini agreeing with itself. A gold run where this is not 0 is a weaker run.
    weaker_votes: int = 0
    latency_s_total: float = 0.0
    latency_s_max: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "drawn": self.drawn,
            "attempted": self.attempted,
            "done": self.done,
            "failed": self.failed,
            "skipped_budget": self.skipped_budget,
            "skipped_fatal": self.skipped_fatal,
            "skipped_cached": self.skipped_cached,
            "pairs_processed": self.pairs_processed,
            "pairs_failed": self.pairs_failed,
            "gold_incomplete": self.gold_incomplete,
            "gold_unstarted": self.gold_unstarted,
            "billed_unparsed": self.billed_unparsed,
            "persist_attempted": self.persist_attempted,
            "persist_failed": self.persist_failed,
            "calls": dict(sorted(self.calls.items())),
            "verdicts": dict(sorted(self.verdicts.items())),
            "gold_flagged": self.gold_flagged,
            "weaker_votes": self.weaker_votes,
            "latency_s_mean": (
                round(self.latency_s_total / self.done, 3) if self.done else None
            ),
            "latency_s_max": round(self.latency_s_max, 3) or None,
            "tokens_in": self.tokens_in or None,
            "tokens_out": self.tokens_out or None,
            "errors": self.errors[:10],
            "errors_total": len(self.errors),
        }


def _cached_pairs(conn: Any, judge_version: str, tier: str,
                  jobs: list[PairJob]) -> set[tuple[int, int]]:
    params = {
        "judge_version": judge_version,
        "tier": tier,
        "los": [job.lo for job in jobs],
        "his": [job.hi for job in jobs],
    }
    with conn.cursor() as cur:
        cur.execute(JUDGEMENT_CACHED_SQL, params)
        return {(int(lo), int(hi)) for lo, hi in cur.fetchall()}


def _gold_pairs(conn: Any, judge_version: str) -> set[tuple[int, int]]:
    with conn.cursor() as cur:
        cur.execute(GOLD_PAIRS_SQL, {"judge_version": judge_version})
        return {(int(lo), int(hi)) for lo, hi in cur.fetchall()}


def _settle_pod_cost(conn: Any, judge_version: str, tier: str,
                     pairs: list[tuple[int, int]], cost_usd: float) -> None:
    with conn.cursor() as cur:
        cur.execute(JUDGEMENT_POD_COST_SQL, {
            "judge_version": judge_version,
            "tier": tier,
            "los": [lo for lo, _ in pairs],
            "his": [hi for _, hi in pairs],
            "cost_usd": round(cost_usd, 6),
        })
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def _settle_jsonl_cost(path: Path, tier: str, cost_usd: float) -> None:
    """The artifact is what `autodedup.compare` reads, so the share has to land there too —
    once, after the pod is gone. Rows written by other tiers are copied through untouched."""
    if not path.is_file():
        return
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("tier") == tier and row.get("cost_usd") is None:
            row["cost_usd"] = round(cost_usd, 6)
        rows.append(row)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    tmp.replace(path)


def _oss_pairs(path: Path) -> list[tuple[int, int]]:
    if not path.is_file():
        return []
    pairs: list[tuple[int, int]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("tier") == "oss" and row.get("verdict"):
            pairs.append((int(row["lo"]), int(row["hi"])))
    return pairs


def _settle_oss_costs(conn_factory: Callable[[], Any], counters: Counters,
                      judge_version: str, out_dir: Path, share: float) -> None:
    """Divide the pod's bill over what it produced, in the store and in the artifact."""
    path = out_dir / JUDGEMENTS_FILE
    pairs = _oss_pairs(path)
    if pairs:
        conn = conn_factory()
        try:
            _settle_pod_cost(conn, judge_version, "oss", pairs, share)
        except Exception as exc:  # noqa: BLE001 — a cost settle must not lose the verdicts
            counters.errors.append(f"pod cost settle: {type(exc).__name__}: {exc}"[:400])
        finally:
            _close(conn)
    _settle_jsonl_cost(path, "oss", share)


def _reconcile_cost(conn: Any, call_ids: list[int]) -> float | None:
    """E32: the ledger, not the arithmetic, is what the run reports as spent."""
    if not call_ids:
        return None
    with conn.cursor() as cur:
        cur.execute(JUDGEMENT_COST_SQL, {"ids": call_ids})
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def run_judge(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Path
) -> dict[str, Any]:
    parsed = parse_args(args)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    judge = judge_module()
    judge_version = parsed.judge_version or judge.JUDGE_VERSION

    started = time.monotonic()
    cohort_path = (
        Path(parsed.cohort)
        if parsed.cohort
        else download_cohort(parsed.export_run, out_dir / "artifact")
    )
    if not cohort_path.is_file():
        raise SystemExit(f"no cohort artifact at {cohort_path}")

    dataset = load(cohort_path)
    settings = load_engine_settings(parsed.settings)
    engine = harness.run_engine(dataset, settings, load_engine_model(parsed.model), out_dir)
    (out_dir / RUN_FILE).write_text(
        json.dumps(engine, indent=2, sort_keys=True), encoding="utf-8"
    )

    rows = harness.read_pairs(out_dir)
    gold_restricted = 0
    if parsed.pairs_from == "gold":
        # The comparison set, maximal per dollar: a pair with no gold row can be judged for
        # free and compared against nothing. Drawn BEFORE the sample, so the stratified floor
        # spends its quota inside the set that already has ground truth.
        conn = conn_factory()
        try:
            gold = _gold_pairs(conn, parsed.gold_version or judge_version)
        finally:
            _close(conn)
        before = len(rows)
        rows = [row for row in rows if (int(row["lo"]), int(row["hi"])) in gold]
        gold_restricted = before - len(rows)
        if not rows:
            raise SystemExit(
                f"pairs_from=gold: none of this pass's {before} pair(s) carry a gold row at "
                f"judge_version {parsed.gold_version or judge_version} — run the gold tier "
                "first, or name the version that holds the labels with gold_version="
            )
    if parsed.strata:
        rows = [
            row for row in rows
            if any(harness.judge_stratum(row).startswith(prefix) for prefix in parsed.strata)
        ]
    sample = harness.sample_pairs(rows, parsed.n, parsed.seed)
    sample["tier"] = parsed.tier
    sample["judge_version"] = judge_version
    sample["strata_filter"] = list(parsed.strata)
    (out_dir / SAMPLE_FILE).write_text(
        json.dumps(sample, indent=2, sort_keys=True), encoding="utf-8"
    )

    selected: list[dict[str, Any]] = list(sample["pairs"])
    if parsed.tier == "smoke":
        selected = selected[:SMOKE_PAIRS]
    plan = vote_plan(parsed.tier, parsed.oss_model)
    drawable = [
        row for row in selected
        if int(row["lo"]) in dataset.listings and int(row["hi"]) in dataset.listings
    ]
    jobs = [
        PairJob(
            lo=int(row["lo"]),
            hi=int(row["hi"]),
            row=row,
            stratum=harness.judge_stratum(row),
            votes=plan,
            index=index,
        )
        for index, row in enumerate(drawable)
    ]

    counters = Counters(drawn=len(jobs))
    # Gold's third vote is the only arm in the lane with somewhere to fall back to, so it is the
    # only one that can be switched off instead of stopping the pass.
    arm = SecondaryArm("qwen", MODEL_GOLD_THIRD)
    summary: dict[str, Any] = {
        "tier": parsed.tier,
        "judge_version": judge_version,
        "export_run": parsed.export_run or None,
        "cohort": str(cohort_path),
        "seed": parsed.seed,
        "n_requested": parsed.n,
        "n_selected": int(sample["n_selected"]),
        # The per-stratum floor is honoured before the target, so a wide cohort draws MORE than
        # `n` — and `n` is the operator's only cost dial, so the overshoot is published here
        # rather than discovered as an early budget stop.
        "sample_inflated_by_floor": max(0, int(sample["n_selected"]) - parsed.n),
        "workers": parsed.workers,
        "max_usd": parsed.max_usd,
        # What the DRAW would cost at the pre-flight rate, published before a call is made.
        # `n` is the operator's only cost dial and the per-stratum floor can multiply it by
        # ten, so "your budget pays for 15% of this draw" has to be readable at the top of the
        # artifact — not inferred afterwards from a `budget_stopped` flag.
        "est_cost_usd_for_draw": round(len(jobs) * min_budget_usd(parsed.tier), 4),
        "budget_covers_draw": (
            len(jobs) * min_budget_usd(parsed.tier) <= parsed.max_usd
        ),
        "dry_run": parsed.dry_run,
        "pairs_from": parsed.pairs_from,
        "gold_version": parsed.gold_version,
        "pairs_without_gold_dropped": gold_restricted,
        "sample_strata": sample["strata"],
        "engine": {
            key: engine[key]
            for key in ("n_listings", "pairs_scored", "pairs_stored", "zones",
                        "certificates", "band_width")
            if key in engine
        },
    }

    if parsed.tier == "oss":
        # Nothing about a rented machine can be priced before the GPU is picked, and a draw
        # estimate of `n x $0.25` would be a fiction the operator reads as a quote. The real
        # look-ahead happens once `usd_per_hr` is known, below, and is PUBLISHED there — it
        # does not refuse: the pass starts and `PodBudget` stops it mid-flight on the clock.
        summary["est_cost_usd_for_draw"] = None
        summary["budget_covers_draw"] = None
        summary["oss_model"] = parsed.oss_model
        summary["oss_gpu_requested"] = list(parsed.oss_gpu)
        summary["oss_cloud_requested"] = list(parsed.oss_cloud)
        summary["oss_image"] = parsed.oss_image
        summary["oss_est_s_per_pair"] = parsed.oss_s_per_pair

    if parsed.dry_run:
        summary.update(counters.to_json())
        summary.update(arm.to_json())
        summary["estimate"] = _estimate(judge, dataset, jobs, parsed, settings)
        summary["spent_usd"] = 0.0
        summary["budget_stopped"] = False
        summary["elapsed_s"] = round(time.monotonic() - started, 2)
        (out_dir / SUMMARY_FILE).write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
        )
        return summary

    if jobs and not parsed.refresh:
        conn = conn_factory()
        try:
            cached = _cached_pairs(conn, judge_version, parsed.tier, jobs)
        finally:
            _close(conn)
        if cached:
            counters.skipped_cached = sum(1 for job in jobs if (job.lo, job.hi) in cached)
            jobs = [job for job in jobs if (job.lo, job.hi) not in cached]

    budget: Budget = Budget(parsed.max_usd)
    client_factory = llm_client
    pod: Any = None
    pod_started = 0.0
    pod_cost = 0.0
    try:
        if parsed.tier == "oss" and jobs:
            boot_started = CLOCK()
            try:
                pod = pod_start(parsed, out_dir)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 — see below
                # The one path in this lane that can burn 25 minutes of GPU rental and produce
                # nothing: no capacity across the whole list, `wait_ready`'s deadline, a smoke
                # that never turned a reply into a tool call. `pod` is still None, so the
                # `finally` records nothing either — without this the operator gets a red run,
                # a real bill and an `out/` holding only run.json. Same shape as `_rails`:
                # write the artifact FIRST, raise second.
                summary["pod_boot_failed"] = f"{type(exc).__name__}: {exc}"[:400]
                summary["pod_boot_s"] = round(max(0.0, CLOCK() - boot_started), 2)
                summary.update(counters.to_json())
                summary["spent_usd"] = 0.0
                summary["spent_usd_source"] = "pod (boot failed, bill unknown)"
                summary["budget_stopped"] = False
                summary["elapsed_s"] = round(time.monotonic() - started, 2)
                (out_dir / SUMMARY_FILE).write_text(
                    json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
                )
                raise
            # The handle's own stamp, so the bill this lane publishes covers the same window
            # the pod module prices: boot, idle and judged pairs alike.
            pod_started = float(pod_field(pod, "started_at", CLOCK()))
            usd_per_hr = float(pod_field(pod, "usd_per_hr", 0.0) or 0.0)
            base_url = str(pod_field(pod, "base_url", "") or "")
            if not base_url:
                raise SystemExit("autodedup.oss_pod returned a handle with no base_url")
            # BEFORE any client is constructed (the workers build theirs inside `_dispatch`):
            # the provider reads its endpoint once, at construction.
            os.environ[OSS_BASE_URL_ENV] = openai_base_url(base_url)
            client_factory = oss_llm_client
            summary["pod_id"] = pod_field(pod, "pod_id")
            summary["gpu"] = pod_field(pod, "gpu") or pod_field(pod, "gpu_type_id")
            summary["cloud_type"] = pod_field(pod, "cloud_type")
            summary["usd_per_hr"] = usd_per_hr
            # The boot is ALREADY BILLED by the time this runs — `started_at` is stamped at
            # launch and `pod_start` has since waited out a weights load that routinely takes
            # 5-15 minutes, against a judging window of ~7 for a 200-pair draw. A look-ahead
            # counting only the pairs publishes a third of the bill and calls a draw covered
            # that the budget cannot cover; `pod_cost_usd` below prices the whole window, so
            # the two numbers have to meter the same clock. The boot share is published apart
            # so the first real pass can calibrate `oss_s_per_pair` against a measured judging
            # rate rather than against a number with a cold start folded into it.
            boot_s = max(0.0, CLOCK() - pod_started)
            estimate = (oss_est_usd(usd_per_hr, len(jobs), parsed.workers,
                                    parsed.oss_s_per_pair)
                        + usd_per_hr * boot_s / 3600.0)
            summary["pod_boot_s"] = round(boot_s, 2)
            summary["est_cost_usd_for_draw"] = round(estimate, 4)
            summary["budget_covers_draw"] = estimate <= parsed.max_usd
            budget = PodBudget(
                parsed.max_usd, usd_per_hr,
                # The duration of the ONE call being admitted, not its share: with W workers
                # judging in parallel, each call occupies `s_per_pair` of WALL clock, and a
                # margin of a Wth of that admits a call only a Wth of which fits under the cap.
                margin_s=parsed.oss_s_per_pair,
                started=pod_started,
            )
        _dispatch(judge, dataset, jobs, parsed, judge_version, budget, counters, arm,
                  conn_factory, out_dir, client_factory, settings)
    finally:
        if pod is not None:
            try:
                pod_stop(pod, out_dir)
            except Exception as exc:  # noqa: BLE001 — a failed teardown must still be visible
                counters.errors.append(f"pod terminate: {type(exc).__name__}: {exc}"[:400])
                summary["pod_terminate_failed"] = True
            pod_hours = max(0.0, CLOCK() - pod_started) / 3600.0
            pod_cost = pod_hours * float(summary.get("usd_per_hr") or 0.0)
            summary["pod_hours"] = round(pod_hours, 5)
            summary["pod_cost_usd"] = round(pod_cost, 6)
            # POD WALL clock per pair — the rental divided by what it produced, which with W
            # workers is roughly a Wth of one call's duration plus the boot share. It is NOT
            # the quantity `oss_s_per_pair` wants (that is ONE CALL's duration, which the
            # estimate then divides by `workers`): feeding this number back would understate
            # the next pass by about W times and make `budget_covers_draw` say yes to a draw
            # the cap cannot pay for. The calibration input is `latency_s_mean`; the name says
            # which clock this one is.
            summary["pod_wall_s_per_pair"] = (
                round(pod_hours * 3600.0 / counters.done, 2) if counters.done else None
            )

    spent = budget.spent
    summary["spent_usd_source"] = "in_process"
    if pod is not None:
        # The bill is the clock, and the clock is the only source: `llm_calls` records $0 for
        # every pod call (there is no per-token price to record), so reconciling against the
        # ledger here would publish a free run.
        spent = pod_cost
        summary["spent_usd_source"] = "pod"
        summary["cost_per_pair_usd"] = (
            round(pod_cost / counters.done, 6) if counters.done else None
        )
        if counters.done:
            _settle_oss_costs(conn_factory, counters, judge_version, out_dir,
                              pod_cost / counters.done)
    elif counters.call_ids:
        conn = conn_factory()
        try:
            ledger = _reconcile_cost(conn, counters.call_ids)
        except Exception as exc:  # noqa: BLE001 — a cost read must not lose the verdicts
            ledger = None
            counters.errors.append(f"cost reconcile: {type(exc).__name__}: {exc}")
        finally:
            _close(conn)
        # A ledger read that matches no row returns 0, which is indistinguishable from a free
        # run: substitute it only when it is plausible, and say so on the artifact when it is
        # not, rather than publishing $0.00 for a pass that was billed.
        if ledger is not None and (ledger > 0 or budget.spent == 0):
            spent = ledger
            summary["spent_usd_source"] = "llm_calls"
        elif ledger is not None:
            summary["spent_usd_source"] = (
                f"in_process (llm_calls summed 0 over {len(counters.call_ids)} call ids)"
            )

    summary.update(counters.to_json())
    summary.update(arm.to_json())
    summary["spent_usd"] = round(spent, 6)
    summary["spent_usd_in_process"] = round(budget.spent, 6)
    summary["budget_stopped"] = budget.stopped
    summary["fatal"] = budget.fatal
    summary["elapsed_s"] = round(time.monotonic() - started, 2)
    summary["mean_cost_usd"] = (
        round(spent / counters.done, 6) if counters.done else None
    )
    (out_dir / SUMMARY_FILE).write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    _rails(counters, budget, parsed, spent)
    return summary


def _rails(counters: Counters, budget: "Budget", parsed: JudgeArgs, spent: float) -> None:
    """Every way a paid lane can spend nothing, store nothing or lose work while exiting 0.

    All four fire AFTER the summary is written: the artifact is the evidence, and an exception
    thrown before it lands leaves the operator with a red run and no numbers."""
    if budget.fatal:
        raise SystemExit(
            f"judge: the provider failed fatally after {counters.done} verdict(s) — "
            f"{counters.skipped_fatal} call(s) never launched: {budget.fatal}"
        )
    if counters.drawn and not counters.done and counters.skipped_cached < counters.drawn:
        # E31 as an exit code: a lane that drew work and read back nothing is a FAILED lane,
        # not a cheap one — whether it failed its calls or refused to launch them at all.
        raise SystemExit(
            f"judge: drew {counters.drawn} pair(s), attempted {counters.attempted} call(s), "
            f"0 done — {counters.failed} failed, {counters.skipped_budget} skipped on budget "
            f"(spent ${spent:.4f} of ${parsed.max_usd:.2f}, one {parsed.tier} pair needs "
            f"~${min_budget_usd(parsed.tier):.5f}) — see {SUMMARY_FILE}"
        )
    if counters.persist_attempted and counters.persist_failed == counters.persist_attempted:
        first = counters.errors[0] if counters.errors else "?"
        raise SystemExit(
            f"judge: {counters.done} verdict(s) were paid for and all "
            f"{counters.persist_attempted} store write(s) failed — the verdicts are in "
            f"{JUDGEMENTS_FILE}; check migration 528 is applied. First error: {first}"
        )
    accounted = counters.pairs_processed + counters.pairs_failed + counters.skipped_cached
    if counters.drawn and accounted < counters.drawn:
        raise SystemExit(
            f"judge: {counters.drawn - accounted} of {counters.drawn} drawn pair(s) were "
            f"never judged and never accounted for — see {SUMMARY_FILE}"
        )


def _close(conn: Any) -> None:
    close = getattr(conn, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 — a closed-connection error is not a result
            pass


def _estimate(judge: Any, dataset: Any, jobs: list[PairJob],
              parsed: JudgeArgs, settings: Settings | None = None) -> dict[str, Any]:
    """`dry_run=1`: build every prompt, count what it would cost, call nothing."""
    chars = 0
    images = 0
    calls = 0
    cost = 0.0
    for job in jobs:
        la, lb = dataset.listings[job.lo], dataset.listings[job.hi]
        digests, evidence = _inputs(judge, job, la, lb, settings)
        for vote in job.votes:
            calls += 1
            cost += vote.est_usd
            if vote.n_images:
                picked_a, picked_b = judge.select_images(
                    la, dataset.images(job.lo), lb, dataset.images(job.hi),
                    feats_of(job.row), vote.n_images, vote.strategy,
                )
                images += len(picked_a) + len(picked_b)
            messages = judge.build_messages(
                job.row, digests, evidence, [], [], vote.prompt_tier or vote.tier
            )
            # The system prompt and the tool schema ride on every call but are passed
            # separately at call time — omitted here, the token line understates each call by
            # ~500 and stops being a check on the flat cost table above.
            chars += (
                len(json.dumps(messages, ensure_ascii=False, default=str))
                + len(judge.SYSTEM_PROMPT)
                + len(json.dumps(judge.TOOL_SCHEMA, ensure_ascii=False, default=str))
            )
    tokens = chars // EST_CHARS_PER_TOKEN + images * EST_TOKENS_PER_IMAGE
    return {
        "pairs": len(jobs),
        "calls": calls,
        "images": images,
        "prompt_chars": chars,
        "est_input_tokens": tokens,
        "est_cost_usd": round(cost, 4),
        "est_cost_per_pair_usd": round(cost / len(jobs), 5) if jobs else 0.0,
        "max_usd": parsed.max_usd,
        "min_usd": round(min_budget_usd(parsed.tier), 5),
        "fits_budget": cost <= parsed.max_usd,
    }


def pin_of(judge: Any, listing: Listing) -> Any:
    location = listing.location
    return judge.Pin(
        grain=location.granularity,
        rank=location.granularity_rank,
        radius_m=uncertainty_radius_m(
            location.granularity_rank, location.uncertainty_radius_m
        ),
    )


def pin_distance_m(la: Listing, lb: Listing) -> float | None:
    """Metres between the two pins, but ONLY under E16's precision gate that `dist_norm` itself
    uses: below street grain the coordinate is an administrative centroid, so the number would
    be the distance between two town halls presented to the model as the distance between two
    flats."""
    a, b = la.location, lb.location
    if not (a.has_point() and b.has_point()):
        return None
    if a.granularity_rank is None or b.granularity_rank is None:
        return None
    if a.granularity_rank < STREET_GRAIN_RANK or b.granularity_rank < STREET_GRAIN_RANK:
        return None
    return haversine_m(float(a.lat), float(a.lon), float(b.lat), float(b.lon))


def _inputs(
    judge: Any, job: PairJob, la: Listing, lb: Listing, settings: Settings | None = None
) -> tuple[Any, str]:
    """The prompt's view of the pair, built through the SAME settings row the engine ran.

    `attribute_conflicts` reads `vocabulary_attr_keys`: a slot the engine refuses to count must
    not be listed to the judge as a conflict either, or the labels come back arguing against a
    contradiction the engine never raised."""
    digests = (judge.listing_digest(scrubbed(la)), judge.listing_digest(scrubbed(lb)))
    evidence = judge.evidence_digest(
        feats_of(job.row),
        job.row.get("probes") or [],
        job.row.get("families") or [],
        job.row.get("block"),
        attr_conflicts=attribute_conflicts(la, lb, settings=settings),
        distance_m=pin_distance_m(la, lb),
        pins=(pin_of(judge, la), pin_of(judge, lb)),
    )
    return digests, evidence


def _dispatch(
    judge: Any,
    dataset: Any,
    jobs: list[PairJob],
    parsed: JudgeArgs,
    judge_version: str,
    budget: Budget,
    counters: Counters,
    arm: SecondaryArm,
    conn_factory: Callable[[], Any],
    out_dir: Path,
    client_factory: Callable[[Any], Any] = None,  # type: ignore[assignment]
    settings: Settings | None = None,
) -> None:
    client_factory = client_factory or llm_client
    work: queue.Queue = queue.Queue()
    for job in jobs:
        work.put(job)
    stats_lock = threading.Lock()
    write_lock = threading.Lock()
    # Truncating, not appending: this file is the backstop for verdicts a failed DB write lost,
    # and a backstop holding two passes' rows interleaved is one nobody can reconcile.
    sink = (out_dir / JUDGEMENTS_FILE).open("w", encoding="utf-8")
    r2 = (image_store()
          if any(vote.n_images for vote in vote_plan(parsed.tier, parsed.oss_model))
          else None)

    def _emit(record: dict[str, Any]) -> None:
        with write_lock:
            sink.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            sink.flush()

    def _worker() -> None:
        conn = conn_factory()
        try:
            client = client_factory(conn)
            while True:
                try:
                    job = work.get_nowait()
                except queue.Empty:
                    return
                try:
                    _run_job(judge, dataset, job, parsed, judge_version, budget, counters,
                             arm, conn, client, r2, stats_lock, _emit, settings)
                except Exception as exc:  # noqa: BLE001 — see below
                    # Everything outside the call itself lives here too: a digest, an image
                    # selection, a JSONL write. Unguarded, one malformed listing kills the
                    # THREAD, its queued pairs vanish, no counter moves and the lane exits 0 —
                    # at six workers that silently removes a sixth of the capacity mid-pass.
                    with stats_lock:
                        counters.pairs_failed += 1
                        counters.errors.append(
                            f"{job.lo}x{job.hi}: {type(exc).__name__}: {exc}"[:400]
                        )
                else:
                    with stats_lock:
                        counters.pairs_processed += 1
        finally:
            _close(conn)

    threads = [
        threading.Thread(target=_worker, name=f"judge-{index}", daemon=True)
        for index in range(max(1, min(parsed.workers, max(1, len(jobs)))))
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sink.close()


def _run_job(
    judge: Any,
    dataset: Any,
    job: PairJob,
    parsed: JudgeArgs,
    judge_version: str,
    budget: Budget,
    counters: Counters,
    arm: SecondaryArm,
    conn: Any,
    client: Any,
    r2: Any,
    lock: threading.Lock,
    emit: Callable[[dict[str, Any]], None],
    settings: Settings | None = None,
) -> None:
    la, lb = dataset.listings[job.lo], dataset.listings[job.hi]
    digests, evidence = _inputs(judge, job, la, lb, settings)
    votes: list[Any] = []
    used: list[Vote] = []
    results: list[dict[str, Any]] = []
    plan = list(job.votes)
    index = 0
    while index < len(plan):
        vote = plan[index]
        index += 1
        secondary = parsed.tier == "gold" and vote.model == arm.model
        if secondary and arm.is_disabled():
            # The arm is out for the rest of the pass: take the weaker vote straight away rather
            # than launch a call whose only possible outcomes are another 429 and another line
            # in `errors`. Budget is never reserved for a call that is not made.
            plan.append(GOLD_FALLBACK)
            continue
        if not budget.reserve(vote.est_usd):
            with lock:
                # A refusal after `abort` is the provider stopping the run, not the budget;
                # folding the two together reports a dead key as a cheap pass.
                if budget.fatal:
                    counters.skipped_fatal += 1
                else:
                    counters.skipped_budget += 1
            continue
        with lock:
            counters.attempted += 1
            counters.calls[vote.tier] = counters.calls.get(vote.tier, 0) + 1
        call_started = time.monotonic()
        try:
            parsed_vote, response = _call_with_retry(
                judge, dataset, job, la, lb, digests, evidence, vote, r2, client
            )
        except Exception as exc:  # noqa: BLE001 — one pair must not kill the pass
            message = f"{job.lo}x{job.hi} {vote.tier}/{vote.model}: {type(exc).__name__}: {exc}"
            # A response that arrived and then failed to PARSE was billed all the same. Dropping
            # its cost hides real spend from both the cap and the summary, which is the E31
            # failure wearing the opposite sign.
            billed = getattr(exc, JUDGE_BILLED, None)
            if billed is not None:
                cost, call_id = billed
                budget.settle(vote.est_usd, cost)
                with lock:
                    counters.billed_unparsed += 1
                    if call_id is not None:
                        counters.call_ids.append(int(call_id))
            else:
                budget.release(vote.est_usd)
            with lock:
                counters.failed += 1
                counters.errors.append(message[:400])
            kind = classify_provider_error(exc)
            if secondary:
                # The independence arm NEVER stops the run — it is the one vote in the lane with
                # somewhere to fall back to. Whatever went wrong, this pair takes a third
                # gpt-5-mini vote with a shuffled image order, FLAGGED as the weaker
                # independence check (JUDGE SPEC §2). What differs is how long the damage lasts:
                # a quota exhaustion or a dead key is permanent for the pass and puts the ARM
                # down (run 35146813906 stopped 1,642 calls short on exactly this), while a rate
                # limit or a 5xx — already waited out by `_call_with_retry` — costs this pair
                # its independent vote and nothing more.
                if kind in ARM_KILLING:
                    arm.disable(f"{kind}: {exc}", job.index)
                plan.append(GOLD_FALLBACK)
                continue
            if kind in ARM_KILLING or is_fatal(str(exc)):
                # The PRIMARY model, which has no second family behind it: a dead key, a missing
                # model or an exhausted account ends the pass rather than failing every
                # remaining pair one expensive call at a time.
                budget.abort(str(exc))
                with lock:
                    counters.skipped_fatal += len(plan) - index
                return
            continue
        # The MODEL's latency, not the lane's: `_call_with_retry` sleeps up to 14s backing off
        # a 429, which is the steady state on the paid tiers at six workers and unheard of on a
        # rented pod with no rate limit — charging that sleep to `latency_s` would bias the very
        # yardstick the oss arm is measured against. `duration_ms` is the call itself; the wall
        # span stays beside it so a pass full of retries is still visible.
        latency_s = float(getattr(response, "duration_ms", 0) or 0) / 1000.0
        pair_wall_s = time.monotonic() - call_started
        cost = float(getattr(response, "cost_usd", 0.0) or 0.0)
        call_id = getattr(response, "llm_call_id", None)
        # A rented pod's call has no price of its own: the bill is the clock, divided once the
        # pod is gone. NULL says "not yet known" where 0.0 would say "free".
        stored_cost: float | None = None if vote.tier == "oss" else cost
        budget.settle(vote.est_usd, cost)
        with lock:
            counters.done += 1
            if vote.weaker:
                counters.weaker_votes += 1
            counters.latency_s_total += latency_s
            counters.latency_s_max = max(counters.latency_s_max, latency_s)
            counters.tokens_in += int(getattr(response, "input_tokens", 0) or 0)
            counters.tokens_out += int(getattr(response, "output_tokens", 0) or 0)
            name = str(getattr(parsed_vote, "verdict", "") or "?")
            counters.verdicts[name] = counters.verdicts.get(name, 0) + 1
            if call_id is not None:
                counters.call_ids.append(int(call_id))
        votes.append(parsed_vote)
        used.append(vote)
        results.append({
            "lo": job.lo, "hi": job.hi, "stratum": job.stratum,
            "tier": vote.tier, "model": vote.model, "strategy": vote.strategy,
            "weaker": vote.weaker, "cost_usd": stored_cost, "llm_call_id": call_id,
            "latency_s": round(latency_s, 3),
            "pair_wall_s": round(pair_wall_s, 3),
            "verdict": _verdict_json(parsed_vote),
        })
        if parsed.tier != "gold":
            _persist(conn, judgement_params(
                lo=job.lo, hi=job.hi, judge_version=judge_version, tier=vote.tier,
                model=vote.model, verdict=parsed_vote, llm_call_id=call_id,
                cost_usd=stored_cost,
            ), counters, lock)

    for record in results:
        emit(record)

    if parsed.tier != "gold":
        return

    total = sum(float(row["cost_usd"] or 0.0) for row in results)
    if len(votes) < GOLD_VOTES:
        # `aggregate_gold` calls ONE surviving vote unanimous at confidence 1.0, and the table
        # has no column that would give it away. Gold IS the program's ground truth, so a
        # short plan writes NOTHING to it — the paid votes stay in the jsonl.
        #
        # A pair with ZERO votes is counted apart: a budget stop leaves hundreds of those, and
        # folding them into `gold_incomplete` reports money thrown away that was never spent.
        with lock:
            if votes:
                counters.gold_incomplete += 1
            else:
                counters.gold_unstarted += 1
        emit({
            "lo": job.lo, "hi": job.hi, "stratum": job.stratum, "tier": "gold",
            "n_votes": len(votes), "cost_usd": total, "incomplete": True,
            "models": [vote.model for vote in used],
        })
        return

    aggregate = judge.aggregate_gold(votes)
    if getattr(aggregate, "flagged", False):
        with lock:
            counters.gold_flagged += 1
    models = "+".join(vote.model for vote in used)
    if any(vote.weaker for vote in used):
        models = f"{models} (weaker)"
    first_id = next((row["llm_call_id"] for row in results if row["llm_call_id"]), None)
    _persist(conn, judgement_params(
        lo=job.lo, hi=job.hi, judge_version=judge_version, tier="gold",
        model=models, verdict=aggregate, llm_call_id=first_id, cost_usd=total,
    ), counters, lock)
    emit({
        "lo": job.lo, "hi": job.hi, "stratum": job.stratum, "tier": "gold",
        "model": models, "n_votes": len(votes), "cost_usd": total,
        "weaker": any(vote.weaker for vote in used),
        "verdict": _verdict_json(aggregate),
    })


def _call_with_retry(
    judge: Any, dataset: Any, job: PairJob, la: Listing, lb: Listing,
    digests: Any, evidence: str, vote: Vote, r2: Any, client: Any,
) -> tuple[Any, Any]:
    """Budget is reserved once, per CALL — a retried 429 bought nothing and is not billed."""
    for attempt in range(len(RETRY_BACKOFF_S) + 1):
        try:
            return _one_call(judge, dataset, job, la, lb, digests, evidence, vote, r2, client)
        except Exception as exc:  # noqa: BLE001 — classified, then re-raised or waited out
            last = attempt == len(RETRY_BACKOFF_S)
            # A quota exhaustion is not a burst: retrying it buys three more identical 429s
            # and fourteen seconds of a worker. Only rate limits and 5xx/timeouts earn the sleep.
            if last or classify_provider_error(exc) not in RETRYABLE:
                raise
            RETRY_SLEEP(RETRY_BACKOFF_S[attempt])
    raise RuntimeError("unreachable")


def _reversed_halves(blocks: list[Any], split: int) -> list[Any]:
    """Reverse the paired head and the unpaired tail separately — the presentation order
    changes, the pairing does not."""
    return blocks[:split][::-1] + blocks[split:][::-1]


def _one_call(
    judge: Any, dataset: Any, job: PairJob, la: Listing, lb: Listing,
    digests: Any, evidence: str, vote: Vote, r2: Any, client: Any,
) -> tuple[Any, Any]:
    from toolkit.vision_images import COMPARISON_MAX_EDGE

    blocks_a: list[tuple[str, dict[str, Any]]] = []
    blocks_b: list[tuple[str, dict[str, Any]]] = []
    paired = 0
    if vote.n_images:
        if r2 is None:
            raise RuntimeError("R2 is not configured; the vision tiers cannot read images")
        picked_a, picked_b = judge.select_images(
            la, dataset.images(job.lo), lb, dataset.images(job.hi),
            feats_of(job.row), vote.n_images, vote.strategy,
        )
        blocks_a = captioned_blocks(judge, r2, picked_a, "A", COMPARISON_MAX_EDGE)
        blocks_b = captioned_blocks(judge, r2, picked_b, "B", COMPARISON_MAX_EDGE)
        paired = judge.paired_prefix(picked_a, picked_b)
        if vote.shuffle:
            # Reversed WITHIN the paired block and within the tail, not across them: a plain
            # reverse would move the room pairs to the end and silently unpair the j2
            # presentation, so the weaker vote would differ from G1 in two things at once.
            blocks_a = _reversed_halves(blocks_a, paired)
            blocks_b = _reversed_halves(blocks_b, paired)
    messages = judge.build_messages(
        job.row, digests, evidence, blocks_a, blocks_b, vote.prompt_tier or vote.tier,
        paired,
    )
    extra: dict[str, Any] = {"provider": vote.provider} if vote.provider else {}
    response = client.call(
        called_for=vote.called_for,
        model=vote.model,
        messages=messages,
        system=judge.SYSTEM_PROMPT,
        tools=[judge.TOOL_SCHEMA],
        tool_choice=judge.TOOL_NAME,
        max_tokens=MAX_TOKENS,
        **extra,
    )
    try:
        calls = list(getattr(response, "tool_calls", None) or [])
        wanted = [call for call in calls if call.get("name") == judge.TOOL_NAME] or calls
        if not wanted:
            raise ValueError(f"no {judge.TOOL_NAME} tool call in the response")
        return judge.parse_verdict(wanted[0].get("input") or {}), response
    except Exception as exc:  # noqa: BLE001 — re-raised; this only attaches what it cost
        try:
            setattr(exc, JUDGE_BILLED, (
                float(getattr(response, "cost_usd", 0.0) or 0.0),
                getattr(response, "llm_call_id", None),
            ))
        except AttributeError:
            pass
        raise


def _verdict_json(verdict: Any) -> dict[str, Any]:
    as_json = getattr(verdict, "to_json", None)
    if callable(as_json):
        return dict(as_json())
    return {
        name: getattr(verdict, name, None)
        for name in (
            "verdict", "confidence", "deal_or_category_conflict", "unit_discriminator",
            "key_evidence", "contradicting_evidence", "developer_project_suspected",
            "flagged", "unanimous",
        )
        if hasattr(verdict, name)
    }


def _persist(conn: Any, params: dict[str, Any], counters: Counters,
             lock: threading.Lock) -> None:
    with lock:
        counters.persist_attempted += 1
    try:
        with conn.cursor() as cur:
            cur.execute(JUDGEMENT_UPSERT_SQL, params)
        commit = getattr(conn, "commit", None)
        if callable(commit):
            commit()
    except Exception as exc:  # noqa: BLE001 — a store failure must not lose the paid verdict
        with lock:
            counters.persist_failed += 1
            counters.errors.append(
                f"persist {params['listing_lo']}x{params['listing_hi']}: "
                f"{type(exc).__name__}: {exc}"[:400]
            )
