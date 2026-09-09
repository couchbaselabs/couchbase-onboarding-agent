"""Shared helpers used by every connector to turn source-native values into
JSON-safe Python primitives Couchbase can store, and to build deterministic
Couchbase document keys."""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import decimal
import queue
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

# Couchbase Server's hard per-document size limit is 20 MiB. Connectors flag
# documents that land close to it (see validator.py's DOCUMENT_SIZE_LIMIT check)
# rather than silently truncating -- truncation would corrupt the migrated record.
COUCHBASE_MAX_DOC_SIZE_BYTES = 20 * 1024 * 1024


def json_safe(value: Any) -> Any:
    """Recursively convert a value from a source SDK's native types (BSON, boto3's
    Decimal/Binary, Cassandra's uuid/date/blob, etc.) into something json.dumps /
    Couchbase's transcoder can serialize directly, preserving as much fidelity as
    practical:
      - datetimes/dates -> ISO 8601 strings
      - UUID -> string
      - Decimal -> float (Couchbase/N1QL has no arbitrary-precision decimal type;
        see the per-connector docstring for any source-specific caveat)
      - bytes -> base64 string, tagged so it's recognizable as binary on read
      - sets -> lists (JSON has no set type)
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return {"$binary": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [json_safe(v) for v in value]
    # Fallback: best-effort string representation rather than letting an unknown
    # type blow up the whole batch.
    return str(value)


def make_key(*parts: str, max_len: int = 250) -> str:
    """Build a deterministic Couchbase document key from one or more identifier
    parts (container name, primary key value(s)). Couchbase keys are limited to 250
    bytes; a long natural key is truncated with a short hash suffix so collisions
    stay astronomically unlikely rather than silently overwriting a different doc."""
    key = "::".join(str(p) for p in parts)
    if len(key.encode("utf-8")) <= max_len:
        return key
    import hashlib

    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    truncated = key.encode("utf-8")[: max_len - 13].decode("utf-8", errors="ignore")
    return f"{truncated}~{digest}"


class _Idle:
    """Sentinel yielded by bridge_blocking_batches() every `poll_interval` seconds
    when the producer thread hasn't put anything new -- lets a long-lived consumer
    (continuous CDC) periodically regain control to check a stop condition without
    needing every source SDK's blocking iterator to support its own timeout
    parameter. Ignored by extract()'s consumer (it just loops back and keeps
    waiting for the next real batch)."""

    def __repr__(self) -> str:  # pragma: no cover
        return "IDLE"


IDLE = _Idle()


async def bridge_blocking_batches(
    produce: Callable[[queue.Queue], None], poll_interval: float = 2.0,
) -> AsyncIterator[Any]:
    """Runs `produce(q)` in a background thread; `produce` should call q.put(batch)
    for each batch of items, q.put(<the exception instance>) on error, and
    q.put(None) as a sentinel when finished. Yields each batch to the (async)
    caller as it becomes available, or IDLE (see above) after `poll_interval`
    seconds of producer inactivity.

    This bridges a blocking source SDK's cursor/iterator (pymongo, boto3, and
    cassandra-driver are all synchronous) into a connector's async extract()/
    stream_changes() methods without requiring every source SDK to have an
    asyncio-native client. Backpressure comes for free from the queue's maxsize:
    the producer thread blocks on q.put() once the consumer falls behind.

    NOTE on stopping a long-lived producer (e.g. a change-stream cursor blocked
    waiting for the next source event): this helper does not forcibly interrupt the
    background thread when the caller stops iterating -- Python cannot safely kill a
    thread blocked inside a C-extension SDK call. For extract() this is moot (the
    producer finishes on its own). For stream_changes(), the caller is expected to
    stop *consuming* (via the IDLE-driven stop check) rather than expect the
    producer thread to exit immediately; the orphaned daemon thread exits once it
    next produces an item into an unread, now-full queue, or when the process exits.
    This is a documented, accepted limitation for this project's stated scope (see
    README.md's connector-depth notes) rather than an oversight."""
    q: queue.Queue = queue.Queue(maxsize=4)
    thread = threading.Thread(target=produce, args=(q,), daemon=True)
    thread.start()
    try:
        while True:
            try:
                item = await asyncio.to_thread(q.get, True, poll_interval)
            except queue.Empty:
                yield IDLE
                continue
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        thread.join(timeout=0.1)


def approx_size_bytes(value: Any) -> int:
    """Cheap, approximate size estimate for a JSON-safe structure -- used for
    per-document size warnings and throughput byte-counting, not billing-accurate."""
    import json

    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0
