"""
Delete reconciliation for continuous sync.

Some change-capture mechanisms never report deletes (Cassandra WRITETIME
polling, the Cosmos DB change feed in Latest Version mode, Couchbase CAS
polling), and any stream can miss events during an outage. Reconciliation
closes that gap: it lists every key at the source (keys only), lists what this
migration wrote to the destination before the run started, and deletes
destination documents whose key no longer exists at the source.

Key sets go through a temporary SQLite file, so memory stays flat for
collections with hundreds of millions of documents.

Safety: documents written after the run started are never considered (the
change stream may have just created them), each delete is CAS-guarded and
re-checks that the document is still stale, an empty source listing never
deletes anything, and a run that would delete more than
RECONCILE_MAX_DELETE_FRACTION of a collection stops for operator confirmation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime

from app.config import get_settings
from app.core.connectors.base import SourceConnector
from app.core.couchbase_loader import CouchbaseLoader

logger = logging.getLogger(__name__)


@dataclass
class ReconcileOutcome:
    deleted: int = 0
    missing_in_destination: int = 0
    journaled: int = 0
    held_for_confirmation: dict[str, int] = field(default_factory=dict)  # container -> would-delete count
    notes: list[str] = field(default_factory=list)


class _KeySet:
    def __init__(self) -> None:
        directory = os.path.join(get_settings().scratch_dir, "reconcile")
        os.makedirs(directory, exist_ok=True)
        fd, self.path = tempfile.mkstemp(suffix=".sqlite", dir=directory)
        os.close(fd)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("CREATE TABLE src (k TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE dst (k TEXT PRIMARY KEY)")

    def add(self, table: str, keys: list[str]) -> None:
        self.db.executemany(f"INSERT OR IGNORE INTO {table}(k) VALUES (?)", [(k,) for k in keys])

    def absent_from_source(self, keys: list[str]) -> list[str]:
        out = []
        for k in keys:
            if self.db.execute("SELECT 1 FROM src WHERE k = ?", (k,)).fetchone() is None:
                out.append(k)
        return out

    def count(self, table: str) -> int:
        return int(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def missing_in_destination(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM src WHERE k NOT IN (SELECT k FROM dst)").fetchone()[0])

    def close(self) -> None:
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


async def reconcile(
    connector: SourceConnector,
    loader: CouchbaseLoader,
    containers: list[str],
    *,
    force: bool = False,
) -> ReconcileOutcome:
    started_at = datetime.utcnow()
    max_fraction = get_settings().reconcile_max_delete_fraction
    outcome = ReconcileOutcome()

    for container in containers:
        keys = _KeySet()
        try:
            chunk: list[str] = []
            async for key in connector.list_keys(container):
                chunk.append(key)
                if len(chunk) >= 5000:
                    await asyncio.to_thread(keys.add, "src", chunk)
                    chunk = []
            if chunk:
                await asyncio.to_thread(keys.add, "src", chunk)

            stale: list[str] = []
            async for dest_chunk in loader.destination_keys(container, started_at):
                await asyncio.to_thread(keys.add, "dst", dest_chunk)
                stale.extend(await asyncio.to_thread(keys.absent_from_source, dest_chunk))

            src_count = await asyncio.to_thread(keys.count, "src")
            dst_count = await asyncio.to_thread(keys.count, "dst")
            outcome.missing_in_destination += await asyncio.to_thread(keys.missing_in_destination)

            if not stale:
                continue
            if src_count == 0:
                outcome.notes.append(
                    f"{container}: the source listed no keys, so no deletes were applied (safety check)."
                )
                continue
            if not force and dst_count and len(stale) > max(100, int(dst_count * max_fraction)):
                outcome.held_for_confirmation[container] = len(stale)
                outcome.notes.append(
                    f"{container}: would delete {len(stale):,} of {dst_count:,} documents -- held for "
                    "confirmation. Run reconciliation again with force if that's expected."
                )
                continue
            for i in range(0, len(stale), 500):
                # CAS-guarded: skips any document the change stream rewrote
                # after this run started listing keys.
                result = await loader.delete_if_stale(container, stale[i:i + 500], started_at)
                outcome.deleted += result.docs_written
                outcome.journaled += result.journaled
        finally:
            await asyncio.to_thread(keys.close)
    return outcome
