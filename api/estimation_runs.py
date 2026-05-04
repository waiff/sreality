"""Persistence and trace machinery for /estimations endpoints.

Trace contract (locked at TRACE_SCHEMA_VERSION = 1):

    {
      "version": 1,
      "summary": "<one-line human-readable summary>",
      "steps": [
        {
          "n": 1,                                  # 1-indexed monotonic
          "kind": "tool_call" | "computation" | "reasoning",
          "started_at": "ISO-8601 UTC ms",
          "duration_ms": int,
          "output_summary": {...},                 # NEVER full output
          # tool_call adds:    "tool", "input"
          # computation adds:  "label"
          # reasoning reserved for Phase U4 agent.
        },
        ...
      ]
    }

Full tool outputs (lists of comparables, full distribution stats) live
in dedicated columns on estimation_runs (comparables_used etc.), not
in the trace. The trace stays bounded regardless of cohort size.

Bumping TRACE_SCHEMA_VERSION is a deliberate change — readers must
handle older versions.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

TRACE_SCHEMA_VERSION = 1


class StepHandle:
    """Returned by recorder context managers; caller sets the summary."""

    def __init__(self) -> None:
        self.summary: dict[str, Any] = {}

    def set_summary(self, summary: dict[str, Any]) -> None:
        self.summary = summary


class TraceRecorder:
    """Captures tool calls and computations into the trace format."""

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []
        self._n = 0

    @contextlib.contextmanager
    def tool_call(
        self, tool: str, input: dict[str, Any]
    ) -> Iterator[StepHandle]:
        started_at = datetime.now(timezone.utc)
        mono_start = time.monotonic()
        handle = StepHandle()
        try:
            yield handle
        finally:
            self._append(
                kind="tool_call",
                started_at=started_at,
                duration_ms=_ms_since(mono_start),
                fields={"tool": tool, "input": input},
                handle=handle,
            )

    @contextlib.contextmanager
    def computation(self, label: str) -> Iterator[StepHandle]:
        started_at = datetime.now(timezone.utc)
        mono_start = time.monotonic()
        handle = StepHandle()
        try:
            yield handle
        finally:
            self._append(
                kind="computation",
                started_at=started_at,
                duration_ms=_ms_since(mono_start),
                fields={"label": label},
                handle=handle,
            )

    def to_dict(self, summary: str) -> dict[str, Any]:
        return {
            "version": TRACE_SCHEMA_VERSION,
            "summary": summary,
            "steps": list(self._steps),
        }

    def _append(
        self,
        *,
        kind: str,
        started_at: datetime,
        duration_ms: int,
        fields: dict[str, Any],
        handle: StepHandle,
    ) -> None:
        self._n += 1
        step: dict[str, Any] = {
            "n": self._n,
            "kind": kind,
            "started_at": started_at.isoformat(timespec="milliseconds"),
            "duration_ms": duration_ms,
            **fields,
            "output_summary": handle.summary,
        }
        self._steps.append(step)


class _NullStepHandle:
    def set_summary(self, summary: dict[str, Any]) -> None:
        return None


class _NullTraceRecorder:
    """No-op recorder used when estimate_yield is called without one.

    Lets estimate_yield use the same `with rec.tool_call(...)` form
    regardless of whether a real recorder was passed, with effectively
    zero overhead and no behavioural change for existing callers.
    """

    @contextlib.contextmanager
    def tool_call(
        self, tool: str, input: dict[str, Any]
    ) -> Iterator[_NullStepHandle]:
        yield _NULL_HANDLE

    @contextlib.contextmanager
    def computation(self, label: str) -> Iterator[_NullStepHandle]:
        yield _NULL_HANDLE

    def to_dict(self, summary: str) -> dict[str, Any]:
        return {
            "version": TRACE_SCHEMA_VERSION,
            "summary": summary,
            "steps": [],
        }


_NULL_HANDLE = _NullStepHandle()
NULL_RECORDER: Any = _NullTraceRecorder()


def _ms_since(mono_start: float) -> int:
    return int((time.monotonic() - mono_start) * 1000)
