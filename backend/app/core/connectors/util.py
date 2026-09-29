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


# ---------------------------------------------------------------------------
# Type hints for lossless write-back (post-cutover fallback)
# ---------------------------------------------------------------------------
#
# Couchbase stores JSON, so extraction turns source-native types (ObjectId,
# dates, sets, binary) into strings/lists. Writing a document back to the source
# needs to undo that, so connectors record the original type of each such value
# under a JSON-pointer path (RFC 6901) in SourceDocument.types, and the reverse
# writer converts those paths back. Values that were native JSON need no hint.

SpecialHook = Callable[[Any], "tuple[Any, str] | None"]


def _ptr_escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _ptr_unescape(part: str) -> str:
    return part.replace("~1", "/").replace("~0", "~")


def json_safe_typed(value: Any, hook: SpecialHook, types: dict[str, str], path: str = "") -> Any:
    """Like json_safe(), but lets `hook` convert source-specific values first
    and records the returned type name at the value's JSON-pointer path."""
    special = hook(value)
    if special is not None:
        converted, type_name = special
        types[path or "/"] = type_name
        return converted
    if isinstance(value, dict):
        return {str(k): json_safe_typed(v, hook, types, f"{path}/{_ptr_escape(str(k))}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_typed(v, hook, types, f"{path}/{i}") for i, v in enumerate(value)]
    if isinstance(value, (set, frozenset)):
        return [json_safe_typed(v, hook, types, f"{path}/{i}") for i, v in enumerate(value)]
    return json_safe(value)


def apply_type_hints(body: dict[str, Any], types: dict[str, str] | None, convert: Callable[[Any, str], Any]) -> dict[str, Any]:
    """Convert each hinted path in `body` back with convert(value, type_name).
    Paths that no longer exist (the application changed the document shape after
    cutover) are skipped rather than failing the write."""
    if not types:
        return body
    for path, type_name in types.items():
        if path == "/":
            continue
        parts = [_ptr_unescape(p) for p in path.split("/")[1:]]
        parent: Any = body
        ok = True
        for part in parts[:-1]:
            if isinstance(parent, dict) and part in parent:
                parent = parent[part]
            elif isinstance(parent, list) and part.isdigit() and int(part) < len(parent):
                parent = parent[int(part)]
            else:
                ok = False
                break
        if not ok:
            continue
        last = parts[-1]
        if isinstance(parent, dict) and last in parent:
            try:
                parent[last] = convert(parent[last], type_name)
            except Exception:  # noqa: BLE001 -- leave the JSON value as-is
                pass
        elif isinstance(parent, list) and last.isdigit() and int(last) < len(parent):
            try:
                parent[int(last)] = convert(parent[int(last)], type_name)
            except Exception:  # noqa: BLE001
                pass
    return body


def decode_binary(value: Any) -> Any:
    """Undo json_safe()'s {"$binary": <base64>} wrapper."""
    if isinstance(value, dict) and set(value.keys()) == {"$binary"}:
        return base64.b64decode(value["$binary"])
    return value


def parse_iso_datetime(value: Any) -> Any:
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(text)
    return value


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def strip_migration_envelope(doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a Couchbase document into (body, _migration envelope)."""
    body = dict(doc)
    envelope = body.pop("_migration", None) or {}
    return body, envelope


class SeenRecently:
    """Bounded memory of recently applied (key, version) pairs, so a poller that
    deliberately re-reads an overlap window doesn't apply the same write twice."""

    def __init__(self, limit: int = 200_000):
        from collections import OrderedDict

        self._items: "OrderedDict[tuple[str, int], None]" = OrderedDict()
        self._limit = limit

    def add(self, item: tuple[str, int]) -> bool:
        if item in self._items:
            return False
        self._items[item] = None
        if len(self._items) > self._limit:
            self._items.popitem(last=False)
        return True
