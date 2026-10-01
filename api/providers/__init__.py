"""Provider-agnostic completion layer for the reasoning agent.

Each provider implements the `CompletionProvider` protocol from
`base.py`. The agent loop in `api/agent.py` and the recorder in
`api/llm_client.py` only ever speak the neutral types defined here.

Five providers ship: anthropic, gemini, openai, qwen, oss. Adding one is a
new file implementing the same protocol, registered in
`api/dependencies.py:_build_providers` (and any hand-built provider map).
"""

from api.providers.base import (
    BatchCapableProvider,
    BatchResultItem,
    BatchResultStatus,
    BatchStatus,
    Block,
    Completion,
    CompletionProvider,
    ImageBlock,
    Message,
    ModelPrice,
    ProviderError,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    Usage,
    compute_cost_usd,
)

__all__ = [
    "BatchCapableProvider",
    "BatchResultItem",
    "BatchResultStatus",
    "BatchStatus",
    "Block",
    "Completion",
    "CompletionProvider",
    "ImageBlock",
    "Message",
    "ModelPrice",
    "ProviderError",
    "TextBlock",
    "ToolCall",
    "ToolResultBlock",
    "ToolSchema",
    "ToolUseBlock",
    "Usage",
    "compute_cost_usd",
]
