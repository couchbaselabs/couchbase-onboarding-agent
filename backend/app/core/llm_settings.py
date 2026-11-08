"""Which model the assistant runs on, and how to reach it.

The default is the bundled local model (Ollama-compatible `qwen-service`). An
administrator can instead point the assistant at a model of their choosing --
OpenAI, Azure OpenAI, Anthropic, or any OpenAI-compatible endpoint (Groq,
Together, OpenRouter, vLLM, LM Studio, Gemini's OpenAI-compatible API, ...) --
from the UI's Settings page or at first-run setup. That choice is persisted
next to the migration state file (`llm.json`) with the API key encrypted the
same way stored database credentials are (core/secrets.py), and takes
precedence over the `LLM_*` environment defaults.

Only the assistant chat (and, optionally, agent-memory embeddings) use the
model. Nothing on a migration's critical path does, and the prompt context
never includes database credentials -- but validation messages, migration
stats and the user's questions ARE sent to whichever provider is configured,
which is why the UI says so when a hosted provider is selected.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.core.secrets import SecretBox
from app.models.schemas import REDACTED

logger = logging.getLogger(__name__)


class LlmProvider(str, Enum):
    LOCAL = "local"                        # bundled Ollama-compatible service (qwen-service)
    OPENAI = "openai"                      # api.openai.com
    OPENAI_COMPATIBLE = "openai_compatible"  # any /v1/chat/completions endpoint
    AZURE_OPENAI = "azure_openai"          # <resource>.openai.azure.com deployments
    ANTHROPIC = "anthropic"                # api.anthropic.com


class EmbeddingSource(str, Enum):
    """Where agent-memory embeddings come from when a hosted chat provider is
    selected. The vector index has a fixed dimension (MEMORY_EMBEDDING_DIMS), so
    switching embedding models also means re-running memory-init."""

    LOCAL = "local"    # keep using the local Ollama embedding model
    PROVIDER = "provider"  # the chat provider's embeddings endpoint (OpenAI-style only)
    NONE = "none"      # no embeddings: memory still records events, recall is recency-only


DEFAULT_BASE_URLS: dict[LlmProvider, str] = {
    LlmProvider.OPENAI: "https://api.openai.com/v1",
    LlmProvider.ANTHROPIC: "https://api.anthropic.com",
}

PROVIDER_LABELS: dict[LlmProvider, str] = {
    LlmProvider.LOCAL: "Local model (bundled)",
    LlmProvider.OPENAI: "OpenAI",
    LlmProvider.OPENAI_COMPATIBLE: "OpenAI-compatible endpoint",
    LlmProvider.AZURE_OPENAI: "Azure OpenAI",
    LlmProvider.ANTHROPIC: "Anthropic",
}

# Providers whose embeddings endpoint follows OpenAI's /embeddings shape.
OPENAI_STYLE = frozenset({LlmProvider.OPENAI, LlmProvider.OPENAI_COMPATIBLE, LlmProvider.AZURE_OPENAI})


class LlmProviderConfig(BaseModel):
    provider: LlmProvider = LlmProvider.LOCAL
    # Chat model name; for Azure OpenAI this is the *deployment* name.
    model: str = ""
    # Endpoint. Optional for openai/anthropic (their public API), required for
    # openai_compatible (e.g. https://api.groq.com/openai/v1) and azure_openai
    # (https://<resource>.openai.azure.com). For `local` it's the Ollama server.
    base_url: str = ""
    api_key: str = ""
    # Azure OpenAI only.
    api_version: str = "2024-10-21"
    embedding_source: EmbeddingSource = EmbeddingSource.LOCAL
    # Embedding model (deployment name on Azure) when embedding_source=provider.
    embedding_model: str = ""
    request_timeout_s: int = Field(default=120, ge=5, le=900)
    max_tokens: int = Field(default=1024, ge=64, le=32768)

    @field_validator("model", "base_url", "api_key", "embedding_model", "api_version", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.rstrip("/")
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v

    # --- derived -----------------------------------------------------------

    @property
    def is_local(self) -> bool:
        return self.provider == LlmProvider.LOCAL

    @property
    def effective_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        if self.is_local:
            return get_settings().qwen_base_url.rstrip("/")
        return DEFAULT_BASE_URLS.get(self.provider, "")

    @property
    def effective_model(self) -> str:
        if self.model:
            return self.model
        return get_settings().qwen_model_name if self.is_local else ""

    @property
    def label(self) -> str:
        return PROVIDER_LABELS[self.provider]

    def problems(self) -> list[str]:
        """Configuration errors a user can act on (checked before saving and
        before any request is made)."""
        out: list[str] = []
        if self.is_local:
            return out
        if not self.effective_model:
            out.append("Choose a model" + (" (deployment name)" if self.provider == LlmProvider.AZURE_OPENAI else "") + ".")
        if not self.api_key and self.provider != LlmProvider.OPENAI_COMPATIBLE:
            out.append("An API key is required.")
        if self.provider in (LlmProvider.OPENAI_COMPATIBLE, LlmProvider.AZURE_OPENAI) and not self.base_url:
            out.append("A base URL is required for this provider.")
        if self.embedding_source == EmbeddingSource.PROVIDER:
            if self.provider not in OPENAI_STYLE:
                out.append(f"{self.label} has no embeddings API; use the local embedding model or turn embeddings off.")
            elif not self.embedding_model:
                out.append("Choose an embedding model, or use the local embedding model.")
        return out

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["api_key"] = REDACTED if self.api_key else ""
        data["api_key_set"] = bool(self.api_key)
        return data


def config_from_env() -> LlmProviderConfig:
    """Defaults from the environment (LLM_PROVIDER, LLM_MODEL, LLM_BASE_URL,
    LLM_API_KEY, ...). Used until an admin saves a choice in the UI."""
    s = get_settings()
    try:
        provider = LlmProvider(s.llm_provider.strip().lower() or "local")
    except ValueError:
        logger.warning("LLM_PROVIDER=%r is not one of %s; using the local model.",
                       s.llm_provider, [p.value for p in LlmProvider])
        provider = LlmProvider.LOCAL
    try:
        embedding_source = EmbeddingSource(s.llm_embedding_source.strip().lower() or "local")
    except ValueError:
        embedding_source = EmbeddingSource.LOCAL
    return LlmProviderConfig(
        provider=provider,
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        api_version=s.llm_azure_api_version or "2024-10-21",
        embedding_source=embedding_source,
        embedding_model=s.llm_embedding_model,
        request_timeout_s=s.qwen_request_timeout_s,
    )


class LlmSettingsStore:
    """Persists the admin's provider choice. `None` means "nothing saved; use
    the environment defaults"."""

    _instance: LlmSettingsStore | None = None

    def __init__(self) -> None:
        self._cached: LlmProviderConfig | None = None
        self._loaded = False

    @classmethod
    def instance(cls) -> LlmSettingsStore:
        if cls._instance is None:
            cls._instance = LlmSettingsStore()
        return cls._instance

    @staticmethod
    def path() -> Path:
        return Path(get_settings().migration_state_file).parent / "llm.json"

    def saved(self) -> Optional[LlmProviderConfig]:
        if not self._loaded:
            self._loaded = True
            p = self.path()
            if p.exists():
                try:
                    raw = json.loads(p.read_text())
                    self._cached = LlmProviderConfig.model_validate(SecretBox.instance().decrypt_tree(raw))
                except Exception as exc:  # noqa: BLE001
                    logger.error("Could not read %s (%s); falling back to the environment defaults.", p, exc)
        return self._cached

    def effective(self) -> LlmProviderConfig:
        return self.saved() or config_from_env()

    def source(self) -> str:
        return "ui" if self.saved() is not None else "env"

    def save(self, config: LlmProviderConfig) -> LlmProviderConfig:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = SecretBox.instance().encrypt_tree(config.model_dump(mode="json"))
        fd, tmp = tempfile.mkstemp(prefix=".llm.", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._cached = config
        self._loaded = True
        logger.info("Assistant model set to %s / %s", config.label, config.effective_model or "(default)")
        return config

    def reset(self) -> None:
        p = self.path()
        if p.exists():
            p.unlink()
        self._cached = None
        self._loaded = True
