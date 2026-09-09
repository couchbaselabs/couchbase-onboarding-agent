"""
Agent memory store backed by Couchbase Enterprise Edition (or Capella), using
native Couchbase Vector Search (FTS) for recall.

Every significant migration event (validation results, connector-introspection
findings, CDC failures, rollback reasons, user Q&A) is embedded with Qwen and
written to a Couchbase collection. recall() runs a native ANN vector query against
the FTS index defined in couchbase-memory/vector_index.json (created by
scripts/init_memory.py at container startup -- see the memory-init service in
docker-compose.yml).

If that vector index is ever unavailable (e.g. momentarily still building on first
boot), recall() falls back to a brute-force cosine-similarity scan over the same
embeddings via N1QL, purely as a resilience net -- it is not the expected
steady-state path. This module is intentionally near-identical to the sibling
couchbase-migration-agent project's -- the memory subsystem itself doesn't depend
on what kind of migration is being run.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any
from uuid import uuid4

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.options import ClusterOptions, SearchOptions
from couchbase.vector_search import VectorQuery, VectorSearch

from app.config import get_settings
from app.memory.embeddings import embed_text, summarize_for_embedding

logger = logging.getLogger(__name__)

FALLBACK_SCAN_LIMIT = 500


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class AgentMemoryStore:
    """Singleton-ish accessor for the agent's long-term memory in Couchbase."""

    _instance: "AgentMemoryStore | None" = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self._cluster: Cluster | None = None
        self._vector_search_unavailable = False

    @classmethod
    def instance(cls) -> "AgentMemoryStore":
        if cls._instance is None:
            cls._instance = AgentMemoryStore()
        return cls._instance

    def _connect(self) -> Cluster:
        if self._cluster is not None:
            return self._cluster
        auth = PasswordAuthenticator(self.settings.memory_cb_username, self.settings.memory_cb_password)
        cluster = Cluster(self.settings.memory_cb_connection_string, ClusterOptions(auth))
        cluster.wait_until_ready(timeout=15)
        self._cluster = cluster
        return cluster

    def _collection(self):
        cluster = self._connect()
        bucket = cluster.bucket(self.settings.memory_cb_bucket)
        scope = bucket.scope(self.settings.memory_cb_scope)
        return scope.collection(self.settings.memory_cb_collection), scope

    def _keyspace(self) -> str:
        return (
            f"`{self.settings.memory_cb_bucket}`."
            f"`{self.settings.memory_cb_scope}`."
            f"`{self.settings.memory_cb_collection}`"
        )

    async def remember(self, kind: str, payload: dict[str, Any], migration_id: str | None = None) -> str:
        """Embed and persist a memory event. Returns the memory document id."""
        text = summarize_for_embedding(kind, payload)
        try:
            vector = await embed_text(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding failed, storing memory without vector: %s", exc)
            vector = []

        doc_id = f"mem::{uuid4()}"
        collection, _scope = self._collection()
        collection.upsert(doc_id, {
            "kind": kind,
            "migration_id": migration_id,
            "text": text,
            "payload": payload,
            "embedding": vector,
            "created_at": datetime.utcnow().isoformat(),
        })
        return doc_id

    async def recall(self, query_text: str, limit: int = 5) -> list[dict[str, Any]]:
        try:
            query_vector = await embed_text(query_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding failed for recall query, returning no memories: %s", exc)
            return []

        if not self._vector_search_unavailable:
            try:
                return self._recall_via_vector_search(query_vector, limit)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Vector search recall failed (%s) -- switching to N1QL "
                    "cosine-similarity fallback for memory recall until the FTS "
                    "index becomes available.", exc
                )
                self._vector_search_unavailable = True

        try:
            return self._recall_via_cosine_fallback(query_vector, limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cosine-similarity fallback recall failed: %s", exc)
            return []

    def _recall_via_vector_search(self, query_vector: list[float], limit: int) -> list[dict[str, Any]]:
        cluster = self._connect()
        request = VectorSearch.from_vector_query(
            VectorQuery("embedding", query_vector, num_candidates=max(limit * 4, 20))
        )
        result = cluster.search(
            self.settings.memory_cb_vector_index,
            request,
            SearchOptions(limit=limit, fields=["kind", "migration_id", "text", "payload", "created_at"]),
        )
        memories = []
        for row in result.rows():
            memories.append({
                "id": row.id,
                "score": row.score,
                **(row.fields or {}),
            })
        return memories

    def _recall_via_cosine_fallback(self, query_vector: list[float], limit: int) -> list[dict[str, Any]]:
        cluster = self._connect()
        query = (
            f"SELECT META(m).id AS id, m.kind, m.migration_id, m.text, m.payload, "
            f"m.embedding, m.created_at "
            f"FROM {self._keyspace()} AS m "
            f"WHERE m.embedding IS NOT MISSING AND ARRAY_LENGTH(m.embedding) > 0 "
            f"ORDER BY m.created_at DESC "
            f"LIMIT {FALLBACK_SCAN_LIMIT}"
        )
        result = cluster.query(query)

        scored: list[tuple[float, dict[str, Any]]] = []
        for row in result:
            score = _cosine_similarity(query_vector, row.get("embedding") or [])
            scored.append((score, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        memories = []
        for score, row in scored[:limit]:
            row = dict(row)
            row.pop("embedding", None)
            row["score"] = score
            memories.append(row)
        return memories

    def close(self) -> None:
        if self._cluster is not None:
            self._cluster.close()
            self._cluster = None
