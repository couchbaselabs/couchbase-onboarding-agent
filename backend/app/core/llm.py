"""Client for the model the assistant runs on.

By default that's the bundled local model (Ollama-compatible `qwen-service`),
which keeps everything the assistant sees on this host. An administrator can
instead choose a hosted model -- OpenAI, Azure OpenAI, Anthropic or any
OpenAI-compatible endpoint -- with their own credentials (core/llm_settings.py).
Two capabilities are exposed either way: chat completion (the user-facing
assistant) and text embeddings (memory/couchbase_memory.py's vector search).

Nothing on a migration's critical path calls the model, and the chat context
never contains database credentials. With a hosted provider, validation
messages, migration stats and the user's questions do leave this host; the UI
says so wherever a hosted provider is selected.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import get_settings
from app.core.llm_settings import (
    OPENAI_STYLE,
    EmbeddingSource,
    LlmProvider,
    LlmProviderConfig,
    LlmSettingsStore,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the Couchbase Labs Onboarding Agent, an expert assistant embedded in a \
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
- Explain destination mapping: every source container is written to one or more
  bucket.scope.collection targets (the first is the primary copy used for verification,
  reconciliation and fallback; extra targets receive the same writes). Buckets named in a
  target are created if missing.
- Explain bi-directional operation: after cutover, fallback sync writes Couchbase changes back
  to the source (reverse XDCR for an Enterprise source with a self-managed destination,
  otherwise META().cas polling + the source's bulk write API); "fall back" hands the system of
  record back to the source and records the source's change position; "failback" (Return to
  Couchbase) resumes forward sync from that position without a new bulk copy, and another
  cutover completes the round trip. The two directions never run at the same time.
- Never fabricate source statistics -- only reference numbers provided to you in context.
- Keep responses concise and actionable; this is an operational tool, not a chatbot.
"""


ANTHROPIC_VERSION = "2023-06-01"


class LlmError(RuntimeError):
    pass


class LlmDisabledError(LlmError):
    """The assistant is turned off (LLM_ENABLED=false); nothing was contacted."""

    def __init__(self) -> None:
        super().__init__(
            "The assistant is turned off for this deployment (LLM_ENABLED=false). Migrations, validation "
            "and bottleneck alerts work as usual; set LLM_ENABLED=true and start the LLM service to chat."
        )


class EmbeddingsUnavailableError(LlmError):
    """Embeddings are turned off for the configured provider (embedding_source=none)."""

    def __init__(self) -> None:
        super().__init__("Agent-memory embeddings are turned off for the configured assistant model.")


# Kept for callers/tests written against the original local-only client.
QwenAgentError = LlmError


def _short(exc: httpx.HTTPError) -> str:
    """One-line, credential-free description of a failed request."""
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text[:300].replace("\n", " ")
        return f"HTTP {exc.response.status_code} from {exc.request.url.host}: {body}"
    return f"{type(exc).__name__}: {exc}"


class LlmClient:
    """One instance per request; cheap to construct."""

    def __init__(self, config: LlmProviderConfig | None = None) -> None:
        self.settings = get_settings()
        self.config = config or LlmSettingsStore.instance().effective()
        self.base_url = self.config.effective_base_url

    @property
    def enabled(self) -> bool:
        return self.settings.llm_enabled

    @property
    def provider(self) -> LlmProvider:
        return self.config.provider

    @property
    def model(self) -> str:
        return self.config.effective_model

    # ------------------------------------------------------------------ chat

    async def chat(self, messages: list[dict[str, str]], context: dict[str, Any] | None = None) -> str:
        if not self.enabled:
            raise LlmDisabledError()
        problems = self.config.problems()
        if problems:
            raise LlmError("The assistant model isn't fully configured: " + " ".join(problems))
        system = SYSTEM_PROMPT
        if context:
            system += f"\n\nRelevant context for this conversation:\n{context}"
        try:
            if self.provider == LlmProvider.LOCAL:
                return await self._chat_ollama(system, messages)
            if self.provider == LlmProvider.ANTHROPIC:
                return await self._chat_anthropic(system, messages)
            return await self._chat_openai(system, messages)
        except httpx.HTTPError as exc:
            if self.provider == LlmProvider.LOCAL:
                # A bare 500 from Ollama is almost always the model not being
                # loaded yet: on first boot the container reports healthy well
                # before entrypoint.sh finishes pulling the models.
                state, _detail = await self.status()
                if state == "waiting":
                    raise LlmError(
                        f"The local model ('{self.model}') is still downloading -- this takes a few minutes "
                        "on first boot. Check progress with `docker compose logs -f qwen-service` and try again shortly."
                    ) from exc
            raise LlmError(f"{self.config.label} chat request failed: {_short(exc)}") from exc

    async def _chat_ollama(self, system: str, messages: list[dict[str, str]]) -> str:
        payload = {"model": self.model, "messages": [{"role": "system", "content": system}, *messages], "stream": False}
        async with httpx.AsyncClient(timeout=self.config.request_timeout_s) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "").strip()

    def _openai_chat_url(self) -> tuple[str, dict[str, str]]:
        if self.provider == LlmProvider.AZURE_OPENAI:
            url = (f"{self.base_url}/openai/deployments/{self.model}/chat/completions"
                   f"?api-version={self.config.api_version}")
            return url, {"api-key": self.config.api_key}
        headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
        return f"{self.base_url}/chat/completions", headers

    async def _chat_openai(self, system: str, messages: list[dict[str, str]]) -> str:
        url, headers = self._openai_chat_url()
        payload: dict[str, Any] = {
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": self.config.max_tokens,
        }
        if self.provider != LlmProvider.AZURE_OPENAI:
            payload["model"] = self.model
        async with httpx.AsyncClient(timeout=self.config.request_timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise LlmError(f"{self.config.label} returned no choices: {str(data)[:200]}")
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, list):  # some compatible servers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return (content or "").strip()

    async def _chat_anthropic(self, system: str, messages: list[dict[str, str]]) -> str:
        headers = {"x-api-key": self.config.api_key, "anthropic-version": ANTHROPIC_VERSION}
        payload = {
            "model": self.model,
            "system": system,
            "messages": [m for m in messages if m.get("role") in ("user", "assistant")],
            "max_tokens": self.config.max_tokens,
        }
        async with httpx.AsyncClient(timeout=self.config.request_timeout_s) as client:
            resp = await client.post(f"{self.base_url}/v1/messages", json=payload, headers=headers)
            resp.raise_for_status()
        data = resp.json()
        return "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()

    # ------------------------------------------------------------ embeddings

    @property
    def embeddings_enabled(self) -> bool:
        if not self.enabled:
            return False
        if self.provider == LlmProvider.LOCAL:
            return True
        return self.config.embedding_source != EmbeddingSource.NONE

    async def embed(self, text: str) -> list[float]:
        if not self.enabled:
            raise LlmDisabledError()
        local = self.provider == LlmProvider.LOCAL or self.config.embedding_source == EmbeddingSource.LOCAL
        if not local and self.config.embedding_source == EmbeddingSource.NONE:
            raise EmbeddingsUnavailableError()
        try:
            if local:
                return await self._embed_ollama(text)
            return await self._embed_openai(text)
        except httpx.HTTPError as exc:
            raise LlmError(f"Embedding request failed: {_short(exc)}") from exc

    async def _embed_ollama(self, text: str) -> list[float]:
        base = self.base_url if self.provider == LlmProvider.LOCAL else self.settings.qwen_base_url.rstrip("/")
        payload = {"model": self.settings.qwen_embedding_model_name, "prompt": text}
        async with httpx.AsyncClient(timeout=self.config.request_timeout_s) as client:
            resp = await client.post(f"{base}/api/embeddings", json=payload)
            resp.raise_for_status()
        embedding = resp.json().get("embedding", [])
        if not embedding:
            raise LlmError("The local embedding model returned an empty vector.")
        return embedding

    async def _embed_openai(self, text: str) -> list[float]:
        if self.provider not in OPENAI_STYLE:
            raise EmbeddingsUnavailableError()
        if self.provider == LlmProvider.AZURE_OPENAI:
            url = (f"{self.base_url}/openai/deployments/{self.config.embedding_model}/embeddings"
                   f"?api-version={self.config.api_version}")
            headers = {"api-key": self.config.api_key}
            payload: dict[str, Any] = {"input": text}
        else:
            url = f"{self.base_url}/embeddings"
            headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
            payload = {"input": text, "model": self.config.embedding_model}
        async with httpx.AsyncClient(timeout=self.config.request_timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        rows = resp.json().get("data") or []
        embedding = rows[0].get("embedding", []) if rows else []
        if not embedding:
            raise LlmError(f"{self.config.label} returned an empty embedding vector.")
        return embedding

    # ---------------------------------------------------------------- status

    async def is_healthy(self) -> bool:
        state, _detail = await self.status()
        return state == "ready"

    def _describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value, "provider_label": self.config.label,
            "model": self.model, "local": self.provider == LlmProvider.LOCAL,
        }

    async def status(self) -> tuple[str, str]:
        """Reachability + readiness, for the sidebar status indicator. Never raises.

        ("ready" | "waiting" | "error" | "disabled", human-readable detail).
        For the local service this probes /api/tags and checks the configured
        models are loaded ("waiting" = still being pulled on first boot). For a
        hosted provider it only checks the configuration -- the sidebar polls
        every few seconds and a billed API call each time would be wasteful;
        the Settings page's "Test connection" makes a real request.
        """
        if not self.enabled:
            return "disabled", "The assistant is turned off for this deployment (LLM_ENABLED=false)."
        if self.provider != LlmProvider.LOCAL:
            problems = self.config.problems()
            if problems:
                return "error", f"{self.config.label} isn't fully configured: " + " ".join(problems)
            return "ready", f"Assistant uses {self.config.label} model '{self.model}'. Prompts leave this host."
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return "error", f"Can't reach the local model service at {self.base_url}: {exc}"

        try:
            models = {m.get("name") for m in resp.json().get("models", [])}
        except Exception:  # noqa: BLE001
            return "error", "The local model service responded but its model list couldn't be parsed."

        # Ollama lists "name:tag"; a bare name in config means the ":latest" tag.
        def loaded(name: str) -> bool:
            return name in models or (":" not in name and f"{name}:latest" in models)

        missing = [m for m in (self.model, self.settings.qwen_embedding_model_name) if not loaded(m)]
        if not missing:
            return "ready", (f"Local assistant model '{self.model}' and embedding model "
                             f"'{self.settings.qwen_embedding_model_name}' are loaded and ready.")
        return "waiting", f"Model(s) {missing} are still downloading (first boot only)."

    async def status_payload(self) -> dict[str, Any]:
        state, detail = await self.status()
        payload = {"status": state, "detail": detail, **self._describe()}
        if self.provider == LlmProvider.LOCAL and state == "ready":
            payload.update(await self.compute_info())
        return payload

    # -------------------------------------------------------------- compute

    # Ollama has no "which GPUs did you detect" endpoint; the only signal is how
    # much of each *loaded* model sits in VRAM (/api/ps: size vs size_vram). A
    # model is unloaded after a few idle minutes, so remember the last answer
    # per server and keep reporting it until a fresh observation replaces it.
    _compute_cache: dict[str, dict[str, Any]] = {}

    async def compute_info(self) -> dict[str, Any]:
        """{"compute": "gpu"|"cpu"|"partial"|"unknown", "compute_detail": str}.

        Never raises. "unknown" only until the first chat/embedding has loaded a
        model (or if /api/ps isn't supported by whatever serves base_url).
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.base_url}/api/ps")
                resp.raise_for_status()
            loaded = resp.json().get("models", [])
        except Exception:  # noqa: BLE001
            loaded = []
        observed = self._classify_loaded(loaded)
        if observed is not None:
            LlmClient._compute_cache[self.base_url] = observed
            return observed
        cached = LlmClient._compute_cache.get(self.base_url)
        if cached:
            return {**cached, "compute_detail": cached["compute_detail"] + " (last observed; model idle)"}
        return {"compute": "unknown",
                "compute_detail": "Not known until the first request loads the model."}

    @staticmethod
    def _classify_loaded(loaded: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Classify Ollama's /api/ps entries; None when nothing is loaded.

        Prefers the chat model's entry (largest) since the tiny embedding model
        may fit in VRAM even when the chat model has spilled to CPU.
        """
        entries = [m for m in loaded if isinstance(m, dict) and (m.get("size") or 0) > 0]
        if not entries:
            return None
        m = max(entries, key=lambda e: e.get("size") or 0)
        size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
        gpu_pct = round(100 * min(vram, size) / size) if size else 0
        name = m.get("name") or m.get("model") or "model"
        if gpu_pct >= 95:
            return {"compute": "gpu", "compute_detail": f"'{name}' is fully loaded on the GPU."}
        if gpu_pct <= 0:
            return {"compute": "cpu", "compute_detail": f"'{name}' is running on the CPU (no GPU in use)."}
        return {"compute": "partial",
                "compute_detail": f"'{name}' is split {gpu_pct}% GPU / {100 - gpu_pct}% CPU -- "
                                  "not enough VRAM for the whole model."}

    async def test(self) -> dict[str, Any]:
        """Make one real round trip (a tiny chat, plus an embedding when the
        provider supplies them) so an admin can check credentials before saving."""
        if not self.enabled:
            return {"ok": False, "detail": LlmDisabledError().args[0]}
        problems = self.config.problems()
        if problems:
            return {"ok": False, "detail": " ".join(problems)}
        started = time.monotonic()
        try:
            reply = await self.chat([{"role": "user", "content": "Reply with the single word OK."}])
        except LlmError as exc:
            return {"ok": False, "detail": str(exc)}
        chat_ms = int((time.monotonic() - started) * 1000)
        detail = f"Chat OK via {self.config.label} ('{self.model}', {chat_ms} ms; replied: {reply[:40]!r})."
        dims: int | None = None
        if self.embeddings_enabled:
            try:
                vector = await self.embed("connection test")
                dims = len(vector)
                detail += f" Embeddings OK ({dims} dimensions"
                expected = self.settings.memory_embedding_dims
                if dims != expected:
                    detail += (f"; MEMORY_EMBEDDING_DIMS is {expected} -- set it to {dims} and re-run "
                               "memory-init or memory recall will not match")
                detail += ")."
            except LlmError as exc:
                return {"ok": False, "detail": f"{detail} Embeddings failed: {exc}", "chat_ms": chat_ms}
        else:
            detail += " Embeddings are off (memory recall is recency-only)."
        return {"ok": True, "detail": detail, "chat_ms": chat_ms, "embedding_dims": dims}


# Name the rest of the code base (and older tests) know the client by.
QwenAgentClient = LlmClient
