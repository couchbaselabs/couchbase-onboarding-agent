"""
Client for the local Qwen 3.8 LLM, served via an Ollama-compatible HTTP API
(see qwen-service/ for the container that serves it). Provides both chat
completion (agentic reasoning / user-facing assistant) and text embeddings (used by
memory/couchbase_memory.py for vector search over agent memory).

Keeping the LLM entirely local/self-hosted means the onboarding agent never has to
send source database credentials, schema samples, or data to a third-party API --
a hard requirement for a tool that handles production database credentials across
eight different source systems (MongoDB, DynamoDB, Redis, Cassandra, Cosmos DB, and
Couchbase Community/Enterprise/Capella).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the Couchbase Onboarding Agent, an expert assistant embedded in a \
migration tool that moves data from MongoDB, Amazon DynamoDB, Redis, Apache Cassandra, \
Microsoft Azure Cosmos DB, or another Couchbase cluster (Community Edition, Enterprise \
Edition, or Capella) into Couchbase Server (Enterprise Edition) or Couchbase Capella.

Your job:
- Explain validation failures and warnings in plain language and suggest concrete fixes.
- Reason about migration strategy (one-time full load vs. continuous CDC vs. hybrid) given the
  source database's size, whether it supports change capture, and downtime tolerance. Note that
  a Couchbase source is a special case: instead of this app's generic per-document pipeline, it
  uses Couchbase's own native tools -- cbbackupmgr for a one-time full load, XDCR for continuous
  replication, or both for hybrid. XDCR continuous/hybrid replication is only available from a
  self-managed Enterprise Edition source (Community Edition has no XDCR, and Capella-as-source
  XDCR isn't wired up yet -- only one-time cbbackupmgr load applies there).
- Explain source-to-Couchbase data modeling decisions: how MongoDB documents, DynamoDB items,
  Redis keys, Cassandra rows, and Cosmos DB items each map to a Couchbase JSON document, key,
  scope, and collection. Note that a Couchbase-to-Couchbase migration doesn't need this kind of
  document remapping -- cbbackupmgr/XDCR move data at the bucket/collection level natively.
- Flag risk before the user approves a migration (e.g. a source type whose CDC mechanism isn't
  enabled/available, documents that may exceed Couchbase's 20MiB document size limit, naming
  collisions once container names are sanitized into Couchbase collection names).
- Never fabricate source statistics -- only reference numbers provided to you in context.
- Keep responses concise and actionable; this is an operational tool, not a chatbot.
"""


class QwenAgentError(RuntimeError):
    pass


class QwenAgentClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = self.settings.qwen_base_url.rstrip("/")

    async def chat(self, messages: list[dict[str, str]], context: dict[str, Any] | None = None) -> str:
        full_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if context:
            full_messages.append({
                "role": "system",
                "content": f"Relevant context for this conversation:\n{context}",
            })
        full_messages += messages

        payload = {
            "model": self.settings.qwen_model_name,
            "messages": full_messages,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=self.settings.qwen_request_timeout_s) as client:
            try:
                resp = await client.post(f"{self.base_url}/api/chat", json=payload)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                # A bare "500 Internal Server Error" from Ollama here is almost
                # always the model not being loaded yet -- on first boot the
                # qwen-service container reports healthy (server up) well before
                # entrypoint.sh finishes pulling the ~5GB model, so an early chat
                # request can race a still-in-progress download. Check for that
                # specific, actionable case rather than surfacing the raw error.
                state, _detail = await self.status()
                if state == "waiting":
                    raise QwenAgentError(
                        f"The local Qwen model ('{self.settings.qwen_model_name}') is still "
                        "downloading -- this takes a few minutes on first boot. Check progress "
                        "with `docker compose logs -f qwen-service` and try again shortly."
                    ) from exc
                raise QwenAgentError(f"Qwen chat request failed: {exc}") from exc
        data = resp.json()
        return data.get("message", {}).get("content", "").strip()

    async def embed(self, text: str) -> list[float]:
        payload = {"model": self.settings.qwen_embedding_model_name, "prompt": text}
        async with httpx.AsyncClient(timeout=self.settings.qwen_request_timeout_s) as client:
            try:
                resp = await client.post(f"{self.base_url}/api/embeddings", json=payload)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise QwenAgentError(f"Qwen embedding request failed: {exc}") from exc
        data = resp.json()
        embedding = data.get("embedding", [])
        if not embedding:
            raise QwenAgentError("Qwen returned an empty embedding vector.")
        return embedding

    async def is_healthy(self) -> bool:
        state, _detail = await self.status()
        return state == "ready"

    async def status(self) -> tuple[str, str]:
        """Reachability + readiness of the Qwen service, for the sidebar status
        indicator and for enriching chat error messages. Never raises.

        Returns ("ready" | "waiting" | "error", human-readable detail):
          - "ready": server reachable and qwen_model_name is loaded.
          - "waiting": server reachable but the model is still being pulled
            (first boot only -- the ~5GB qwen3:8b download takes a few minutes).
          - "error": can't reach the server at all (not started, crashed, or a
            networking/config problem).
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return "error", f"Can't reach the local Qwen service at {self.base_url}: {exc}"

        try:
            models = {m.get("name") for m in resp.json().get("models", [])}
        except Exception:  # noqa: BLE001
            return "error", "Qwen service responded but its model list couldn't be parsed."

        if self.settings.qwen_model_name in models:
            return "ready", f"Qwen ('{self.settings.qwen_model_name}') is loaded and ready."
        return (
            "waiting",
            f"Qwen model '{self.settings.qwen_model_name}' is still downloading (first boot only).",
        )
