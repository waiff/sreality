"""Provider-agnostic completion layer for the reasoning agent.

Each provider implements the `CompletionProvider` protocol from
`base.py`. The agent loop in `api/agent.py` and the recorder in
`api/llm_client.py` only ever speak the neutral types defined here.

Today we ship two providers — Anthropic and Gemini. Adding a third
(OpenAI, Vertex AI, etc.) is one new file implementing the same
protocol, registered in `api/main.py`.
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
