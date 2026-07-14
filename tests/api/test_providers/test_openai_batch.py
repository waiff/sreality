"""OpenAI Batch API surface (api/providers/openai.py) — hermetic, fake HTTP.

No network: a fake session captures each request and returns scripted responses.
Covers the request shape (Anthropic-dict -> OpenAI chat body incl. base64 image),
the two-phase submit (file upload + batch create), status mapping, result-file
parsing (success + error lines), and the transient-retry wrapper.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from api.providers import openai as openai_mod
from api.providers.openai import OpenAIProvider


class _Resp:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


class _FakeSession:
    def __init__(self, get_map: dict[str, _Resp] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.post_responses: list[_Resp] = []
        self.get_map: dict[str, _Resp] = get_map or {}

    def post(self, url: str, **kw: Any) -> _Resp:
        self.calls.append(("POST", url, kw))
        return self.post_responses.pop(0)

    def get(self, url: str, **kw: Any) -> _Resp:
        self.calls.append(("GET", url, kw))
        for key, resp in self.get_map.items():
            if key in url:
                return resp
        raise AssertionError(f"no fake GET response for {url}")


def _prov(session: _FakeSession) -> OpenAIProvider:
    return OpenAIProvider(api_key="k", session=session)


def test_build_batch_request_params_converts_anthropic_dicts():
    body = _prov(_FakeSession()).build_batch_request_params(
        system="SYS",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "compare these"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
        ]}],
        tools=[{"name": "record", "description": "d", "input_schema": {"type": "object"}}],
        model="gpt-5-mini",
        tool_choice="record",
    )
    assert body["model"] == "gpt-5-mini"
    assert body["max_completion_tokens"] == 4096
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    parts = body["messages"][1]["content"]
    assert {"type": "text", "text": "compare these"} in parts
    img = next(p for p in parts if p["type"] == "image_url")
    assert img["image_url"]["url"] == "data:image/jpeg;base64,AAAA"
    assert body["tools"][0]["function"]["name"] == "record"
    assert body["tool_choice"] == {"type": "function", "function": {"name": "record"}}


def test_submit_batch_uploads_file_then_creates_batch():
    fake = _FakeSession()
    fake.post_responses = [_Resp(200, {"id": "file-1"}), _Resp(200, {"id": "batch-9"})]
    bid = _prov(fake).submit_batch([("cid1", {"model": "gpt-5-mini", "messages": []})])

    assert bid == "batch-9"
    up = fake.calls[0]
    assert up[0] == "POST" and up[1].endswith("/files")
    assert up[2]["data"] == {"purpose": "batch"}
    jsonl = up[2]["files"]["file"][1].decode()
    line = json.loads(jsonl.splitlines()[0])
    assert line["custom_id"] == "cid1"
    assert line["method"] == "POST"
    assert line["url"] == "/v1/chat/completions"
    assert line["body"]["model"] == "gpt-5-mini"
    create = fake.calls[1]
    assert create[1].endswith("/batches")
    assert create[2]["json"]["input_file_id"] == "file-1"
    assert create[2]["json"]["endpoint"] == "/v1/chat/completions"
    assert create[2]["json"]["completion_window"] == "24h"


def test_submit_batch_empty_raises():
    with pytest.raises(Exception):
        _prov(_FakeSession()).submit_batch([])


def test_submit_batch_retries_transient(monkeypatch):
    monkeypatch.setattr(openai_mod.time, "sleep", lambda _s: None)
    fake = _FakeSession()
    # 1st upload 503 (transient) -> retry: upload ok, create ok.
    fake.post_responses = [
        _Resp(503, None, text="overloaded"),
        _Resp(200, {"id": "file-2"}),
        _Resp(200, {"id": "batch-2"}),
    ]
    bid = _prov(fake).submit_batch([("c", {"model": "gpt-5-mini"})])
    assert bid == "batch-2"
    assert len(fake.calls) == 3


@pytest.mark.parametrize("status,ended", [
    ("in_progress", False),
    ("validating", False),
    ("completed", True),
    ("failed", True),
    ("expired", True),
    ("cancelled", True),
])
def test_poll_batch_status_mapping(status, ended):
    fake = _FakeSession(get_map={"/batches/": _Resp(200, {
        "status": status,
        "request_counts": {"total": 5, "completed": 2, "failed": 1},
    })})
    st = _prov(fake).poll_batch("batch-9")
    assert st.ended is ended
    assert st.raw_status == status
    # OpenAI's completed/failed are normalized to the neutral succeeded/errored
    # vocabulary that scripts/ingest_dedup_batch reads (the Anthropic provider's
    # keys) — otherwise an OpenAI batch NULLs dedup_batches.{succeeded,errored}_count.
    assert st.counts == {"total": 5, "succeeded": 2, "errored": 1}


def test_iter_batch_results_parses_success_and_error():
    batch_resp = _Resp(200, {
        "status": "completed",
        "output_file_id": "out-1",
        "error_file_id": "err-1",
        "request_counts": {},
    })
    out_line = json.dumps({"custom_id": "cidA", "response": {"status_code": 200, "body": {
        "choices": [{"message": {"content": "", "tool_calls": [{
            "id": "t1", "type": "function",
            "function": {"name": "record_visual_match", "arguments": "{\"verdict\": \"High\"}"},
        }]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        "model": "gpt-5-mini",
    }}})
    err_line = json.dumps({"custom_id": "cidB",
                           "response": {"status_code": 400, "body": {"error": {"message": "bad"}}},
                           "error": None})
    fake = _FakeSession(get_map={
        "/batches/": batch_resp,
        "/files/out-1/content": _Resp(200, None, text=out_line),
        "/files/err-1/content": _Resp(200, None, text=err_line),
    })
    items = {it.custom_id: it for it in _prov(fake).iter_batch_results("b")}
    assert items["cidA"].status == "succeeded"
    assert items["cidA"].completion.tool_calls[0].input == {"verdict": "High"}
    assert items["cidB"].status == "errored"
    assert "bad" in (items["cidB"].error or "")
