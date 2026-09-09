"""
Common interface every source database connector implements. See
mongodb.py / dynamodb.py / redis_connector.py / cassandra_connector.py / cosmosdb.py
for the five implementations, and registry.py for how a SourceConnectionConfig
selects one.

Design notes:
- Every connector is READ-ONLY against the source. Nothing in this app ever writes
  to, deletes from, or modifies the source database -- see the top-level README's
  "Why there's no separate backup step" for why that makes a cbbackupmgr-style
  pre-migration backup/rollback-of-the-source unnecessary here (contrast with the
  sibling couchbase-migration-agent project, whose source and destination are both
  Couchbase and whose migration tooling can and does touch the source).
- extract() streams SourceDocument batches through `sink` rather than returning a
  big list, so a multi-million-row table doesn't have to fit in memory, and so the
  caller (MigrationEngine) can report progress incrementally.
- stream_changes() only has to do something real on sources where supports_cdc is
  True; the base implementation raises a clear error for the rest.
"""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.models.schemas import SourceConnectionConfig, SourceTopologySnapshot


@dataclass
class SourceDocument:
    """One extracted/changed record, already transformed into a Couchbase-ready
    JSON document by the connector."""

    key: str
    body: dict[str, Any]
    container: str


@dataclass
class ChangeEvent:
    """One CDC event yielded from stream_changes()."""

    container: str
    op: str  # "upsert" | "delete"
    document: SourceDocument | None = None  # set when op == "upsert"
    key: str | None = None                   # set when op == "delete"
    # Opaque, connector-defined resume token to persist (per container) into
    # MigrationRecord.checkpoint once this event has been durably written to
    # Couchbase -- see MigrationEngine._monitor_cdc().
    checkpoint: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


class SourceConnectorError(RuntimeError):
    pass


class SourceConnector(abc.ABC):
    def __init__(self, config: SourceConnectionConfig):
        self.config = config

    @property
    @abc.abstractmethod
    def supports_cdc(self) -> bool:
        """Whether stream_changes() is implemented for this connector class at all.
        Per-instance availability (e.g. "this Mongo isn't a replica set", "DynamoDB
        Streams isn't enabled on this table") is a separate, run-time check
        surfaced via SourceTopologySnapshot.supports_cdc/cdc_notes from
        test_connection()."""

    @abc.abstractmethod
    async def test_connection(self) -> SourceTopologySnapshot:
        """Connect, enumerate containers, and return best-effort size/count
        estimates plus CDC availability. Should not raise for a healthy connection
        even when some stats are unavailable -- degrade into `notes` instead."""

    @abc.abstractmethod
    async def extract(
        self,
        containers: list[str],
        sink: Callable[[list[SourceDocument]], Awaitable[None]],
        *,
        batch_size: int = 500,
    ) -> None:
        """Full extraction of `containers`, invoking `sink` with batches of
        SourceDocument as they're read. Couchbase upserts are naturally idempotent,
        so MigrationEngine treats a full load as safe to simply restart from the
        beginning of a container on retry -- extract() doesn't need to support
        resuming mid-container."""

    def stream_changes(
        self,
        containers: list[str],
        checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        """Yield ChangeEvents indefinitely (until the caller stops iterating) for
        continuous replication. `checkpoint` is this migration's last-persisted
        per-container resume token (MigrationRecord.checkpoint), used to resume from
        where a previous run left off. Only meaningful when supports_cdc is True."""
        raise SourceConnectorError(
            f"{type(self).__name__} does not implement continuous change-data-capture."
        )

    async def close(self) -> None:
        """Release any pooled connections/clients. Safe to call multiple times."""
        return None
