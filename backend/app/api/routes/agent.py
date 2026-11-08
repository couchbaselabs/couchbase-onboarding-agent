"""Conversational endpoint for the AI assistant panel: answers questions about a
migration in progress, recalling relevant memories from Couchbase EE vector search
and grounding responses in the migration's actual validation report / stats."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from app.core.llm import LlmClient, LlmDisabledError
from app.core.llm_settings import (
    PROVIDER_LABELS,
    EmbeddingSource,
    LlmProvider,
    LlmProviderConfig,
    LlmSettingsStore,
)
from app.core.recommendation import recommend_replication_mode
from app.core.store import MigrationStore
from app.memory.couchbase_memory import AgentMemoryStore
from app.models.schemas import (
    REDACTED,
    AgentChatRequest,
    AgentChatResponse,
    AgentStatusResponse,
    ReplicationModeRecommendationRequest,
    ReplicationModeRecommendationResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status", response_model=AgentStatusResponse)
async def status() -> AgentStatusResponse:
    return AgentStatusResponse(**await LlmClient().status_payload())


# ---------------------------------------------------------------------------
# Assistant model provider (bring your own model)
# ---------------------------------------------------------------------------

def _provider_payload() -> dict:
    store = LlmSettingsStore.instance()
    return {"config": store.effective().redacted(), "source": store.source()}


@router.get("/provider")
async def get_provider() -> dict:
    """The configured assistant model (API key redacted) and whether it came
    from the UI (`source: ui`) or the environment defaults (`source: env`)."""
    return {
        **_provider_payload(),
        "providers": [
            {
                "value": p.value, "label": PROVIDER_LABELS[p],
                "needs_api_key": p not in (LlmProvider.LOCAL, LlmProvider.OPENAI_COMPATIBLE),
                "needs_base_url": p in (LlmProvider.OPENAI_COMPATIBLE, LlmProvider.AZURE_OPENAI),
                "has_embeddings": p in (LlmProvider.OPENAI, LlmProvider.OPENAI_COMPATIBLE, LlmProvider.AZURE_OPENAI),
            }
            for p in LlmProvider
        ],
        "embedding_sources": [e.value for e in EmbeddingSource],
    }


def _merge(payload: dict) -> LlmProviderConfig:
    """Build a config from a PUT/test body; a redacted api_key means "keep the
    one already saved" so the UI never has to round-trip the secret."""
    if payload.get("api_key") in (REDACTED, None):
        payload = {**payload, "api_key": LlmSettingsStore.instance().effective().api_key}
    try:
        return LlmProviderConfig.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "; ".join(e.get("msg", "") for e in exc.errors())) from exc


@router.post("/provider/test")
async def test_provider(payload: dict) -> dict:
    """One real round trip against the given (unsaved) configuration."""
    config = _merge(payload)
    problems = config.problems()
    if problems:
        return {"ok": False, "detail": " ".join(problems)}
    return await LlmClient(config).test()


@router.put("/provider")
async def put_provider(payload: dict) -> dict:
    config = _merge(payload)
    problems = config.problems()
    if problems:
        raise HTTPException(422, " ".join(problems))
    LlmSettingsStore.instance().save(config)
    return _provider_payload()


@router.delete("/provider")
async def reset_provider() -> dict:
    """Forget the UI choice and go back to the environment defaults."""
    LlmSettingsStore.instance().reset()
    return _provider_payload()


@router.post("/chat", response_model=AgentChatResponse)
async def chat(req: AgentChatRequest) -> AgentChatResponse:
    context: dict = {}
    recalled: list[str] = []

    if req.migration_id:
        record = await MigrationStore.instance().get(req.migration_id)
        if not record:
            raise HTTPException(404, "Migration not found")
        context["migration_name"] = record.plan.name
        context["source_type"] = record.plan.source.source_type.value
        context["phase"] = record.phase.value
        context["stats"] = record.stats.model_dump()
        if record.validation_report:
            context["validation_summary"] = [
                {"check": c.label, "passed": c.passed, "message": c.message}
                for c in record.validation_report.checks
            ]

    if req.use_memory:
        try:
            memories = await AgentMemoryStore.instance().recall(req.message, limit=5)
            recalled = [m.get("text", "") for m in memories]
            if recalled:
                context["recalled_similar_events"] = recalled
        except Exception as exc:  # noqa: BLE001
            logger.warning("Memory recall failed: %s", exc)

    client = LlmClient()
    try:
        reply = await client.chat([{"role": "user", "content": req.message}], context=context)
    except LlmDisabledError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Assistant model ({client.config.label}) unavailable: {exc}") from exc

    try:
        await AgentMemoryStore.instance().remember(
            "chat_exchange",
            {"user_message": req.message, "assistant_reply": reply},
            migration_id=str(req.migration_id) if req.migration_id else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to persist chat memory: %s", exc)

    return AgentChatResponse(reply=reply, recalled_memories=recalled)


@router.post("/recommend-replication-mode", response_model=ReplicationModeRecommendationResponse)
async def recommend_mode(req: ReplicationModeRecommendationRequest) -> ReplicationModeRecommendationResponse:
    if req.cutover_plan not in ("cutover", "phased"):
        raise HTTPException(400, "cutover_plan must be 'cutover' or 'phased'")
    result = recommend_replication_mode(req.cutover_plan, req.source_topology, req.concurrency)  # type: ignore[arg-type]
    return ReplicationModeRecommendationResponse(
        recommended_strategy=result.recommended_strategy,
        headline=result.headline,
        rationale=result.rationale,
        considerations=result.considerations,
        estimated_duration_seconds=result.estimated_duration_seconds,
    )
