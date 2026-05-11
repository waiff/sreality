"""Per-source LLM-driven parsers for non-sreality listing URLs.

Each module exposes:
  build_messages(url: str, html: str) -> list[dict]
  post_process(extraction: dict, warnings: list[str]) -> tuple[dict, list[str]]

Where `extraction` is the dict the LLM returned via the `record_listing`
tool (each field is {"value": ..., "confidence": ...}; plus a top-level
`warnings` list).

Source-kind dispatch is in `scraper.source_dispatcher`; this package
just holds the per-source prompt templates and any source-specific
post-processing.
"""

from scraper.source_parsers import bezrealitky, generic, idnes_reality, remax

__all__ = ["bezrealitky", "generic", "idnes_reality", "remax"]
