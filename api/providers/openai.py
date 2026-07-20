"""OpenAI backend for the CompletionProvider protocol (Session-3 vision bake-off).

Wraps the plain Chat Completions REST API via `OpenAICompatibleProvider` — no
`openai` SDK dependency (rule #7): the wire format is JSON over HTTP and the
shared base already speaks it. Also implements the async Batch API (the
`BatchCapableProvider` surface) so gpt-5-mini can run the dedup/enrichment vision
lanes through OpenAI's own −50% batch tier, the way Sonnet runs through
Anthropic's — see scripts.submit_dedup_batch / ingest_dedup_batch, which drive
whichever provider a lane's model resolves to (llm_client.provider_for_model).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import requests

from api.providers.base import (
    BatchResultItem,
    BatchStatus,
    ModelPrice,
    ProviderError,
)
from api.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _completion_from_raw,
)

LOG = logging.getLogger(__name__)

# Batch HTTP timeouts: create/poll are quick; the results file can be tens of MB.
_BATCH_HTTP_TIMEOUT_S = 120
_BATCH_RESULTS_TIMEOUT_S = 600
# OpenAI batch terminal states (both spellings of cancel seen across API versions).
_TERMINAL_BATCH_STATUSES = frozenset(
    {"completed", "failed", "expired", "cancelled", "canceled"}
)

# gpt-5-mini: $0.25 input / $2.00 output per 1M tokens; cached input $0.025 (the
# standard OpenAI 90%-off cached rate = 10% of input). Re-verified 2026-07-14
# against openrouter.ai ($0.25 / $2.00, matching) — OpenAI's own pricing page
# (developers.openai.com/api/docs/pricing) still lists only gpt-5.4-mini, not
# gpt-5-mini, though gpt-5-mini remains a live, callable model id. The definitive
# post-hoc check is the OpenAI billing dashboard vs recorded llm_calls.cost_usd.
PRICES: dict[str, ModelPrice] = {
    "gpt-5-mini": ModelPrice(0.25, 2.00, 0.025, 0.0),
}


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"

    def __init__(self, *, api_key: str | None = None, session: Any = None) -> None:
        super().__init__(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            prices=PRICES,
            # GPT-5-series rejects `max_tokens` with a 400 ("use max_completion_tokens");
            # see the OpenAICompatibleProvider docstring for the source.
            max_tokens_param="max_completion_tokens",
            api_key=api_key,
            session=session,
        )

    # --- async Batch API (BatchCapableProvider) -------------------------------
    # OpenAI's Batch API is two-phase (unlike Anthropic's inline create): upload a
    # JSONL file of {custom_id, method, url, body} lines to /v1/files, then create a
    # /v1/batches job referencing it. Results return as an output (+ error) file of
    # one JSON line per request. These four methods mirror AnthropicProvider's batch
    # surface so the submit/ingest scripts stay provider-agnostic.

    def build_batch_request_params(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        """One batch request's `body` (the `/v1/chat/completions` params).

        The batch builders (visual_match.build_compare_request, …) emit
        Anthropic-shaped content-block dicts, so convert to neutral blocks first
        and reuse `_chat_body` — a batched request then serialises identically to
        the same request on the sync path. The neutral converters are imported
        lazily to avoid a provider→llm_client cycle at module load.
        """
        from api.llm_client import _to_neutral_message, _to_neutral_tool

        neutral_messages = [_to_neutral_message(m) for m in messages]
        neutral_tools = [_to_neutral_tool(t) for t in (tools or [])]
        return self._chat_body(
            system=system,
            messages=neutral_messages,
            tools=neutral_tools,
            model=model,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )

    def submit_batch(self, items: list[tuple[str, dict[str, Any]]]) -> str:
        """Upload the JSONL request file, then create the batch. Returns the batch
        id. Bounded retry on transient upload/create failures (mirrors Anthropic):
        nothing is recorded until an id comes back, so a retry is safe."""
        if not items:
            raise ProviderError("submit_batch called with no requests")
        if not self._api_key:
            raise ProviderError(
                f"{self._api_key_env} is not set; cannot submit an {self.name} batch"
            )
        payload = "\n".join(
            json.dumps(
                {
                    "custom_id": custom_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": body,
                },
                separators=(",", ":"),
            )
            for custom_id, body in items
        ).encode("utf-8")

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                file_id = self._upload_batch_file(payload)
                return self._create_batch(file_id)
            except ProviderError as exc:
                last_exc = exc
                if not _is_transient(exc) or attempt == 2:
                    raise
                wait = 2.0 * (2 ** attempt)
                LOG.warning(
                    "openai batch submit transient failure (%s); retry %d/2 in %.0fs",
                    exc, attempt + 1, wait,
                )
                time.sleep(wait)
        raise ProviderError(f"openai batch submit failed: {last_exc}") from last_exc

    def poll_batch(self, provider_batch_id: str) -> BatchStatus:
        raw = self._batch_get(f"/batches/{provider_batch_id}")
        status = str(raw.get("status") or "")
        rc = raw.get("request_counts") or {}
        # Normalize to the neutral BatchStatus.counts vocabulary the Anthropic
        # provider established (succeeded/errored) — OpenAI names them
        # completed/failed. scripts/ingest_dedup_batch reads counts["succeeded"] /
        # counts["errored"] regardless of provider; without this mapping an OpenAI
        # batch NULLs dedup_batches.{succeeded,errored}_count.
        counts = {
            "total": int(rc.get("total") or 0),
            "succeeded": int(rc.get("completed") or 0),
            "errored": int(rc.get("failed") or 0),
        }
        return BatchStatus(
            provider_batch_id=provider_batch_id,
            ended=status in _TERMINAL_BATCH_STATUSES,
            raw_status=status,
            counts=counts,
        )

    def iter_batch_results(
        self, provider_batch_id: str
    ) -> Iterator[BatchResultItem]:
        raw = self._batch_get(f"/batches/{provider_batch_id}")
        # A completed request lands in the output file; a failed one in the error
        # file — the two id-sets are disjoint, so iterating both never double-yields.
        for file_id in (raw.get("output_file_id"), raw.get("error_file_id")):
            if not file_id:
                continue
            for line in self._file_content(str(file_id)).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning(
                        "openai batch %s: unparseable result line", provider_batch_id
                    )
                    continue
                yield _result_item(rec)

    # --- HTTP helpers ---------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _upload_batch_file(self, payload: bytes) -> str:
        try:
            resp = self._session.post(
                f"{self._base_url}/files",
                headers=self._auth_headers(),
                files={"file": ("batch.jsonl", payload, "application/jsonl")},
                data={"purpose": "batch"},
                timeout=_BATCH_HTTP_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"openai file upload failed: {exc}") from exc
        _raise_for_status(resp, "file upload")
        return str(resp.json().get("id") or "")

    def _create_batch(self, file_id: str) -> str:
        try:
            resp = self._session.post(
                f"{self._base_url}/batches",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={
                    "input_file_id": file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": "24h",
                },
                timeout=_BATCH_HTTP_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"openai batch create failed: {exc}") from exc
        _raise_for_status(resp, "batch create")
        return str(resp.json().get("id") or "")

    def _batch_get(self, path: str) -> dict[str, Any]:
        try:
            resp = self._session.get(
                f"{self._base_url}{path}",
                headers=self._auth_headers(),
                timeout=_BATCH_HTTP_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"openai batch GET {path} failed: {exc}") from exc
        _raise_for_status(resp, f"GET {path}")
        return resp.json()

    def _file_content(self, file_id: str) -> str:
        try:
            resp = self._session.get(
                f"{self._base_url}/files/{file_id}/content",
                headers=self._auth_headers(),
                timeout=_BATCH_RESULTS_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"openai file content {file_id} failed: {exc}") from exc
        _raise_for_status(resp, f"file content {file_id}")
        return resp.text


def _result_item(rec: dict[str, Any]) -> BatchResultItem:
    """One JSONL result line → BatchResultItem. A 200 with a body is a success;
    anything else (non-200, an `error` object, a missing body) is an error."""
    custom_id = str(rec.get("custom_id") or "")
    resp = rec.get("response") or {}
    body = resp.get("body")
    if int(resp.get("status_code") or 0) == 200 and isinstance(body, dict):
        try:
            return BatchResultItem(
                custom_id=custom_id,
                status="succeeded",
                completion=_completion_from_raw(body, model=""),
            )
        except ProviderError as exc:
            return BatchResultItem(custom_id=custom_id, status="errored", error=str(exc))
    err = rec.get("error") or body or resp or "errored"
    return BatchResultItem(
        custom_id=custom_id, status="errored", error=json.dumps(err)[:500],
    )


def _raise_for_status(resp: Any, what: str) -> None:
    if resp.status_code >= 400:
        # Keep status + body in the message so scripts.validate_vision_models
        # ._is_infra_error can keyword-match a dead key / quota (mirrors complete()).
        raise ProviderError(
            f"openai {what} failed: HTTP {resp.status_code} {resp.text[:500]}"
        )


def _is_transient(exc: Exception) -> bool:
    s = str(exc).lower()
    return (
        any(code in s for code in ("429", "500", "502", "503", "504"))
        or "timeout" in s
        or "connection" in s
    )
