"""Interchangeable coding CLI adapters."""
from .base import CLICodingAdapter, CODING_RESULT_SCHEMA, ExecutionContext
from .claude_cli import ClaudeCliAdapter
from .codex_cli import CodexCliAdapter

__all__ = ["CLICodingAdapter", "CODING_RESULT_SCHEMA", "ExecutionContext", "CodexCliAdapter", "ClaudeCliAdapter"]
