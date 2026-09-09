"""
Thin wrapper around the Couchbase Python SDK (couchbase-python-client, cluster >= 4.x)
used to introspect and manage the DESTINATION cluster: version, buckets, scopes,
collections. Works against on-prem Couchbase Server and Capella (Capella just uses
couchbases:// with TLS + Capella-issued credentials).
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.exceptions import CouchbaseException, ScopeAlreadyExistsException, CollectionAlreadyExistsException
from couchbase.management.collections import CollectionSpec
from couchbase.options import ClusterOptions
import requests

from app.models.schemas import CouchbaseConnectionConfig, CouchbaseNode, CouchbaseTopologySnapshot

logger = logging.getLogger(__name__)

# Couchbase scope/collection names: 1-251 chars, cannot start with `_` or `%`,
# alphanumeric plus a small punctuation set. Source container names (a Mongo
# collection, a Cassandra "keyspace.table", a Redis logical namespace, ...) commonly
# violate this (dots, spaces, leading underscores for Mongo system collections, etc.)
# -- sanitize deterministically so the same source name always maps to the same
# Couchbase name.
_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_%\-]")


def sanitize_couchbase_name(name: str, *, max_len: int = 251) -> str:
    cleaned = _INVALID_NAME_CHARS.sub("_", name)
    cleaned = cleaned.lstrip("_%") or "col"
    return cleaned[:max_len]


class CouchbaseClientError(RuntimeError):
    pass


class CouchbaseClusterClient:
    """Wraps a live SDK connection plus the cluster's REST management API
    (port 8091/18091), which the SDK does not expose (pools/default, bucket
    creation, etc.)."""

    def __init__(self, config: CouchbaseConnectionConfig):
        self.config = config
        self._cluster: Cluster | None = None

    def connect(self, timeout_s: int = 15) -> Cluster:
        if self._cluster is not None:
            return self._cluster
        try:
            auth = PasswordAuthenticator(self.config.username, self.config.password)
            opts = ClusterOptions(auth)
            opts.apply_profile("wan_development")
            cluster = Cluster(self.config.connection_string, opts)
            cluster.wait_until_ready(timedelta(seconds=timeout_s))
            self._cluster = cluster
            return cluster
        except CouchbaseException as exc:
            raise CouchbaseClientError(f"Failed to connect to {self.config.label}: {exc}") from exc

    def close(self) -> None:
        if self._cluster is not None:
            self._cluster.close()
            self._cluster = None

    # -- REST management API helpers ------------------------------------

    def _mgmt_base_url(self) -> str:
        host = (
            self.config.connection_string.replace("couchbases://", "")
            .replace("couchbase://", "")
            .split(",")[0]
            .split("/")[0]
        )
        if self.config.is_capella:
            return f"https://{host}:18091"
        scheme = "https" if self.config.use_tls else "http"
        port = 18091 if self.config.use_tls else 8091
        return f"{scheme}://{host}:{port}"

    def _rest_get(self, path: str) -> dict[str, Any]:
        url = f"{self._mgmt_base_url()}{path}"
        try:
            resp = requests.get(
                url,
                auth=(self.config.username, self.config.password),
                verify=self.config.ca_cert_path if self.config.ca_cert_path else False,
                timeout=15,
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                raise CouchbaseClientError(
                    f"{self.config.label}: authentication failed (401 Unauthorized) calling {url}. "
                    "Double-check the username/password."
                ) from exc
            if status == 403:
                raise CouchbaseClientError(
                    f"{self.config.label}: {self.config.username!r} authenticated but lacks permission "
                    f"(403 Forbidden) for {url}. It needs a cluster-level admin role."
                ) from exc
            raise CouchbaseClientError(f"{self.config.label}: REST call to {url} failed: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise CouchbaseClientError(
                f"{self.config.label}: could not reach {url} ({exc}). Confirm the hostname/port are "
                "correct and reachable from inside the backend container."
            ) from exc
        return resp.json()

    def _rest_post(self, path: str, data: dict[str, Any]) -> requests.Response:
        url = f"{self._mgmt_base_url()}{path}"
        return requests.post(
            url, auth=(self.config.username, self.config.password), data=data,
            verify=self.config.ca_cert_path if self.config.ca_cert_path else False, timeout=30,
        )

    # -- introspection ----------------------------------------------------

    def get_pools_default(self) -> dict[str, Any]:
        return self._rest_get("/pools/default")

    def get_server_version(self) -> str:
        pools = self.get_pools_default()
        nodes = pools.get("nodes", [])
        if not nodes:
            raise CouchbaseClientError("No nodes reported by cluster; cannot determine version")
        return nodes[0].get("version", "unknown").split("-")[0]

    def get_nodes(self) -> list[CouchbaseNode]:
        pools = self.get_pools_default()
        nodes = []
        for n in pools.get("nodes", []):
            nodes.append(
                CouchbaseNode(
                    hostname=n.get("hostname", "unknown"),
                    services=n.get("services", ["kv"]),
                    version=n.get("version", "").split("-")[0],
                    status=n.get("status", "unknown"),
                )
            )
        return nodes

    def get_buckets(self) -> list[dict[str, Any]]:
        return self._rest_get("/pools/default/buckets")

    def get_scopes_and_collections(self, bucket: str) -> dict[str, list[str]]:
        data = self._rest_get(f"/pools/default/buckets/{bucket}/scopes")
        result: dict[str, list[str]] = {}
        for scope in data.get("scopes", []):
            result[scope["name"]] = [c["name"] for c in scope.get("collections", [])]
        return result

    def get_vbucket_count(self, bucket: str) -> int | None:
        """Returns this destination bucket's vBucket count, or None if the
        bucket doesn't exist yet or the field can't be read -- best-effort,
        used by validator.py's XDCR_VBUCKET_COMPAT pre-flight check (a bucket
        that doesn't exist yet just hasn't been auto-provisioned, which this
        app now always does with a 1024-vBucket bucket for a Couchbase source
        -- see capella_client.py's create_bucket()). This same classic REST
        call already works against a Capella destination (see _mgmt_base_url()
        above), not just self-managed clusters."""
        try:
            data = self._rest_get(f"/pools/default/buckets/{bucket}")
        except CouchbaseClientError:
            return None
        vbucket_map = (data.get("vBucketServerMap") or {}).get("vBucketMap")
        return len(vbucket_map) if vbucket_map else None

    def snapshot_topology(self) -> CouchbaseTopologySnapshot:
        pools = self.get_pools_default()
        version = self.get_server_version()
        nodes = self.get_nodes()
        bucket_summaries = self.get_buckets()
        bucket_names = [b["name"] for b in bucket_summaries]

        scopes_by_bucket: dict[str, list[str]] = {}
        collections_by_bucket: dict[str, list[str]] = {}
        total_docs = 0
        total_size = 0

        for b in bucket_summaries:
            name = b["name"]
            try:
                scopes = self.get_scopes_and_collections(name)
                scopes_by_bucket[name] = list(scopes.keys())
                collections_by_bucket[name] = [c for cols in scopes.values() for c in cols]
            except Exception:  # noqa: BLE001
                scopes_by_bucket[name] = []
                collections_by_bucket[name] = []
            stats = b.get("basicStats", {})
            total_docs += stats.get("itemCount", 0) or 0
            total_size += stats.get("dataUsed", 0) or 0

        return CouchbaseTopologySnapshot(
            cluster_uuid=pools.get("uuid") if isinstance(pools.get("uuid"), str) else None,
            cluster_version=version,
            nodes=nodes,
            buckets=bucket_names,
            scopes_by_bucket=scopes_by_bucket,
            collections_by_bucket=collections_by_bucket,
            total_docs=total_docs,
            total_data_size_bytes=total_size,
        )

    # -- provisioning -------------------------------------------------------

    def ensure_bucket(self, name: str, ram_quota_mb: int = 1024) -> bool:
        """Create the bucket if it doesn't already exist (self-managed clusters only
        -- Capella buckets are created via CapellaClient instead, since Capella's
        management REST API differs from self-managed Couchbase Server's). Returns
        True if it was created."""
        existing = {b["name"] for b in self.get_buckets()}
        if name in existing:
            return False
        resp = self._rest_post("/pools/default/buckets", {
            "name": name, "bucketType": "couchbase", "ramQuotaMB": ram_quota_mb,
            "replicaNumber": 1, "flushEnabled": 0,
        })
        if resp.status_code not in (200, 202):
            raise CouchbaseClientError(f"Failed to create bucket {name!r}: {resp.status_code} {resp.text}")
        return True

    def ensure_scope_and_collection(self, bucket: str, scope: str, collection: str) -> None:
        """Idempotently create `scope`/`collection` in `bucket` via the SDK's
        CollectionManager. Names must already be sanitized (see
        sanitize_couchbase_name) before reaching here."""
        cluster = self.connect()
        mgr = cluster.bucket(bucket).collections()
        if scope != "_default":
            try:
                mgr.create_scope(scope)
            except ScopeAlreadyExistsException:
                pass
        try:
            mgr.create_collection(CollectionSpec(collection_name=collection, scope_name=scope))
        except CollectionAlreadyExistsException:
            pass
