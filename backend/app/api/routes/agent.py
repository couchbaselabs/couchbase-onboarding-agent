"""Conversational endpoint for the AI assistant panel: answers questions about a
migration in progress, recalling relevant memories from Couchbase EE vector search
and grounding responses in the migration's actual validation report / stats."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.core.qwen_agent import QwenAgentClient
from app.core.recommendation import recommend_replication_mode
from app.core.store import MigrationStore
from app.memory.couchbase_memory import AgentMemoryStore
from app.models.schemas import (
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
    state, detail = await QwenAgentClient().status()
    return AgentStatusResponse(status=state, detail=detail)


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

    client = QwenAgentClient()
    try:
        reply = await client.chat([{"role": "user", "content": req.message}], context=context)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"Local LLM (Qwen) unavailable: {exc}") from exc

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
