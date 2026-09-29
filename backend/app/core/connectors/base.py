"""
Common interface every source connector implements. See mongodb.py,
dynamodb.py, redis_connector.py, cassandra_connector.py, cosmosdb.py and
couchbase_source.py for the implementations, and registry.py for how a
SourceConnectionConfig selects one.

Forward migration (source -> Couchbase) is read-only against the source:
  - test_connection()          introspection for the wizard and validator
  - extract()                  full, batched read (bulk copy)
  - capture_change_position()  record "now" in the source's change log BEFORE a
                               bulk copy starts, so changes made during the copy
                               are replayed afterwards instead of lost
  - stream_changes()           ongoing change events from a checkpoint
  - list_keys()                every document key, for delete reconciliation

Post-cutover fallback (Couchbase -> source) is the one path that writes to the
source, and only when an operator starts it explicitly:
  - apply_reverse()            upsert or delete one document at the source

Change events are applied after the bulk copy finishes, in source order per key,
and every write is an idempotent upsert/delete keyed deterministically -- so
replaying a change that the bulk copy already saw is harmless. That is what lets
each connector hand the engine an at-least-once stream with a slightly early
start position rather than an exactly-once one.
"""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.enums import CdcMethod
from app.models.schemas import SourceConnectionConfig, SourceTopologySnapshot


@dataclass
class SourceDocument:
    """One extracted/changed record, already transformed into a Couchbase-ready
    JSON document by the connector."""

    key: str
    body: dict[str, Any]
    container: str
    # The record's identity in source-native terms (e.g. {"_id": ...} for MongoDB,
    # the key attributes for DynamoDB). Stored in the document's _migration
    # envelope so a post-cutover fallback can write the document back.
    source_key: dict[str, Any] | None = None
    # Field path -> source type for values JSON can't represent natively
    # (ObjectId, dates, sets, binary...), so writing back to the source is lossless.
    types: dict[str, str] | None = None


@dataclass
class ChangeEvent:
    """One CDC event yielded from stream_changes()."""

    container: str
    op: str  # "upsert" | "delete" | "heartbeat"
    document: SourceDocument | None = None  # set when op == "upsert"
    key: str | None = None                   # set when op == "delete"
    # Opaque, connector-defined resume token to persist (per container) into
    # MigrationRecord.checkpoint once this event has been durably written to
    # Couchbase -- see MigrationEngine._apply_change_batch().
    checkpoint: Any = None
    # When the change happened at the source, if the source reports it. Drives
    # the replication-lag figure in the UI.
    source_ts: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CdcCapabilities:
    method: CdcMethod | None
    resumable: bool          # restart resumes from a checkpoint, no re-scan needed
    captures_deletes: bool   # deletes arrive as events (else: reconciliation)
    reverse_sync: bool       # apply_reverse() is implemented


class SourceConnectorError(RuntimeError):
    pass


def heartbeat() -> ChangeEvent:
    return ChangeEvent(container="*", op="heartbeat")


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

    @property
    def capabilities(self) -> CdcCapabilities:
        return CdcCapabilities(method=None, resumable=False, captures_deletes=False, reverse_sync=False)

    def describe_capabilities(self, snapshot: SourceTopologySnapshot) -> SourceTopologySnapshot:
        caps = self.capabilities
        snapshot.cdc_method = caps.method
        snapshot.cdc_resumable = caps.resumable
        snapshot.cdc_captures_deletes = caps.captures_deletes
        snapshot.reverse_sync_supported = caps.reverse_sync
        return snapshot

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
        SourceDocument as they're read. Couchbase upserts are idempotent, so a
        full load is safe to restart from the beginning of a container."""

    async def capture_change_position(self, containers: list[str]) -> dict[str, Any]:
        """Return a checkpoint per container that points at "now" in the source's
        change log. Called before a bulk copy; stream_changes() later starts from
        it, so nothing written during the copy is missed. Connectors whose change
        capture isn't a durable log start buffering here instead."""
        return {}

    def stream_changes(
        self,
        containers: list[str],
        checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        """Yield ChangeEvents indefinitely (until the caller stops iterating),
        including a periodic heartbeat while idle. `checkpoint` is the migration's
        last-persisted per-container position (MigrationRecord.checkpoint)."""
        raise SourceConnectorError(
            f"{type(self).__name__} does not implement continuous change-data-capture."
        )

    def list_keys(self, container: str) -> AsyncIterator[str]:
        """Every Couchbase document key this container would produce, for delete
        reconciliation. Should read only key fields (projection), not documents."""
        raise SourceConnectorError(f"{type(self).__name__} does not support key listing.")

    async def apply_reverse(
        self,
        container: str,
        op: str,
        key: str,
        body: dict[str, Any] | None,
        source_key: dict[str, Any] | None,
        types: dict[str, str] | None = None,
    ) -> None:
        """Write one change made in Couchbase back to the source (post-cutover
        fallback). `op` is "upsert" or "delete"; `key` is the Couchbase document
        key; `body` is the document without its _migration envelope (None for a
        delete). `source_key`/`types` come from the envelope when the document was
        written by the migration, and are None for documents applications created
        after cutover -- connectors derive the identity from `key`/`body` then."""
        raise SourceConnectorError(f"{type(self).__name__} does not support writing back to the source.")

    async def close(self) -> None:
        """Release any pooled connections/clients. Safe to call multiple times."""
        return None
