"""Shared test fakes for the api/llm_client + agent tests.

`_FakeConn` mimics enough of `psycopg.Connection` for the SQL paths
the LLM orchestrator + skill loader + agent use. It is intentionally
narrow — each test extends the cursor's `execute` switch with what
it actually needs.

`_ScriptedProvider` is a CompletionProvider implementation that
returns pre-recorded Completions in order. Used by the agent tests
to drive both Anthropic and Gemini code paths without hitting any
real SDK.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from api.providers import (
    Completion,
    Message,
    ModelPrice,
    ToolSchema,
    Usage,
)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._last: list[Any] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    @property
    def description(self) -> list[tuple[str, ...]] | None:
        return self._conn._last_description

    def execute(
        self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()
    ) -> None:
        sql_norm = " ".join(sql.split()).lower()
        self._conn._last_description = None
        if sql_norm.startswith("select value from app_settings"):
            key = params[0] if isinstance(params, tuple) else params["key"]
            value = self._conn.app_settings.get(key)
            self._last = [value] if value is not None else None
        elif sql_norm.startswith("insert into llm_calls"):
            row_id = self._conn.next_id
            self._conn.next_id += 1
            self._conn.llm_calls_rows.append({
                "id": row_id,
                "params": params,
            })
            self._last = [row_id]
        elif "sum(cost_usd)" in sql_norm and "from llm_calls" in sql_norm:
            if "estimation_run_id" in sql_norm:
                target = params[0] if isinstance(params, tuple) else None
                total = sum(
                    float(row["params"][7] or 0)
                    for row in self._conn.llm_calls_rows
                    if row["params"][9] == target
                )
            else:
                total = sum(
                    float(row["params"][7] or 0)
                    for row in self._conn.llm_calls_rows
                )
            self._last = [total]
        elif sql_norm.startswith("select name, description, system_prompt"):
            name = params[0] if isinstance(params, tuple) else params.get("name")
            skill = self._conn.skills.get(name)
            self._last = list(skill) if skill is not None else None
        elif sql_norm.startswith("insert into estimation_runs"):
            row_id = self._conn.next_id
            self._conn.next_id += 1
            self._conn.estimation_rows[row_id] = dict(params) if isinstance(params, dict) else {}
            self._last = [row_id]
        elif sql_norm.startswith("update estimation_runs"):
            run_id = params.get("id") if isinstance(params, dict) else None
            if run_id in self._conn.estimation_rows:
                for k, v in (params or {}).items():
                    self._conn.estimation_rows[run_id][k] = v
            self._last = None
        elif sql_norm.startswith("select") and "from estimation_runs" in sql_norm:
            target = params[0] if isinstance(params, tuple) else params.get("id")
            row = self._conn.estimation_rows.get(target)
            self._last = [row] if row is not None else None
        else:
            self._last = None

    def fetchone(self) -> Any:
        return self._last

    def fetchall(self) -> list[Any]:
        return [self._last] if self._last is not None else []


class _FakeConn:
    def __init__(
        self,
        *,
        app_settings: dict[str, Any] | None = None,
        skills: dict[str, tuple[Any, ...]] | None = None,
    ) -> None:
        self.app_settings: dict[str, Any] = dict(app_settings or {})
        self.skills: dict[str, tuple[Any, ...]] = dict(skills or {})
        self.llm_calls_rows: list[dict[str, Any]] = []
        self.estimation_rows: dict[int, dict[str, Any]] = {}
        self.next_id = 1
        self._last_description: list[tuple[str, ...]] | None = None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    @contextmanager
    def transaction(self):
        yield self


class _ScriptedProvider:
    """A CompletionProvider that pops Completions from a scripted list.

    Lets one test drive a multi-turn loop deterministically. After
    the script is exhausted, `complete()` raises so tests fail loud
    rather than running an infinite loop.
    """

    def __init__(
        self,
        name: str,
        completions: list[Completion],
        prices: dict[str, ModelPrice] | None = None,
    ) -> None:
        self.name = name
        self._queue = list(completions)
        self._prices = prices or {}
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
        model: str,
        max_tokens: int = 4096,
    ) -> Completion:
        self.calls.append({
            "system": system,
            "messages": list(messages),
            "tools": list(tools),
            "model": model,
            "max_tokens": max_tokens,
        })
        if not self._queue:
            raise RuntimeError(
                f"{self.name}: scripted completions exhausted"
            )
        return self._queue.pop(0)

    def price_for(self, model: str) -> ModelPrice | None:
        return self._prices.get(model) or ModelPrice(1.0, 1.0, 0.0, 0.0)


def make_skill_row(
    name: str = "rental_estimator_v1",
    allowed_tools: list[str] | None = None,
    preferred_model: dict[str, str] | None = None,
    limits: dict[str, Any] | None = None,
    system_prompt: str = "system",
    description: str = "test",
    archived_at: Any = None,
) -> tuple[Any, ...]:
    """Build a tuple matching the SELECT order in api.skills.load_skill."""
    return (
        name,
        description,
        system_prompt,
        list(allowed_tools or [
            "find_comparables_relaxed",
            "analyze_distribution",
            "record_estimate",
        ]),
        dict(preferred_model or {
            "anthropic": "claude-sonnet-4-5",
            "gemini": "gemini-2.5-pro",
        }),
        dict(limits or {
            "max_iterations": 12,
            "max_cost_usd": 1.0,
            "wall_clock_timeout_s": 120.0,
        }),
        None,
        archived_at,
    )


def usage(input_tokens: int = 100, output_tokens: int = 50) -> Usage:
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
