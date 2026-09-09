"""
Client for the Couchbase Capella public Management API (v4).
Docs: https://docs.couchbase.com/cloud/management-api-guide/management-api-start.html

Used to verify a target Capella cluster/project is reachable and to create the
destination bucket ahead of migration.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from app.config import get_settings
from app.models.schemas import CouchbaseConnectionConfig

logger = logging.getLogger(__name__)


class CapellaAPIError(RuntimeError):
    pass


class CapellaClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = self.settings.capella_api_base_url.rstrip("/")
        self.token = self.settings.capella_api_token

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise CapellaAPIError(
                "CAPELLA_API_TOKEN is not configured. Generate a v4 API token in "
                "Capella > Settings > API Keys and set it in the environment."
            )
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = requests.get(f"{self.base_url}{path}", headers=self._headers(), params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = requests.post(f"{self.base_url}{path}", headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_cluster(self, project_id: str, cluster_id: str) -> dict[str, Any]:
        return self._get(f"/organizations/{self.settings.capella_org_id}/projects/{project_id}/clusters/{cluster_id}")

    def verify_cluster_reachable(self, dest: CouchbaseConnectionConfig) -> tuple[bool, dict[str, str]]:
        if dest.capella_project_id and dest.capella_cluster_id and self.token:
            try:
                data = self.get_cluster(dest.capella_project_id, dest.capella_cluster_id)
                state = data.get("currentState", "unknown")
                healthy = state.lower() in {"healthy", "running", "deploying"}
                return healthy, {"message": f"Capella cluster state: {state}", "state": state}
            except requests.HTTPError as exc:
                return False, {"message": f"Capella API error: {exc}"}
        return True, {"message": "Capella API token not configured; skipping management-plane check "
                                  "and relying on direct data-plane connectivity test."}

    def list_buckets(self, project_id: str, cluster_id: str) -> list[dict[str, Any]]:
        data = self._get(
            f"/organizations/{self.settings.capella_org_id}/projects/{project_id}"
            f"/clusters/{cluster_id}/buckets"
        )
        return data.get("data", [])

    def create_bucket(self, project_id: str, cluster_id: str, name: str, ram_quota_mb: int = 1024) -> dict[str, Any]:
        payload = {
            "name": name,
            "type": "couchbase",
            "storageBackend": "couchstore",
            "memoryAllocationInMb": ram_quota_mb,
            "bucketConflictResolution": "seqno",
            "durabilityLevel": "none",
            "replicas": 1,
            "flush": False,
            # Explicit, not just relying on storageBackend "couchstore" defaulting to
            # 1024 -- XDCR refuses to replicate between buckets with different
            # vbucket counts (confirmed against a live cluster on 2026-07-30:
            # "The number of vbuckets in source cluster, 1024, and target cluster,
            # 128, does not match. This configuration is not supported."), and a
            # bucket's vbucket count can never be changed after creation. Every
            # self-managed Couchbase Server source this app supports (7.2-8.0.2)
            # uses the traditional 1024-vbucket layout, so a Capella destination
            # bucket THIS APP creates should always match it for
            # continuous/hybrid (XDCR) migrations to even be possible.
            "numVBuckets": 1024,
        }
        return self._post(
            f"/organizations/{self.settings.capella_org_id}/projects/{project_id}"
            f"/clusters/{cluster_id}/buckets",
            payload,
        )

    def ensure_bucket_exists(self, dest: CouchbaseConnectionConfig, name: str, ram_quota_mb: int = 1024) -> bool:
        """Create the destination bucket on Capella if it doesn't already exist.
        Returns True if it was created."""
        if not (dest.capella_project_id and dest.capella_cluster_id and self.token):
            logger.warning("Capella project/cluster id or API token missing; skipping bucket auto-provisioning.")
            return False
        existing = {b["name"] for b in self.list_buckets(dest.capella_project_id, dest.capella_cluster_id)}
        if name in existing:
            return False
        self.create_bucket(dest.capella_project_id, dest.capella_cluster_id, name, ram_quota_mb=ram_quota_mb)
        return True
