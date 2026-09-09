"""
Pre-migration validation: source connectivity/edition, destination (Couchbase)
connectivity/capacity, CDC availability for the chosen strategy, container-name
sanitization collisions, a lightweight document-size sanity check, network
latency, and TLS configuration.

Every check produces a ValidationCheckResult; the report as a whole gates the
"Approve migration" control in the UI, same contract as the sibling
couchbase-migration-agent project's validator.
"""
from __future__ import annotations

import logging
import time
from uuid import UUID

import asyncio

from app.core.capella_client import CapellaClient
from app.core.connectors.base import SourceConnectorError
from app.core.connectors.couchbase_source import CouchbaseSourceConnector
from app.core.connectors.registry import get_connector
from app.core.connectors.util import COUCHBASE_MAX_DOC_SIZE_BYTES
from app.core.couchbase_client import CouchbaseClientError, CouchbaseClusterClient, sanitize_couchbase_name
from app.models.enums import (
    COUCHBASE_SOURCE_TYPES,
    CONTINUOUS_STRATEGIES,
    MigrationStrategy,
    ValidationCheckId,
    ValidationSeverity,
)
from app.models.schemas import (
    ContainerMigrationSpec,
    CouchbaseConnectionConfig,
    CouchbaseTopologySnapshot,
    SourceConnectionConfig,
    SourceTopologySnapshot,
    ValidationCheckResult,
    ValidationReport,
)

logger = logging.getLogger(__name__)


class MigrationValidator:
    def __init__(
        self,
        migration_id: UUID,
        source: SourceConnectionConfig,
        destination: CouchbaseConnectionConfig,
        strategy: MigrationStrategy,
        containers: list[ContainerMigrationSpec],
        destination_bucket: str | None = None,
    ):
        self.migration_id = migration_id
        self.source_config = source
        self.dest_config = destination
        self.strategy = strategy
        self.containers = containers
        self.destination_bucket = destination_bucket
        self.checks: list[ValidationCheckResult] = []

    def _add(self, check_id: ValidationCheckId, label: str, severity: ValidationSeverity,
              passed: bool, message: str, details: dict | None = None) -> None:
        self.checks.append(ValidationCheckResult(
            check_id=check_id, label=label, severity=severity,
            passed=passed, message=message, details=details or {},
        ))

    async def run(self) -> ValidationReport:
        source_topology = await self._check_source_connectivity()
        dest_topology = self._check_destination(source_topology)

        if source_topology:
            self._check_cdc_support(source_topology)
            self._check_naming_compat(source_topology)
            self._check_document_size(source_topology)
        if source_topology and dest_topology:
            self._check_capacity(source_topology, dest_topology)

        await self._check_xdcr_vbucket_compat()
        self._check_tls()

        return ValidationReport(
            migration_id=self.migration_id, checks=self.checks,
            source_topology=source_topology, dest_topology=dest_topology,
        )

    # -- individual checks --------------------------------------------------

    async def _check_source_connectivity(self) -> SourceTopologySnapshot | None:
        start = time.monotonic()
        connector = get_connector(self.source_config)
        try:
            topo = await connector.test_connection()
        except (SourceConnectorError, Exception) as exc:  # noqa: BLE001
            self._add(
                ValidationCheckId.SOURCE_CONNECTIVITY, "Source database connectivity",
                ValidationSeverity.ERROR, False, f"Could not connect to source: {exc}",
            )
            return None
        finally:
            await connector.close()
        latency_ms = (time.monotonic() - start) * 1000

        self._add(
            ValidationCheckId.SOURCE_CONNECTIVITY, "Source database connectivity",
            ValidationSeverity.ERROR, True,
            f"Connected successfully and enumerated {len(topo.containers)} container(s).",
        )
        self._add(
            ValidationCheckId.SOURCE_EDITION, "Source database version/edition",
            ValidationSeverity.INFO, True,
            topo.server_edition or topo.server_version or "Detected, version unknown.",
            details={"version": topo.server_version},
        )
        self._add(
            ValidationCheckId.NETWORK_LATENCY, "Network latency to source",
            ValidationSeverity.WARNING, latency_ms < 5000,
            f"Round-trip introspection of the source took {latency_ms:.0f} ms.",
            details={"latency_ms": latency_ms},
        )
        return topo

    def _check_destination(self, source_topology: SourceTopologySnapshot | None) -> CouchbaseTopologySnapshot | None:
        if self.dest_config.is_capella:
            capella = CapellaClient()
            try:
                ok, info = capella.verify_cluster_reachable(self.dest_config)
            except Exception as exc:  # noqa: BLE001
                self._add(
                    ValidationCheckId.DEST_CONNECTIVITY, "Destination (Capella) connectivity",
                    ValidationSeverity.ERROR, False, f"Capella API check failed: {exc}",
                )
                return None
            self._add(
                ValidationCheckId.DEST_CONNECTIVITY, "Destination (Capella) connectivity",
                ValidationSeverity.ERROR, ok,
                info.get("message", "Capella cluster reachable." if ok else "Capella cluster unreachable."),
            )
            if not ok:
                return None

        client = CouchbaseClusterClient(self.dest_config)
        try:
            topo = client.snapshot_topology()
        except (CouchbaseClientError, Exception) as exc:  # noqa: BLE001
            self._add(
                ValidationCheckId.DEST_CONNECTIVITY, "Destination cluster connectivity",
                ValidationSeverity.ERROR, False, f"Could not connect to destination: {exc}",
            )
            return None
        finally:
            client.close()

        if not self.dest_config.is_capella:
            self._add(
                ValidationCheckId.DEST_CONNECTIVITY, "Destination cluster connectivity",
                ValidationSeverity.ERROR, True, "Connected successfully.",
            )
        return topo

    def _check_capacity(self, source: SourceTopologySnapshot, dest: CouchbaseTopologySnapshot) -> None:
        needed = source.total_estimated_size_bytes or 0
        self._add(
            ValidationCheckId.DEST_CAPACITY, "Destination storage capacity",
            ValidationSeverity.WARNING, True,
            f"Source data size ~{needed / (1024**3):.2f} GiB (estimate). Confirm the destination "
            "bucket's RAM quota / cluster storage before approving.",
            details={"source_bytes": needed},
        )

    def _check_cdc_support(self, source: SourceTopologySnapshot) -> None:
        if self.strategy not in CONTINUOUS_STRATEGIES:
            self._add(
                ValidationCheckId.CDC_SUPPORT, "Change-data-capture availability",
                ValidationSeverity.INFO, True,
                "Not required for a one-time migration.",
            )
            return
        if source.supports_cdc:
            self._add(
                ValidationCheckId.CDC_SUPPORT, "Change-data-capture availability",
                ValidationSeverity.ERROR, True,
                "Source supports continuous change-data-capture for the selected replication mode.",
            )
        else:
            self._add(
                ValidationCheckId.CDC_SUPPORT, "Change-data-capture availability",
                ValidationSeverity.ERROR, False,
                source.cdc_notes or "This source does not currently support continuous change-data-capture, "
                "which the selected replication mode requires.",
            )

    async def _check_xdcr_vbucket_compat(self) -> None:
        """Continuous/hybrid replication from a Couchbase source uses XDCR
        (see core/couchbase_native.py), which refuses to replicate between
        buckets with different vBucket counts -- and a bucket's vBucket count
        can never be changed after it's created. Confirmed against a live
        cluster on 2026-07-30: a full cbbackupmgr backup+restore ran to
        completion before XDCR setup failed with "The number of vbuckets in
        source cluster, 1024, and target cluster, 128, does not match. This
        configuration is not supported." -- Couchbase Server 8.0 defaults new
        Magma buckets to 128 vBuckets, which doesn't match the traditional
        1024-vBucket layout every self-managed source this app supports
        (7.2-8.0.2) uses. Catching this here, before approval, avoids wasting
        that same backup+restore time on a migration that was always going to
        fail at the XDCR step."""
        if self.strategy not in CONTINUOUS_STRATEGIES or self.source_config.source_type not in COUCHBASE_SOURCE_TYPES:
            return  # XDCR isn't involved at all otherwise

        source_connector = CouchbaseSourceConnector(self.source_config)
        try:
            source_vbuckets = await source_connector.get_vbucket_count()
        except Exception as exc:  # noqa: BLE001
            source_vbuckets = None
            logger.warning("Could not determine source vBucket count: %s", exc)
        finally:
            await source_connector.close()

        if source_vbuckets is None:
            self._add(
                ValidationCheckId.XDCR_VBUCKET_COMPAT, "XDCR vBucket compatibility",
                ValidationSeverity.WARNING, True,
                "Could not determine the source bucket's vBucket count to compare against the destination -- "
                "this will only surface as a hard failure at XDCR setup time if the two don't match.",
            )
            return

        if not self.destination_bucket:
            self._add(
                ValidationCheckId.XDCR_VBUCKET_COMPAT, "XDCR vBucket compatibility",
                ValidationSeverity.INFO, True,
                f"Source bucket uses {source_vbuckets} vBuckets. No destination bucket name given yet to compare "
                "against.",
            )
            return

        dest_client = CouchbaseClusterClient(self.dest_config)
        try:
            dest_vbuckets = await asyncio.to_thread(dest_client.get_vbucket_count, self.destination_bucket)
        except Exception as exc:  # noqa: BLE001
            dest_vbuckets = None
            logger.warning("Could not determine destination vBucket count: %s", exc)
        finally:
            dest_client.close()

        if dest_vbuckets is None:
            self._add(
                ValidationCheckId.XDCR_VBUCKET_COMPAT, "XDCR vBucket compatibility",
                ValidationSeverity.INFO, True,
                f"Destination bucket '{self.destination_bucket}' doesn't exist yet -- it will be "
                f"auto-provisioned with {source_vbuckets} vBuckets to match the source when this migration "
                "is approved.",
            )
            return

        passed = dest_vbuckets == source_vbuckets
        self._add(
            ValidationCheckId.XDCR_VBUCKET_COMPAT, "XDCR vBucket compatibility",
            ValidationSeverity.ERROR, passed,
            f"Source and destination buckets both use {source_vbuckets} vBuckets." if passed else
            f"Source bucket uses {source_vbuckets} vBuckets, but destination bucket '{self.destination_bucket}' "
            f"already exists with {dest_vbuckets} -- XDCR requires both to match, and a bucket's vBucket count "
            "can never be changed after creation. Drop and recreate the destination bucket requesting "
            f"{source_vbuckets} vBuckets before approving this migration (Couchbase Server 8.0+ defaults new "
            "Magma buckets to 128, which won't match a traditional 1024-vBucket source).",
            details={"source_vbuckets": source_vbuckets, "dest_vbuckets": dest_vbuckets},
        )

    def _check_naming_compat(self, source: SourceTopologySnapshot) -> None:
        by_target: dict[tuple[str, str], list[str]] = {}
        spec_by_name = {s.container_name: s for s in self.containers}
        for c in source.containers:
            spec = spec_by_name.get(c.name)
            if spec is not None and not spec.include:
                continue
            scope = sanitize_couchbase_name((spec.target_scope_name if spec else None) or "_default") \
                if (spec and spec.target_scope_name) else "_default"
            collection = sanitize_couchbase_name((spec.target_collection_name if spec else None) or c.name)
            by_target.setdefault((scope, collection), []).append(c.name)

        collisions = {k: v for k, v in by_target.items() if len(v) > 1}
        passed = not collisions
        self._add(
            ValidationCheckId.NAMING_COMPAT, "Container name -> Couchbase collection mapping",
            ValidationSeverity.WARNING, passed,
            "Every included container maps to a distinct scope.collection." if passed else
            f"Multiple source containers sanitize to the same destination collection: {collisions}. "
            "Set an explicit target collection name for these containers to avoid their documents "
            "landing in the same collection.",
            details={"collisions": {f"{s}.{c}": names for (s, c), names in collisions.items()}},
        )

    def _check_document_size(self, source: SourceTopologySnapshot) -> None:
        # Cheap, best-effort: only flags a container whose AVERAGE document size
        # (estimated_size_bytes / estimated_count) is already large -- a true
        # per-document check happens during extraction itself (couchbase_loader.py
        # surfaces any individual oversized document as a failed-doc error rather
        # than corrupting it via truncation).
        flagged = []
        for c in source.containers:
            if c.estimated_count and c.estimated_size_bytes:
                avg = c.estimated_size_bytes / max(c.estimated_count, 1)
                if avg > COUCHBASE_MAX_DOC_SIZE_BYTES * 0.5:
                    flagged.append(c.name)
        passed = not flagged
        self._add(
            ValidationCheckId.DOCUMENT_SIZE_LIMIT, "Document size vs. Couchbase's 20 MiB limit",
            ValidationSeverity.WARNING, passed,
            "No container's average document size is close to Couchbase's per-document limit." if passed
            else f"Average document size in {flagged} is already over half of Couchbase's 20 MiB "
            "per-document limit -- individual documents may fail to migrate.",
            details={"flagged_containers": flagged},
        )

    def _check_tls(self) -> None:
        both_tls = self.source_config.use_tls and (self.dest_config.use_tls or self.dest_config.is_capella)
        self._add(
            ValidationCheckId.TLS_CONFIG, "TLS configuration",
            ValidationSeverity.WARNING if not both_tls else ValidationSeverity.INFO,
            both_tls,
            "TLS enabled on both source and destination." if both_tls else
            "TLS is not enabled on both ends; Capella requires TLS in transit, and most managed "
            "source databases do too.",
        )
