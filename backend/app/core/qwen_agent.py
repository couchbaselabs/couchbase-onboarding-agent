"""Compatibility shim: the assistant client moved to core/llm.py when it grew
support for hosted providers. Import from there in new code."""
from app.core.llm import (  # noqa: F401
    SYSTEM_PROMPT,
    EmbeddingsUnavailableError,
    LlmClient,
    LlmDisabledError,
    LlmError,
    QwenAgentClient,
    QwenAgentError,
)
