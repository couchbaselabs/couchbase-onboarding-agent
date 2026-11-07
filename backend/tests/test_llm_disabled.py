"""LLM_ENABLED=false: the app serves, migrations run, memory records events,
and the assistant endpoints answer clearly without contacting anything."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.qwen_agent import LlmDisabledError, QwenAgentClient
from app.memory import couchbase_memory as mem


@pytest.fixture
def llm_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_enabled", False)
    monkeypatch.setattr(get_settings(), "api_auth_token", "")
    yield


async def test_client_short_circuits_without_network(llm_off, monkeypatch):
    import httpx

    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("the LLM service was contacted while disabled")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    client = QwenAgentClient()
    assert (await client.status())[0] == "disabled"
    with pytest.raises(LlmDisabledError):
        await client.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(LlmDisabledError):
        await client.embed("x")


def test_endpoints_report_disabled(llm_off):
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/config").json()["llm_enabled"] is False
        assert client.get("/api/agent/status").json()["status"] == "disabled"
        r = client.post("/api/agent/chat", json={"message": "hello", "use_memory": False})
        assert r.status_code == 503 and "LLM_ENABLED=false" in r.json()["detail"]
        # the rule-based recommendation needs no model
        topo = {"source_type": "mongodb", "containers": [], "supports_cdc": True}
        r = client.post("/api/agent/recommend-replication-mode",
                        json={"cutover_plan": "cutover", "source_topology": topo, "concurrency": 8})
        assert r.status_code == 200 and r.json()["recommended_strategy"]


async def test_memory_stores_without_vectors_and_recalls_recent(llm_off, monkeypatch):
    stored: dict = {}

    class Col:
        def upsert(self, key, doc):
            stored[key] = doc

    store = mem.AgentMemoryStore.__new__(mem.AgentMemoryStore)
    store.settings = get_settings()
    monkeypatch.setattr(store, "_collection", lambda: (Col(), None))

    async def no_embed(text):  # pragma: no cover
        raise AssertionError("embedding attempted while disabled")

    monkeypatch.setattr(mem, "embed_text", no_embed)
    await store.remember("validation_result", {"passed": True})
    (doc,) = stored.values()
    assert doc["embedding"] == [] and doc["kind"] == "validation_result"

    monkeypatch.setattr(store, "_recall_recent", lambda limit: [{"text": "recent", "kind": "x"}])
    assert await store.recall("anything") == [{"text": "recent", "kind": "x"}]
