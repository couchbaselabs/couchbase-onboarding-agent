"""Helper for turning migration events/context into embeddable text + vectors."""
from __future__ import annotations

from app.core.qwen_agent import QwenAgentClient


def summarize_for_embedding(kind: str, payload: dict) -> str:
    """Flatten a structured memory event into a compact text blob for embedding."""
    parts = [f"type={kind}"]
    for k, v in payload.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}={str(v)[:200]}")
    return " | ".join(parts)


async def embed_text(text: str) -> list[float]:
    client = QwenAgentClient()
    return await client.embed(text)
