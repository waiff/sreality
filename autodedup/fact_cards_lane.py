"""`mode=facts` — fill one UNIT IDENTITY CARD per LISTING and write them as a run artifact.

The whole pass in one sentence: read a committed list of listing ids, pull each listing's
advert text with the export lane's OWN SQL, scrub it, ask one model for one structured card
per listing under a forced tool, and write `out/cards.jsonl` plus a summary carrying token
counts and the cost the provider actually billed.

Why this is not the judge, mechanically:

  * O(LISTINGS), not O(pairs). A card is a statement about ONE advert, so it is cacheable for
    ever on (listing_id, content hash, prompt version) — the property that makes the whole
    approach affordable at 180,000 new listings a month. The judge's per-pair question can
    never be cached that way.
  * The model never decides a merge. `fact_cards.card_conflicts` compares two cards in plain
    code afterwards, and a conflict needs both sides non-null. That is deliberate: LLM judges
    asked "same or different?" per pair were MEASURED on this programme's cohorts and refused
    — they answer `same` across development twins, the one hazard the operator ranked first.

This experiment writes NO table and needs NO migration: the cards are the artifact. Every call
still goes through `LLMClient.call`, so each lands one `llm_calls` row and the spend is read
back from there (E32), never extrapolated from token prices. The `called_for` is the judge's
existing `autodedup_judge_text` — deliberately reused, because a production value
(`autodedup_facts`) is a CHECK-constraint change, i.e. a migration, and this pass is not
entitled to one. What tells the two apart in the ledger is `llm_calls.model` plus the run's own
summary; a production cache table and its own `called_for` are drafted alongside this wave's
artifacts and are a later migration.

E31 binds here exactly as it does on the judge: `max_usd` is required, the budget is reserved
BEFORE each call under a shared lock, and the summary reports `done`, not merely `drawn`.
E28 binds too: the text is scrubbed by the judge's own scrubber, once, in `fact_cards`.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from autodedup import fact_cards
from autodedup.export_sql import COHORT_LISTINGS_SQL
from autodedup.judge_lane import (
    ERROR_QUOTA,
    ERROR_RATE_LIMIT,
    RETRY_BACKOFF_S,
    RETRY_SLEEP,
    RETRYABLE,
    ArmPacer,
    Budget,
    _close,
    classify_provider_error,
    jittered,
)
from autodedup.judge_sql import JUDGEMENT_COST_SQL

CARDS_FILE: str = "cards.jsonl"
# Its OWN directory, not `autodedup/pairs/`: a committed pair list is `[[lo, hi], ...]` and a
# census test globs that whole directory to prove every file in it is one. A list of LISTING
# ids is a different shape and belongs in a different drawer.
LISTINGS_DIR: Path = Path(__file__).resolve().parent / "listings"

# Reused on purpose — see the module docstring. A production `autodedup_facts` value needs a
# migration this experiment is not entitled to open.
CALLED_FOR: str = "autodedup_judge_text"

MAX_TOKENS: int = 2048

# D2's hard per-run cap, the same number the judge lane binds to.
HARD_CAP_USD: float = 25.0

# Ids travel to Postgres in batches so one prepared plan serves the whole read.
READ_BATCH: int = 500


@dataclass(frozen=True)
class Arm:
    """One model arm: what to ask, whose API answers, and how hard to push it.

    `provider` is named rather than derived so an arm stays right whatever
    `provider_for_model` does next — the same reason the judge's votes name theirs.
    """

    name: str
    model: str
    provider: str
    workers: int
    min_interval_ms: int
    # Pre-flight only (E32 takes over the moment the first call lands): USD per million
    # input / output tokens, copied from the provider's own PRICES row.
    usd_in_per_mtok: float
    usd_out_per_mtok: float


# The four arms this experiment can run. Two OpenAI (the cheapest text model the repo prices,
# and the judge's own text model as the quality reference) and two DashScope/Qwen through the
# path the judge lane already opened — a small one and a mid one. Prices are the PRICES rows
# in api/providers/{openai,qwen}.py; they are the FORECAST, never the reported spend.
#
# DashScope answered W6's judge arms with 429s at three and six workers, so the Qwen arms ship
# at one worker with a spacing floor. That is not timidity: an arm that dies mid-pass measures
# nothing.
ARMS: dict[str, Arm] = {
    "nano": Arm("nano", "gpt-5-nano", "openai", 6, 0, 0.05, 0.40),
    "luna": Arm("luna", "gpt-5.6-luna", "openai", 6, 0, 0.20, 1.20),
    "qwen-flash": Arm("qwen-flash", "qwen3.7-flash", "qwen", 1, 400, 0.03, 0.13),
    "qwen-30b": Arm("qwen-30b", "qwen3-vl-30b-a3b-instruct", "qwen", 1, 400, 0.20, 0.80),
}
DEFAULT_ARM: str = "nano"

# Czech runs about 2.6 characters to a token on these tokenizers — materially worse than the
# ~4 English rule of thumb the judge's estimator uses, because the diacritics and the
# inflections both cost. Pre-flight only.
EST_CHARS_PER_TOKEN: float = 2.6
# The system prompt is long and fixed, so it dominates a short advert's bill.
EST_SYSTEM_TOKENS: int = 1500
EST_OUTPUT_TOKENS: int = 320


@dataclass(frozen=True)
class FactsArgs:
    listings: str
    arm: Arm
    max_usd: float
    workers: int
    max_chars: int
    limit: int
    prompt_version: str


def _int_arg(args: dict[str, str], name: str, default: int) -> int:
    raw = (args.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def load_listing_ids(raw: str) -> tuple[int, ...]:
    """`listings=<name>` under autodedup/listings/ — a JSON list of listing ids.

    A committed file, not an inline list: the set a pass ran over is the only way to read its
    numbers again, and an `--args` string is not a record of anything.
    """
    from autodedup.score_lane import repo_path

    path = repo_path(raw, LISTINGS_DIR)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"listings {raw!r}: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"listings {raw!r} is not valid JSON: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("listing_ids")
    if not isinstance(payload, list) or not payload:
        raise SystemExit(
            f"listings {raw!r} must be a non-empty JSON list of listing ids, or an object "
            "carrying one under 'listing_ids'"
        )
    ids: list[int] = []
    for entry in payload:
        try:
            ids.append(int(entry))
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"listings {raw!r}: {entry!r} is not an integer") from exc
    return tuple(dict.fromkeys(ids))


def parse_args(args: dict[str, str]) -> FactsArgs:
    listings = (args.get("listings") or "").strip()
    if not listings:
        raise SystemExit(
            "listings=<name> is required: a JSON list of listing ids under autodedup/listings/"
        )
    arm_name = (args.get("arm") or DEFAULT_ARM).strip()
    if arm_name not in ARMS:
        raise SystemExit(f"unknown arm {arm_name!r}; known arms: {', '.join(sorted(ARMS))}")
    arm = ARMS[arm_name]
    model = (args.get("model") or "").strip()
    provider = (args.get("provider") or "").strip()
    if model or provider:
        # An arm names a price as well as a model, and a model swapped under an arm's name
        # would forecast at the wrong one. Clearing the forecast is honest; guessing is not.
        arm = Arm(
            name=f"{arm.name}+override",
            model=model or arm.model,
            provider=provider or arm.provider,
            workers=arm.workers,
            min_interval_ms=arm.min_interval_ms,
            usd_in_per_mtok=arm.usd_in_per_mtok if not model else 0.0,
            usd_out_per_mtok=arm.usd_out_per_mtok if not model else 0.0,
        )

    raw_budget = (args.get("max_usd") or "").strip()
    if not raw_budget:
        raise SystemExit(
            "max_usd is required on every paid lane (E31): "
            "--args 'listings=w14_factcards,arm=nano,max_usd=2'"
        )
    try:
        max_usd = float(raw_budget)
    except ValueError as exc:
        raise SystemExit(f"max_usd must be a number, got {raw_budget!r}") from exc
    if max_usd <= 0:
        raise SystemExit(f"max_usd must be positive, got {max_usd}")
    if max_usd > HARD_CAP_USD:
        raise SystemExit(
            f"max_usd {max_usd} exceeds the D2 hard per-run cap of ${HARD_CAP_USD:.2f}"
        )

    workers = _int_arg(args, "workers", arm.workers)
    if workers < 1:
        raise SystemExit(f"workers must be at least 1, got {workers}")
    max_chars = _int_arg(args, "max_chars", fact_cards.TEXT_MAX_CHARS)
    if max_chars < 200:
        raise SystemExit(f"max_chars must be at least 200, got {max_chars}")
    limit = _int_arg(args, "limit", 0)
    if limit < 0:
        raise SystemExit(f"limit must not be negative, got {limit}")
    return FactsArgs(
        listings=listings,
        arm=arm,
        max_usd=max_usd,
        workers=workers,
        max_chars=max_chars,
        limit=limit,
        prompt_version=(args.get("prompt_version") or "").strip()
        or fact_cards.PROMPT_VERSION,
    )


# --- reading the adverts -----------------------------------------------------------------


def read_listings(conn: Any, ids: tuple[int, ...]) -> dict[int, dict[str, Any]]:
    """The adverts, through the EXPORT lane's own SQL — one statement, one contract.

    `COHORT_LISTINGS_SQL` selects `broker_phone` and `broker_email` because the export hashes
    them into its salted broker key. Nothing here wants them, so they are dropped on the way
    out of the cursor and never enter a dict that could be written or sent (E28).
    """
    kept = (
        "id", "source", "source_id_native", "source_url", "category_main", "category_type",
        "subtype", "disposition", "area_m2", "floor", "total_floors", "price_czk",
        "description",
    )
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), READ_BATCH):
        batch = list(ids[start: start + READ_BATCH])
        with conn.cursor() as cur:
            cur.execute(COHORT_LISTINGS_SQL, {"ids": batch})
            columns = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                record = dict(zip(columns, row))
                listing_id = int(record["id"])
                out[listing_id] = {
                    name: record.get(name) for name in kept if name in record
                }
    return out


# --- the pass ----------------------------------------------------------------------------


@dataclass
class Counters:
    listings: int = 0
    done: int = 0
    failed: int = 0
    no_text: int = 0
    not_found: int = 0
    skipped_budget: int = 0
    skipped_fatal: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    call_ids: list[int] | None = None
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.call_ids is None:
            self.call_ids = []
        if self.errors is None:
            self.errors = []


def llm_client(conn: Any) -> Any:
    from api.llm_client import LLMClient
    from api.providers.openai import OpenAIProvider
    from api.providers.qwen import QwenProvider

    return LLMClient(conn, providers={"openai": OpenAIProvider(), "qwen": QwenProvider()})


def est_call_usd(arm: Arm, text_chars: int) -> float:
    """What ONE card should cost, before anything has been billed (E32 replaces it after)."""
    input_tokens = EST_SYSTEM_TOKENS + int(text_chars / EST_CHARS_PER_TOKEN)
    return (
        input_tokens * arm.usd_in_per_mtok / 1_000_000.0
        + EST_OUTPUT_TOKENS * arm.usd_out_per_mtok / 1_000_000.0
    )


def _call_with_retry(client: Any, arm: Arm, listing_id: int, text: str,
                     truncated: bool, pacer: ArmPacer) -> Any:
    """The judge's own ladder: back off a burst, step the arm down, then give up.

    A retried 429 bought nothing and is not billed, so the budget is reserved per CALL by the
    caller and held across the whole ladder.
    """
    attempt = 0
    while True:
        try:
            with pacer.slot():
                return client.call(
                    called_for=CALLED_FOR,
                    model=arm.model,
                    provider=arm.provider,
                    messages=fact_cards.build_messages(listing_id, text, truncated),
                    system=fact_cards.SYSTEM_PROMPT,
                    tools=[fact_cards.TOOL_SCHEMA],
                    tool_choice=fact_cards.TOOL_NAME,
                    max_tokens=MAX_TOKENS,
                )
        except Exception as exc:  # noqa: BLE001 — classified, then waited out or re-raised
            kind = classify_provider_error(exc, after_success=pacer.had_success)
            if kind in RETRYABLE and attempt < len(RETRY_BACKOFF_S):
                delay = jittered(RETRY_BACKOFF_S[attempt])
                pacer.record_backoff(delay)
                RETRY_SLEEP(delay)
                attempt += 1
                continue
            if kind == ERROR_RATE_LIMIT and pacer.step_down(str(exc), listing_id):
                delay = jittered(RETRY_BACKOFF_S[0])
                pacer.record_backoff(delay)
                RETRY_SLEEP(delay)
                attempt = 0
                continue
            raise


def _card_from_response(response: Any, listing_id: int) -> fact_cards.UnitCard:
    calls = list(getattr(response, "tool_calls", None) or [])
    wanted = [call for call in calls if call.get("name") == fact_cards.TOOL_NAME] or calls
    if not wanted:
        raise fact_cards.CardParseError(
            f"no {fact_cards.TOOL_NAME} tool call in the response"
        )
    return fact_cards.parse_card(wanted[0].get("input") or {}, listing_id)


def run_pass(
    parsed: FactsArgs,
    jobs: list[tuple[int, dict[str, Any]]],
    budget: Budget,
    counters: Counters,
    conn_factory: Callable[[], Any],
    out_dir: Path,
    client_factory: Callable[[Any], Any] | None = None,
) -> ArmPacer:
    client_factory = client_factory or llm_client
    work: queue.Queue = queue.Queue()
    for job in jobs:
        work.put(job)
    stats_lock = threading.Lock()
    write_lock = threading.Lock()
    pacer = ArmPacer(parsed.arm.name, parsed.workers, parsed.arm.min_interval_ms)
    # Truncated, not appended: this file IS the pass's result, and a file holding two passes'
    # cards interleaved is one nobody can reconcile against a cost.
    sink = (out_dir / CARDS_FILE).open("w", encoding="utf-8")

    def emit(record: dict[str, Any]) -> None:
        with write_lock:
            sink.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            sink.flush()

    def worker() -> None:
        conn = conn_factory()
        try:
            client = client_factory(conn)
            while True:
                try:
                    listing_id, listing = work.get_nowait()
                except queue.Empty:
                    return
                try:
                    _run_job(parsed, listing_id, listing, budget, counters, client,
                             pacer, stats_lock, emit)
                except Exception as exc:  # noqa: BLE001 — one bad advert must not kill a thread
                    with stats_lock:
                        counters.failed += 1
                        counters.errors.append(
                            f"{listing_id}: {type(exc).__name__}: {exc}"[:400]
                        )
        finally:
            _close(conn)

    threads = [
        threading.Thread(target=worker, name=f"facts-{index}", daemon=True)
        for index in range(max(1, min(parsed.workers, max(1, len(jobs)))))
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sink.close()
    return pacer


def _run_job(
    parsed: FactsArgs,
    listing_id: int,
    listing: dict[str, Any],
    budget: Budget,
    counters: Counters,
    client: Any,
    pacer: ArmPacer,
    stats_lock: threading.Lock,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    text, truncated = fact_cards.prompt_text(listing.get("description"), parsed.max_chars)
    if not text:
        with stats_lock:
            counters.no_text += 1
        emit({
            "listing_id": listing_id,
            "source": listing.get("source"),
            "status": "no_text",
        })
        return

    estimate = est_call_usd(parsed.arm, len(text))
    if not budget.reserve(estimate):
        with stats_lock:
            if budget.fatal:
                counters.skipped_fatal += 1
            else:
                counters.skipped_budget += 1
        return

    started = time.monotonic()
    try:
        response = _call_with_retry(client, parsed.arm, listing_id, text, truncated, pacer)
    except Exception as exc:  # noqa: BLE001 — classified once more, then recorded
        budget.release(estimate)
        if classify_provider_error(exc, after_success=pacer.had_success) == ERROR_QUOTA:
            budget.abort(f"{type(exc).__name__}: {exc}")
        with stats_lock:
            counters.failed += 1
            counters.errors.append(f"{listing_id}: {type(exc).__name__}: {exc}"[:400])
        return
    pacer.record_success()
    cost = float(getattr(response, "cost_usd", 0.0) or 0.0)
    budget.settle(estimate, cost)

    record: dict[str, Any] = {
        "listing_id": listing_id,
        "source": listing.get("source"),
        "status": "done",
        "model": getattr(response, "model", parsed.arm.model),
        "arm": parsed.arm.name,
        "prompt_version": parsed.prompt_version,
        "content_key": fact_cards.content_key(text),
        "text_chars": len(text),
        "text_truncated": truncated,
        "input_tokens": int(getattr(response, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(response, "output_tokens", 0) or 0),
        "cost_usd": round(cost, 8),
        "llm_call_id": getattr(response, "llm_call_id", None),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    try:
        card = _card_from_response(response, listing_id)
    except Exception as exc:  # noqa: BLE001 — a billed call that read back nothing is a fact
        record["status"] = "unparsed"
        record["error"] = f"{type(exc).__name__}: {exc}"[:400]
        with stats_lock:
            counters.failed += 1
            counters.input_tokens += record["input_tokens"]
            counters.output_tokens += record["output_tokens"]
            if record["llm_call_id"] is not None:
                counters.call_ids.append(int(record["llm_call_id"]))
        emit(record)
        return

    record["card"] = card.to_json()
    record["filled"] = list(card.filled())
    with stats_lock:
        counters.done += 1
        counters.input_tokens += record["input_tokens"]
        counters.output_tokens += record["output_tokens"]
        if record["llm_call_id"] is not None:
            counters.call_ids.append(int(record["llm_call_id"]))
    emit(record)


def billed_usd(conn: Any, call_ids: list[int]) -> tuple[float, int]:
    """E32: the spend, READ from `llm_calls`, never extrapolated from token prices."""
    if not call_ids:
        return 0.0, 0
    with conn.cursor() as cur:
        cur.execute(JUDGEMENT_COST_SQL, {"ids": list(call_ids)})
        row = cur.fetchone()
    if not row:
        return 0.0, 0
    return float(row[0] or 0.0), int(row[1] or 0)


def run_facts(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Path
) -> dict[str, Any]:
    parsed = parse_args(args)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ids = load_listing_ids(parsed.listings)
    if parsed.limit:
        ids = ids[: parsed.limit]

    started = time.monotonic()
    conn = conn_factory()
    try:
        listings = read_listings(conn, ids)
    finally:
        _close(conn)

    counters = Counters(listings=len(ids))
    counters.not_found = len([listing_id for listing_id in ids if listing_id not in listings])
    jobs = [(listing_id, listings[listing_id]) for listing_id in ids if listing_id in listings]

    estimates = [
        est_call_usd(parsed.arm, len(fact_cards.prompt_text(
            listing.get("description"), parsed.max_chars)[0] or ""))
        for _, listing in jobs
    ]
    est_total = sum(estimates)
    # E31's pre-flight half: a budget that cannot pay for the DEAREST single card would open
    # every connection, walk the queue and skip its way to zero. Say so before anything runs.
    dearest = max(estimates, default=0.0)
    if dearest and parsed.max_usd < dearest:
        raise SystemExit(
            f"max_usd {parsed.max_usd} cannot pay for a single {parsed.arm.name} card "
            f"(~${dearest:.5f}); raise it or narrow the set with limit="
        )

    budget = Budget(parsed.max_usd)
    pacer = run_pass(parsed, jobs, budget, counters, conn_factory, out_dir)

    conn = conn_factory()
    try:
        spent_usd, n_calls = billed_usd(conn, counters.call_ids or [])
    finally:
        _close(conn)

    summary: dict[str, Any] = {
        "mode": "facts",
        "listings_file": parsed.listings,
        "arm": parsed.arm.name,
        "model": parsed.arm.model,
        "provider": parsed.arm.provider,
        "prompt_version": parsed.prompt_version,
        "max_usd": parsed.max_usd,
        "workers": parsed.workers,
        "max_chars": parsed.max_chars,
        "counts": {
            "listings_requested": counters.listings,
            "listings_read": len(jobs),
            "not_found": counters.not_found,
            "done": counters.done,
            "failed": counters.failed,
            "no_text": counters.no_text,
            "skipped_budget": counters.skipped_budget,
            "skipped_fatal": counters.skipped_fatal,
        },
        "tokens": {
            "input": counters.input_tokens,
            "output": counters.output_tokens,
        },
        "est_cost_usd": round(est_total, 6),
        "budget_covers_estimate": parsed.max_usd >= est_total,
        "spent_usd": round(spent_usd, 6),
        "llm_calls": n_calls,
        "usd_per_listing": round(spent_usd / counters.done, 8) if counters.done else None,
        "budget_stopped": budget.stopped,
        "budget_fatal": budget.fatal,
        "pacer": pacer.to_json(),
        "timings": {"seconds": round(time.monotonic() - started, 1)},
        "artifact": CARDS_FILE,
        "errors": (counters.errors or [])[:40],
    }
    (out_dir / "facts.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    # E31's other half: a pass that drew a list and read nothing back must not look green.
    if jobs and counters.done == 0:
        raise SystemExit(
            f"facts: {len(jobs)} listing(s) read, 0 card(s) done "
            f"(failed={counters.failed}, skipped_budget={counters.skipped_budget}, "
            f"skipped_fatal={counters.skipped_fatal}, fatal={budget.fatal})"
        )
    return summary
