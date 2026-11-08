"""Bring-your-own model: provider config, persistence (key encrypted at rest),
and the request shapes sent to each provider -- all against an httpx mock
transport, so nothing leaves the process."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.llm import EmbeddingsUnavailableError, LlmClient, LlmError
from app.core.llm_settings import EmbeddingSource, LlmProvider, LlmProviderConfig, LlmSettingsStore


@pytest.fixture(autouse=True)
def fresh_llm_store(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_enabled", True)
    monkeypatch.setattr(get_settings(), "api_auth_token", "")
    LlmSettingsStore._instance = None
    LlmSettingsStore.instance().reset()
    yield
    LlmSettingsStore.instance().reset()
    LlmSettingsStore._instance = None


class Recorder:
    """Captures every request LlmClient makes and answers with canned bodies."""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self.responder = responder

    def transport(self):
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.responder(request)
        return httpx.MockTransport(handle)


@pytest.fixture
def mock_http(monkeypatch):
    holder: dict = {}

    def install(responder):
        rec = Recorder(responder)
        real_init = httpx.AsyncClient.__init__

        def patched(self, *a, **kw):
            kw["transport"] = rec.transport()
            real_init(self, *a, **kw)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        holder["rec"] = rec
        return rec
    return install


def _body(req: httpx.Request) -> dict:
    return json.loads(req.content)


# --------------------------------------------------------------- config

def test_problems_and_defaults():
    assert LlmProviderConfig().problems() == []  # local needs nothing
    c = LlmProviderConfig(provider="openai")
    assert any("model" in p.lower() for p in c.problems()) and any("API key" in p for p in c.problems())
    c = LlmProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-x")
    assert c.problems() == [] and c.effective_base_url == "https://api.openai.com/v1"
    c = LlmProviderConfig(provider="openai_compatible", model="llama", api_key="")
    assert any("base URL" in p for p in c.problems())
    c = LlmProviderConfig(provider="anthropic", model="claude-x", api_key="k", embedding_source="provider",
                          embedding_model="e")
    assert any("no embeddings API" in p for p in c.problems())
    with pytest.raises(ValueError):
        LlmProviderConfig(base_url="ftp://nope")
    assert LlmProviderConfig(base_url="https://x/v1/").base_url == "https://x/v1"


def test_saved_config_is_encrypted_and_overrides_env():
    store = LlmSettingsStore.instance()
    assert store.source() == "env" and store.effective().is_local
    store.save(LlmProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-secret-123"))
    raw = store.path().read_text()
    assert "sk-secret-123" not in raw and "enc:v1:" in raw
    LlmSettingsStore._instance = None
    reloaded = LlmSettingsStore.instance()
    assert reloaded.source() == "ui" and reloaded.effective().api_key == "sk-secret-123"
    assert reloaded.effective().redacted()["api_key"] == "********"
    reloaded.reset()
    assert reloaded.source() == "env"


# --------------------------------------------------------------- wire shapes

async def test_openai_chat_and_embeddings(mock_http):
    def respond(req):
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": " hello "}}]})
        if req.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        return httpx.Response(404)
    rec = mock_http(respond)
    cfg = LlmProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-1",
                            embedding_source="provider", embedding_model="text-embedding-3-small")
    client = LlmClient(cfg)
    assert await client.chat([{"role": "user", "content": "hi"}], context={"phase": "draft"}) == "hello"
    req = rec.requests[0]
    assert req.url == "https://api.openai.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-1"
    body = _body(req)
    assert body["model"] == "gpt-4o-mini" and body["messages"][0]["role"] == "system"
    assert "phase" in body["messages"][0]["content"] and body["messages"][-1]["content"] == "hi"
    assert await client.embed("x") == [0.1, 0.2]
    assert _body(rec.requests[1]) == {"input": "x", "model": "text-embedding-3-small"}
    assert (await client.status())[0] == "ready" and len(rec.requests) == 2  # status makes no request


async def test_anthropic_chat_shape_and_no_embeddings(mock_http):
    rec = mock_http(lambda req: httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]}))
    cfg = LlmProviderConfig(provider="anthropic", model="claude-sonnet-4-5", api_key="ant-1",
                            embedding_source="none")
    client = LlmClient(cfg)
    assert await client.chat([{"role": "user", "content": "hi"}]) == "OK"
    req = rec.requests[0]
    assert req.url == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "ant-1" and req.headers["anthropic-version"]
    body = _body(req)
    assert "system" in body and body["messages"] == [{"role": "user", "content": "hi"}] and body["max_tokens"] == 1024
    assert not client.embeddings_enabled
    with pytest.raises(EmbeddingsUnavailableError):
        await client.embed("x")


async def test_azure_openai_urls(mock_http):
    rec = mock_http(lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": "ok"}}], "data": [{"embedding": [1.0]}]}))
    cfg = LlmProviderConfig(provider="azure_openai", model="gpt4o-deploy", api_key="az", api_version="2024-10-21",
                            base_url="https://acme.openai.azure.com", embedding_source="provider",
                            embedding_model="embed-deploy")
    client = LlmClient(cfg)
    await client.chat([{"role": "user", "content": "hi"}])
    await client.embed("x")
    chat, emb = rec.requests
    assert str(chat.url) == "https://acme.openai.azure.com/openai/deployments/gpt4o-deploy/chat/completions?api-version=2024-10-21"
    assert chat.headers["api-key"] == "az" and "model" not in _body(chat)
    assert str(emb.url).startswith("https://acme.openai.azure.com/openai/deployments/embed-deploy/embeddings")


async def test_openai_compatible_with_local_embeddings(mock_http, monkeypatch):
    def respond(req):
        if req.url.host == "localhost":
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        return httpx.Response(200, json={"embedding": [0.5]})
    rec = mock_http(respond)
    monkeypatch.setattr(get_settings(), "qwen_base_url", "http://qwen-service:11434")
    cfg = LlmProviderConfig(provider="openai_compatible", model="llama3", base_url="http://localhost:8080/v1",
                            embedding_source="local")
    client = LlmClient(cfg)
    await client.chat([{"role": "user", "content": "hi"}])
    assert "authorization" not in rec.requests[0].headers  # no key configured -> no header
    assert await client.embed("x") == [0.5]
    assert rec.requests[1].url.host == "qwen-service" and rec.requests[1].url.path == "/api/embeddings"


async def test_http_errors_are_wrapped_without_the_key(mock_http):
    mock_http(lambda req: httpx.Response(401, json={"error": "bad key"}))
    cfg = LlmProviderConfig(provider="openai", model="m", api_key="sk-verysecret")
    with pytest.raises(LlmError) as exc:
        await LlmClient(cfg).chat([{"role": "user", "content": "hi"}])
    assert "401" in str(exc.value) and "sk-verysecret" not in str(exc.value)
    result = await LlmClient(cfg).test()
    assert result["ok"] is False and "401" in result["detail"]


async def test_connection_test_reports_embedding_dims_mismatch(mock_http, monkeypatch):
    def respond(req):
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 1536}]})
    mock_http(respond)
    monkeypatch.setattr(get_settings(), "memory_embedding_dims", 768)
    cfg = LlmProviderConfig(provider="openai", model="m", api_key="k", embedding_source="provider",
                            embedding_model="e")
    result = await LlmClient(cfg).test()
    assert result["ok"] and result["embedding_dims"] == 1536 and "MEMORY_EMBEDDING_DIMS is 768" in result["detail"]


# --------------------------------------------------------------- API

def test_provider_endpoints_round_trip(mock_http):
    from app.main import app

    mock_http(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    with TestClient(app) as client:
        r = client.get("/api/agent/provider").json()
        assert r["source"] == "env" and r["config"]["provider"] == "local"
        assert {p["value"] for p in r["providers"]} == {p.value for p in LlmProvider}

        bad = client.put("/api/agent/provider", json={"provider": "openai", "model": "m"})
        assert bad.status_code == 422 and "API key" in bad.json()["detail"]

        t = client.post("/api/agent/provider/test", json={"provider": "openai", "model": "m", "api_key": "k",
                                                          "embedding_source": "none"}).json()
        assert t["ok"] and "Chat OK" in t["detail"]

        r = client.put("/api/agent/provider", json={"provider": "openai", "model": "m", "api_key": "sk-abc",
                                                     "embedding_source": EmbeddingSource.NONE.value}).json()
        assert r["source"] == "ui" and r["config"]["api_key"] == "********" and r["config"]["api_key_set"]
        assert "sk-abc" not in json.dumps(r)

        # a redacted key on PUT keeps the stored one
        r = client.put("/api/agent/provider", json={"provider": "openai", "model": "m2", "api_key": "********",
                                                     "embedding_source": "none"})
        assert r.status_code == 200 and LlmSettingsStore.instance().effective().api_key == "sk-abc"

        s = client.get("/api/agent/status").json()
        assert s["status"] == "ready" and s["provider"] == "openai" and s["local"] is False and s["model"] == "m2"

        chat = client.post("/api/agent/chat", json={"message": "hello", "use_memory": False})
        assert chat.status_code == 200 and chat.json()["reply"] == "OK"

        r = client.delete("/api/agent/provider").json()
        assert r["source"] == "env" and r["config"]["provider"] == "local"


# --------------------------------------------------------------- compute (GPU/CPU)

def _ps(size: int, vram: int, name: str = "qwen3:4b") -> dict:
    return {"models": [{"name": name, "model": name, "size": size, "size_vram": vram},
                       {"name": "nomic-embed-text:latest", "size": 300, "size_vram": 300}]}


def test_classify_loaded_prefers_chat_model():
    assert LlmClient._classify_loaded([]) is None
    assert LlmClient._classify_loaded(_ps(1000, 1000)["models"])["compute"] == "gpu"
    assert LlmClient._classify_loaded(_ps(1000, 0)["models"])["compute"] == "cpu"
    partial = LlmClient._classify_loaded(_ps(1000, 600)["models"])
    assert partial["compute"] == "partial" and "60% GPU" in partial["compute_detail"]


@pytest.mark.asyncio
async def test_status_payload_reports_compute_and_remembers_it(mock_http, monkeypatch):
    LlmClient._compute_cache.clear()
    ps_body: dict = _ps(1000, 1000)

    def responder(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}, {"name": "nomic-embed-text:latest"}]})
        if req.url.path == "/api/ps":
            return httpx.Response(200, json=ps_body)
        return httpx.Response(404)

    mock_http(responder)
    settings = get_settings()
    monkeypatch.setattr(settings, "qwen_model_name", "qwen3:4b")
    monkeypatch.setattr(settings, "qwen_embedding_model_name", "nomic-embed-text")

    payload = await LlmClient().status_payload()
    assert payload["status"] == "ready" and payload["compute"] == "gpu"

    # Model idles out: /api/ps is empty, but the last observation is kept.
    ps_body = {"models": []}
    payload = await LlmClient().status_payload()
    assert payload["compute"] == "gpu" and "last observed" in payload["compute_detail"]

    # Hosted providers never report compute.
    LlmClient._compute_cache.clear()
    cfg = LlmProviderConfig(provider=LlmProvider.ANTHROPIC, api_key="k", model="claude-x",
                            embedding_source=EmbeddingSource.NONE)
    payload = await LlmClient(cfg).status_payload()
    assert "compute" not in payload
