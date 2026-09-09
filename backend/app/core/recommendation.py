"""
Replication-mode recommendation for the "Destination & Mode" wizard step.

The wizard asks one question -- do you plan to cut every application over to
Couchbase at once, or migrate them gradually (a phased cutover)? -- and combines
the answer with the already-introspected source topology (total estimated size,
container count, and whether the source supports continuous change-data-capture at
all) to recommend one of the three MigrationStrategy options.

DESIGN NOTE: deliberately a deterministic, rule-based recommender, not a live call
to the local Qwen LLM -- same rationale as the sibling couchbase-migration-agent
project's identically-named module: a wizard step is on the critical path of
setting up a migration and shouldn't be exposed to LLM latency or a hallucinated
recommendation.

The duration estimate is a rough planning figure only -- real throughput depends
heavily on the source database's own performance characteristics, network path,
and Couchbase cluster load, none of which can be known ahead of time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.models.enums import MigrationStrategy
from app.models.schemas import SourceTopologySnapshot

CutoverPlan = Literal["cutover", "phased"]

# Rough, deliberately conservative combined extract+load throughput assumption per
# worker, used only to turn a data size into a ballpark duration for planning
# purposes -- see the module docstring.
ASSUMED_MB_PER_SEC_PER_WORKER = 3.0
MAX_EFFECTIVE_WORKERS_FOR_ESTIMATE = 16

SHORT_WINDOW_SECONDS = 2 * 3600  # 2 hours

DOMINANT_CONTAINER_SIZE_RATIO = 0.6


@dataclass
class ReplicationModeRecommendation:
    recommended_strategy: MigrationStrategy
    headline: str
    rationale: str
    considerations: list[str] = field(default_factory=list)
    estimated_duration_seconds: float | None = None


def _effective_mbps(concurrency: int) -> float:
    workers = max(1, min(concurrency, MAX_EFFECTIVE_WORKERS_FOR_ESTIMATE))
    return ASSUMED_MB_PER_SEC_PER_WORKER * workers


def _estimate_full_load_duration_seconds(total_bytes: int, concurrency: int) -> float:
    mb = max(total_bytes, 0) / (1024 * 1024)
    mbps = _effective_mbps(concurrency)
    if mbps <= 0:
        return 0.0
    return mb / mbps


def _format_hours(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1:
        return f"~{max(1, round(seconds / 60))} minutes"
    if hours < 48:
        return f"~{hours:.1f} hours"
    return f"~{hours / 24:.1f} days"


def recommend_replication_mode(
    cutover_plan: CutoverPlan,
    topology: SourceTopologySnapshot,
    concurrency: int,
) -> ReplicationModeRecommendation:
    total_bytes = topology.total_estimated_size_bytes or 0
    container_count = len(topology.containers)
    estimated_seconds = _estimate_full_load_duration_seconds(total_bytes, concurrency)

    considerations: list[str] = []
    if not topology.supports_cdc:
        considerations.append(
            f"{topology.cdc_notes or 'This source does not currently support continuous change-data-capture.'} "
            "Continuous replication and bulk copy + continuous sync aren't available until that's resolved; "
            "one-time migration is the only option in the meantime."
        )
    if container_count and total_bytes and topology.containers:
        largest = max(topology.containers, key=lambda c: c.estimated_size_bytes or 0)
        largest_bytes = largest.estimated_size_bytes or 0
        if total_bytes and largest_bytes / total_bytes >= DOMINANT_CONTAINER_SIZE_RATIO:
            considerations.append(
                f"Container \"{largest.name}\" accounts for {largest_bytes / total_bytes:.0%} of the "
                "source's total estimated size -- it will dominate the transfer time."
            )
    if container_count > 15:
        considerations.append(
            f"{container_count} containers detected. Concurrency is shared across all of them, so a "
            "higher worker count matters more here than for a single-container migration."
        )

    can_use_cdc = topology.supports_cdc

    if cutover_plan == "phased":
        if can_use_cdc:
            strategy = MigrationStrategy.FULL_LOAD_AND_CDC
            headline = "Bulk copy + continuous sync (phased cutover)"
            rationale = (
                "You're planning to move applications over gradually rather than all at once, which "
                "means the source and Couchbase both need to stay in sync for a while -- a one-time "
                "load can't do that, since the moment the first application starts writing to Couchbase, "
                "a plain full load would silently miss new source writes. Bulk copy + continuous sync "
                "moves the existing data over quickly with a full load, then change-data-capture keeps "
                "both stores converged so each application can cut over on its own schedule."
            )
            if total_bytes and total_bytes < 50 * 1024 * 1024:
                considerations.append(
                    "The source currently has very little data, so the bulk-copy step will finish almost "
                    "immediately -- continuous replication reaches the same end state with one fewer "
                    "moving part if you'd rather skip it."
                )
        else:
            strategy = MigrationStrategy.FULL_LOAD
            headline = "One-time migration (CDC unavailable)"
            rationale = (
                "A phased cutover normally calls for continuous sync, but this source doesn't currently "
                "support change-data-capture (see the note above). One-time migration is the only option "
                "until that's resolved -- plan the phased cutover around a single migration window instead."
            )
    else:  # "cutover"
        if not can_use_cdc or estimated_seconds < SHORT_WINDOW_SECONDS:
            strategy = MigrationStrategy.FULL_LOAD
            headline = "One-time migration (single maintenance window)"
            rationale = (
                "You're planning to cut every application over at the same time, and the estimated "
                f"transfer time ({_format_hours(estimated_seconds)}) comfortably fits a single maintenance "
                "window. A one-time full load is the simplest option here -- no ongoing replication to "
                "configure, monitor, or tear down afterward."
            ) if estimated_seconds < SHORT_WINDOW_SECONDS else (
                "You're planning to cut every application over at once, and this source doesn't support "
                "continuous sync, so a one-time full load is the only option regardless of transfer time."
            )
        else:
            strategy = MigrationStrategy.FULL_LOAD_AND_CDC
            headline = "Bulk copy + continuous sync (shrink the cutover window)"
            rationale = (
                "You're planning to cut every application over at the same time, but the estimated "
                f"one-time transfer ({_format_hours(estimated_seconds)}) would mean a long outage window "
                "if done as a single full load. Bulk copy + continuous sync moves the bulk of the data "
                "ahead of time, then change-data-capture keeps Couchbase current in the background -- the "
                "actual cutover only has to cover whatever changed since the bulk copy finished."
            )
            considerations.append(
                "This still results in every application cutting over at the same moment -- only the "
                "data transfer is staged, not the application switchover itself."
            )

    return ReplicationModeRecommendation(
        recommended_strategy=strategy,
        headline=headline,
        rationale=rationale,
        considerations=considerations,
        estimated_duration_seconds=estimated_seconds if estimated_seconds > 0 else None,
    )
