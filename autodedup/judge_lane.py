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
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from autodedup import harness
from autodedup.dataset import Image, Listing, load
from autodedup.judge_sql import (
    JUDGEMENT_CACHED_SQL,
    JUDGEMENT_COST_SQL,
    JUDGEMENT_UPSERT_SQL,
)
from autodedup.model import hand_initialised
from autodedup.settings import Settings
from toolkit.vision_batch import is_fatal

TIERS: tuple[str, ...] = ("smoke", "text", "vision", "gold")

MODEL_TEXT: str = "gpt-5.6-luna"
MODEL_VISION: str = "gpt-5-mini"
MODEL_GOLD_THIRD: str = "qwen3-vl-30b-a3b-instruct"

CALLED_FOR: dict[str, str] = {
    "text": "autodedup_judge_text",
    "vision": "autodedup_judge_vision",
    "gold": "autodedup_judge_gold",
}

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

# A 429 is the steady state at six workers pushing eight images a call, and it is not a verdict:
# retry it before it shrinks the sample (or, on gold, silently downgrades the independent model
# family to a gpt-5-mini self-consistency vote).
RETRY_BACKOFF_S: tuple[float, ...] = (2.0, 8.0)
RETRY_SLEEP: Callable[[float], None] = time.sleep

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


@dataclass(slots=True)
class PairJob:
    lo: int
    hi: int
    row: dict[str, Any]
    stratum: str
    votes: tuple[Vote, ...]


def vote_plan(tier: str) -> tuple[Vote, ...]:
    """The tiers of E26 and the three gold votes of the JUDGE SPEC §2, as data."""
    text = Vote("text", MODEL_TEXT, CALLED_FOR["text"], 0, "matched_first",
                EST_COST_USD["text"])
    vision = Vote("vision", MODEL_VISION, CALLED_FOR["vision"], IMAGES_VISION, "matched_first",
                  EST_COST_USD["vision"])
    if tier == "text":
        return (text,)
    if tier == "vision":
        return (vision,)
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
    return sum(vote.est_usd for vote in vote_plan(tier))


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
    engine = harness.run_engine(dataset, Settings(), hand_initialised(), out_dir)
    (out_dir / RUN_FILE).write_text(
        json.dumps(engine, indent=2, sort_keys=True), encoding="utf-8"
    )

    rows = harness.read_pairs(out_dir)
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
    plan = vote_plan(parsed.tier)
    jobs = [
        PairJob(
            lo=int(row["lo"]),
            hi=int(row["hi"]),
            row=row,
            stratum=harness.judge_stratum(row),
            votes=plan,
        )
        for row in selected
        if int(row["lo"]) in dataset.listings and int(row["hi"]) in dataset.listings
    ]

    counters = Counters(drawn=len(jobs))
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
        "sample_strata": sample["strata"],
        "engine": {
            key: engine[key]
            for key in ("n_listings", "pairs_scored", "pairs_stored", "zones",
                        "certificates", "band_width")
            if key in engine
        },
    }

    if parsed.dry_run:
        summary.update(counters.to_json())
        summary["estimate"] = _estimate(judge, dataset, jobs, parsed)
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

    budget = Budget(parsed.max_usd)
    _dispatch(judge, dataset, jobs, parsed, judge_version, budget, counters,
              conn_factory, out_dir)

    spent = budget.spent
    summary["spent_usd_source"] = "in_process"
    if counters.call_ids:
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
              parsed: JudgeArgs) -> dict[str, Any]:
    """`dry_run=1`: build every prompt, count what it would cost, call nothing."""
    chars = 0
    images = 0
    calls = 0
    cost = 0.0
    for job in jobs:
        la, lb = dataset.listings[job.lo], dataset.listings[job.hi]
        digests, evidence = _inputs(judge, job, la, lb)
        for vote in job.votes:
            calls += 1
            cost += vote.est_usd
            if vote.n_images:
                picked_a, picked_b = judge.select_images(
                    la, dataset.images(job.lo), lb, dataset.images(job.hi),
                    feats_of(job.row), vote.n_images, vote.strategy,
                )
                images += len(picked_a) + len(picked_b)
            messages = judge.build_messages(job.row, digests, evidence, [], [], vote.tier)
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


def _inputs(judge: Any, job: PairJob, la: Listing, lb: Listing) -> tuple[Any, str]:
    digests = (judge.listing_digest(scrubbed(la)), judge.listing_digest(scrubbed(lb)))
    evidence = judge.evidence_digest(
        feats_of(job.row),
        job.row.get("probes") or [],
        job.row.get("families") or [],
        job.row.get("block"),
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
    conn_factory: Callable[[], Any],
    out_dir: Path,
) -> None:
    work: queue.Queue = queue.Queue()
    for job in jobs:
        work.put(job)
    stats_lock = threading.Lock()
    write_lock = threading.Lock()
    # Truncating, not appending: this file is the backstop for verdicts a failed DB write lost,
    # and a backstop holding two passes' rows interleaved is one nobody can reconcile.
    sink = (out_dir / JUDGEMENTS_FILE).open("w", encoding="utf-8")
    r2 = image_store() if any(vote.n_images for vote in vote_plan(parsed.tier)) else None

    def _emit(record: dict[str, Any]) -> None:
        with write_lock:
            sink.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            sink.flush()

    def _worker() -> None:
        conn = conn_factory()
        try:
            client = llm_client(conn)
            while True:
                try:
                    job = work.get_nowait()
                except queue.Empty:
                    return
                try:
                    _run_job(judge, dataset, job, parsed, judge_version, budget, counters,
                             conn, client, r2, stats_lock, _emit)
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
    conn: Any,
    client: Any,
    r2: Any,
    lock: threading.Lock,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    la, lb = dataset.listings[job.lo], dataset.listings[job.hi]
    digests, evidence = _inputs(judge, job, la, lb)
    votes: list[Any] = []
    used: list[Vote] = []
    results: list[dict[str, Any]] = []
    plan = list(job.votes)
    index = 0
    while index < len(plan):
        vote = plan[index]
        index += 1
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
            if is_fatal(str(exc)):
                budget.abort(str(exc))
                with lock:
                    counters.skipped_fatal += len(plan) - index
                return
            if (parsed.tier == "gold" and vote.model == MODEL_GOLD_THIRD
                    and not is_transient(exc)):
                # The genuinely different model family is unavailable (not merely rate-limited,
                # which `_call_with_retry` has already waited out): fall back to a third
                # gpt-5-mini vote with a shuffled image order and FLAG the pair as a weaker
                # independence check (JUDGE SPEC §2).
                plan.append(GOLD_FALLBACK)
            continue
        cost = float(getattr(response, "cost_usd", 0.0) or 0.0)
        call_id = getattr(response, "llm_call_id", None)
        budget.settle(vote.est_usd, cost)
        with lock:
            counters.done += 1
            name = str(getattr(parsed_vote, "verdict", "") or "?")
            counters.verdicts[name] = counters.verdicts.get(name, 0) + 1
            if call_id is not None:
                counters.call_ids.append(int(call_id))
        votes.append(parsed_vote)
        used.append(vote)
        results.append({
            "lo": job.lo, "hi": job.hi, "stratum": job.stratum,
            "tier": vote.tier, "model": vote.model, "strategy": vote.strategy,
            "weaker": vote.weaker, "cost_usd": cost, "llm_call_id": call_id,
            "verdict": _verdict_json(parsed_vote),
        })
        if parsed.tier != "gold":
            _persist(conn, judgement_params(
                lo=job.lo, hi=job.hi, judge_version=judge_version, tier=vote.tier,
                model=vote.model, verdict=parsed_vote, llm_call_id=call_id, cost_usd=cost,
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


def is_transient(exc: Exception) -> bool:
    """The provider's own rate-limit / 5xx / timeout classifier, imported lazily so the lane
    registry does not pull the HTTP providers in."""
    try:
        from api.providers.openai import _is_transient
    except Exception:  # noqa: BLE001 — a missing provider module is not a transient failure
        return False
    return bool(_is_transient(exc))


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
            if last or is_fatal(str(exc)) or not is_transient(exc):
                raise
            RETRY_SLEEP(RETRY_BACKOFF_S[attempt])
    raise RuntimeError("unreachable")


def _one_call(
    judge: Any, dataset: Any, job: PairJob, la: Listing, lb: Listing,
    digests: Any, evidence: str, vote: Vote, r2: Any, client: Any,
) -> tuple[Any, Any]:
    from toolkit.vision_images import COMPARISON_MAX_EDGE

    blocks_a: list[tuple[str, dict[str, Any]]] = []
    blocks_b: list[tuple[str, dict[str, Any]]] = []
    if vote.n_images:
        if r2 is None:
            raise RuntimeError("R2 is not configured; the vision tiers cannot read images")
        picked_a, picked_b = judge.select_images(
            la, dataset.images(job.lo), lb, dataset.images(job.hi),
            feats_of(job.row), vote.n_images, vote.strategy,
        )
        blocks_a = captioned_blocks(judge, r2, picked_a, "A", COMPARISON_MAX_EDGE)
        blocks_b = captioned_blocks(judge, r2, picked_b, "B", COMPARISON_MAX_EDGE)
        if vote.shuffle:
            blocks_a.reverse()
            blocks_b.reverse()
    messages = judge.build_messages(
        job.row, digests, evidence, blocks_a, blocks_b, vote.tier
    )
    response = client.call(
        called_for=vote.called_for,
        model=vote.model,
        messages=messages,
        system=judge.SYSTEM_PROMPT,
        tools=[judge.TOOL_SCHEMA],
        tool_choice=judge.TOOL_NAME,
        max_tokens=MAX_TOKENS,
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
