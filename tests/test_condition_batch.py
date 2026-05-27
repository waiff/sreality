"""Hermetic tests for the Anthropic batch capability (Phase 1.8b).

No network, no DB: a fake SDK client is injected onto the provider so the
batch request-building, polling, and result-conversion paths can be
exercised offline.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.providers.anthropic import AnthropicProvider


def _provider_with_fake(fake_client: object) -> AnthropicProvider:
    p = AnthropicProvider(api_key="test-key")
    p._client = fake_client  # type: ignore[attr-defined]
    return p


def test_build_batch_request_params_caches_system_and_last_tool() -> None:
    p = AnthropicProvider(api_key="test-key")
    params = p.build_batch_request_params(
        system="SYS",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=[{"name": "a", "description": "", "input_schema": {}},
               {"name": "b", "description": "", "input_schema": {}}],
        model="claude-sonnet-4-6",
    )
    assert params["model"] == "claude-sonnet-4-6"
    assert params["messages"][0]["role"] == "user"
    # system wrapped as a cache-eligible block
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["system"][0]["text"] == "SYS"
    # only the LAST tool carries the cache breakpoint
    assert "cache_control" not in params["tools"][0]
    assert params["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_submit_batch_forwards_requests_and_returns_id() -> None:
    captured: dict[str, object] = {}

    class FakeBatches:
        def create(self, *, requests):  # noqa: ANN001
            captured["requests"] = requests
            return SimpleNamespace(id="batch_123")

    fake = SimpleNamespace(messages=SimpleNamespace(batches=FakeBatches()))
    p = _provider_with_fake(fake)

    bid = p.submit_batch([("s1-snap2", {"model": "m", "max_tokens": 10, "messages": []})])
    assert bid == "batch_123"
    assert captured["requests"][0]["custom_id"] == "s1-snap2"
    assert captured["requests"][0]["params"]["model"] == "m"


def test_poll_batch_maps_status_and_counts() -> None:
    counts = SimpleNamespace(processing=2, succeeded=8, errored=1, canceled=0, expired=0)

    class FakeBatches:
        def retrieve(self, batch_id):  # noqa: ANN001
            return SimpleNamespace(processing_status="ended", request_counts=counts)

    fake = SimpleNamespace(messages=SimpleNamespace(batches=FakeBatches()))
    status = _provider_with_fake(fake).poll_batch("batch_123")
    assert status.ended is True
    assert status.raw_status == "ended"
    assert status.counts == {
        "processing": 2, "succeeded": 8, "errored": 1, "canceled": 0, "expired": 0,
    }


def _succeeded_message() -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="ok"),
            SimpleNamespace(
                type="tool_use", id="tu_1", name="record_listing_condition",
                input={"building_level": 3, "apartment_level": 4},
            ),
        ],
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=50, cache_creation_input_tokens=0,
        ),
        stop_reason="tool_use",
        model="claude-sonnet-4-6",
    )


def test_iter_batch_results_splits_success_and_error() -> None:
    results = [
        SimpleNamespace(
            custom_id="s1-snap1",
            result=SimpleNamespace(type="succeeded", message=_succeeded_message()),
        ),
        SimpleNamespace(
            custom_id="s2-snap2",
            result=SimpleNamespace(type="errored", error="boom"),
        ),
        SimpleNamespace(
            custom_id="s3-snap3",
            result=SimpleNamespace(type="expired"),
        ),
    ]

    class FakeBatches:
        def results(self, batch_id):  # noqa: ANN001
            return iter(results)

    fake = SimpleNamespace(messages=SimpleNamespace(batches=FakeBatches()))
    items = list(_provider_with_fake(fake).iter_batch_results("batch_123"))

    assert items[0].custom_id == "s1-snap1"
    assert items[0].status == "succeeded"
    assert items[0].completion is not None
    assert items[0].completion.tool_calls[0].name == "record_listing_condition"
    assert items[0].completion.usage.input_tokens == 100
    assert items[0].completion.usage.cache_read_tokens == 50

    assert items[1].status == "errored"
    assert items[1].error == "boom"

    assert items[2].status == "expired"
