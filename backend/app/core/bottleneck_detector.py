"""
Best-effort, read-only detection of common ETL pipeline bottlenecks while a
migration is actively running.

Unlike the sibling couchbase-migration-agent project (which watches a cbbackupmgr
subprocess it launches and can only retune by killing/relaunching it), this app's
extract/load pipeline is a pool of asyncio worker tasks it owns directly end to
end -- see couchbase_loader.py's asyncio.Semaphore-bounded concurrency. That means
concurrency can be adjusted *live*, without restarting anything, for every
bottleneck kind below (not just resource-pressure ones).

What's checked, using data the pipeline already has:
  - Stalled throughput: docs/sec has been ~0 for a sustained window -- usually a
    dropped source or destination connection rather than a merely slow transfer.
  - Degraded throughput: rate has dropped well below this run's own peak for a
    sustained window -- typically network or resource contention on either side.
  - Source throttling: the connector reported a rate-limit/throttling error from
    the source (e.g. DynamoDB's ProvisionedThroughputExceededException, MongoDB
    Atlas connection-storm errors) -- a strong signal to reduce concurrency.
  - Destination backpressure: Couchbase upserts are failing at an elevated rate
    (temp failures, durability timeouts) -- also addressed by reducing concurrency.

Source-throttling and destination-backpressure findings are auto-remediated by
MigrationEngine's pipeline loop (reduce concurrency, no restart needed). Stalled/
degraded throughput stays diagnosis + suggestion only, same rationale as the
sibling project: a concurrency change doesn't fix a dead connection or a genuine
network problem.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from app.models.enums import BottleneckKind

STALL_THRESHOLD_DOCS_PER_SEC = 0.1
STALL_WINDOW_S = 45.0

DEGRADED_RATIO = 0.4
DEGRADED_MIN_PEAK_DOCS_PER_SEC = 5.0
DEGRADED_WINDOW_S = 60.0

# A batch's failure rate above this, sustained across several consecutive batches,
# is treated as destination backpressure rather than isolated bad-document errors.
DEST_ERROR_RATE_THRESHOLD = 0.10
DEST_ERROR_MIN_SAMPLES = 3

FINDING_COOLDOWN_S = 90.0

MIN_AUTO_THROTTLE_CONCURRENCY = 1
MAX_AUTO_THROTTLE_ATTEMPTS = 4


@dataclass
class _RawFinding:
    kind: BottleneckKind
    message: str
    suggestion: str
    recommended_concurrency: int | None = None


@dataclass
class BottleneckMonitor:
    """Per-run rolling state. Feed throughput samples via observe_throughput() and
    batch outcomes via observe_batch_result(); call poll() every tick to get any new
    findings."""

    _samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=200))
    _peak_docs_per_sec: float = 0.0
    _stall_started_at: float | None = None
    _degraded_started_at: float | None = None
    _recent_batches: deque[tuple[int, int]] = field(default_factory=lambda: deque(maxlen=10))  # (written, failed)
    _last_finding_at: dict[BottleneckKind, float] = field(default_factory=dict)
    current_concurrency: int = 8

    def observe_throughput(self, docs_per_sec: float, elapsed_s: float) -> None:
        now = time.monotonic()
        self._samples.append((now, docs_per_sec))
        if docs_per_sec > self._peak_docs_per_sec:
            self._peak_docs_per_sec = docs_per_sec

        if docs_per_sec < STALL_THRESHOLD_DOCS_PER_SEC and elapsed_s > 5:
            if self._stall_started_at is None:
                self._stall_started_at = now
        else:
            self._stall_started_at = None

        if (
            self._peak_docs_per_sec >= DEGRADED_MIN_PEAK_DOCS_PER_SEC
            and docs_per_sec < self._peak_docs_per_sec * DEGRADED_RATIO
        ):
            if self._degraded_started_at is None:
                self._degraded_started_at = now
        else:
            self._degraded_started_at = None

    def observe_batch_result(self, docs_written: int, docs_failed: int) -> None:
        self._recent_batches.append((docs_written, docs_failed))

    def _eligible(self, kind: BottleneckKind) -> bool:
        last = self._last_finding_at.get(kind)
        return last is None or (time.monotonic() - last) >= FINDING_COOLDOWN_S

    def _mark(self, kind: BottleneckKind) -> None:
        self._last_finding_at[kind] = time.monotonic()

    def poll(self) -> list[_RawFinding]:
        out: list[_RawFinding] = []
        now = time.monotonic()

        if (
            self._stall_started_at is not None
            and (now - self._stall_started_at) >= STALL_WINDOW_S
            and self._eligible(BottleneckKind.THROUGHPUT_STALLED)
        ):
            stalled_for = int(now - self._stall_started_at)
            out.append(_RawFinding(
                kind=BottleneckKind.THROUGHPUT_STALLED,
                message=f"Migration throughput has been essentially flat for over {stalled_for}s.",
                suggestion=(
                    "This usually isn't a concurrency setting -- check that the connection to "
                    "both the source and Couchbase is still healthy (network blip, VPN drop, or "
                    "the source itself stalling) before changing anything."
                ),
            ))
            self._mark(BottleneckKind.THROUGHPUT_STALLED)

        if (
            self._degraded_started_at is not None
            and (now - self._degraded_started_at) >= DEGRADED_WINDOW_S
            and self._eligible(BottleneckKind.THROUGHPUT_DEGRADED)
        ):
            degraded_for = int(now - self._degraded_started_at)
            out.append(_RawFinding(
                kind=BottleneckKind.THROUGHPUT_DEGRADED,
                message=(
                    f"Throughput has dropped well below this run's own peak "
                    f"({self._peak_docs_per_sec:.0f} docs/s) for over {degraded_for}s."
                ),
                suggestion=(
                    "A sustained drop like this usually points to resource contention on the "
                    "source or destination, or an under-provisioned network path -- check both "
                    "before assuming a concurrency change will help."
                ),
            ))
            self._mark(BottleneckKind.THROUGHPUT_DEGRADED)

        if len(self._recent_batches) >= DEST_ERROR_MIN_SAMPLES and self._eligible(BottleneckKind.DEST_BACKPRESSURE):
            written = sum(w for w, _f in self._recent_batches)
            failed = sum(f for _w, f in self._recent_batches)
            total = written + failed
            error_rate = (failed / total) if total else 0.0
            if error_rate >= DEST_ERROR_RATE_THRESHOLD:
                recommended = max(MIN_AUTO_THROTTLE_CONCURRENCY, self.current_concurrency // 2)
                out.append(_RawFinding(
                    kind=BottleneckKind.DEST_BACKPRESSURE,
                    message=(
                        f"{error_rate:.0%} of recent writes to Couchbase have failed "
                        f"(temp failures / durability timeouts are the common cause) -- the "
                        f"destination looks like it's under write pressure at the current "
                        f"concurrency ({self.current_concurrency})."
                    ),
                    suggestion=f"Reduce concurrency to {recommended} and retry the failed documents.",
                    recommended_concurrency=recommended,
                ))
                self._mark(BottleneckKind.DEST_BACKPRESSURE)

        return out

    def note_source_throttled(self, detail: str) -> _RawFinding:
        recommended = max(MIN_AUTO_THROTTLE_CONCURRENCY, self.current_concurrency // 2)
        finding = _RawFinding(
            kind=BottleneckKind.SOURCE_THROTTLED,
            message=f"The source database reported a rate-limit/throttling error: {detail}",
            suggestion=f"Reduce concurrency to {recommended} and retry.",
            recommended_concurrency=recommended,
        )
        self._mark(BottleneckKind.SOURCE_THROTTLED)
        return finding
