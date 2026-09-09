#!/usr/bin/env python3
"""
One-shot initializer for the Couchbase Enterprise Edition instance used as the
onboarding agent's long-term memory store. Run as a short-lived container (see the
`memory-init` service in docker-compose.yml) after `couchbase-memory` becomes
healthy.

Steps:
  1. Wait for the node to accept REST calls.
  2. Initialize the node (data/index/search/query services, memory quotas).
  3. Create the `Administrator` cluster user (from env).
  4. Create the `agent_memory` bucket, `agent` scope, `episodes` collection.
  5. Create a primary GSI index (lets the agent run N1QL over memory -- used by
     backend/app/memory/couchbase_memory.py's cosine-similarity fallback if the
     vector index below is ever unavailable).
  6. Create the `agent_memory_vector_idx` FTS vector index from
     vector_index.json for native ANN vector search (requires Enterprise Edition
     or Capella). Non-fatal if this fails: the backend falls back to the N1QL +
     cosine-similarity path from step 5 automatically.

Idempotent: safe to re-run against an already-initialized cluster.
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

HOST = os.environ.get("MEMORY_CB_HOST", "couchbase-memory")
USERNAME = os.environ.get("MEMORY_CB_USERNAME", "Administrator")
PASSWORD = os.environ.get("MEMORY_CB_PASSWORD", "password")
BUCKET = os.environ.get("MEMORY_CB_BUCKET", "agent_memory")
SCOPE = os.environ.get("MEMORY_CB_SCOPE", "agent")
COLLECTION = os.environ.get("MEMORY_CB_COLLECTION", "episodes")
RAM_QUOTA_MB = int(os.environ.get("MEMORY_CB_RAM_QUOTA_MB", "512"))
INDEX_RAM_QUOTA_MB = int(os.environ.get("MEMORY_CB_INDEX_RAM_QUOTA_MB", "256"))
FTS_RAM_QUOTA_MB = int(os.environ.get("MEMORY_CB_FTS_RAM_QUOTA_MB", "256"))

MGMT = f"http://{HOST}:8091"
FTS = f"http://{HOST}:8094"
AUTH = (USERNAME, PASSWORD)


def wait_for_node(timeout_s: int = 240) -> None:
    print(f"Waiting for {MGMT} to accept connections...")
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            r = requests.get(f"{MGMT}/pools", timeout=3)
            if r.status_code == 200:
                print("Node is reachable.")
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    print("ERROR: timed out waiting for couchbase-memory node.", file=sys.stderr)
    sys.exit(1)


def already_initialized() -> bool:
    r = requests.get(f"{MGMT}/pools/default", auth=AUTH, timeout=5)
    return r.status_code == 200


def init_node() -> None:
    if already_initialized():
        print("Cluster already initialized; skipping node init.")
        return

    print("Setting memory quotas...")
    requests.post(
        f"{MGMT}/pools/default",
        data={"memoryQuota": RAM_QUOTA_MB, "indexMemoryQuota": INDEX_RAM_QUOTA_MB, "ftsMemoryQuota": FTS_RAM_QUOTA_MB},
        timeout=10,
    ).raise_for_status()

    print("Enabling services: kv, index, n1ql, fts...")
    requests.post(
        f"{MGMT}/node/controller/setupServices",
        data={"services": "kv,index,n1ql,fts"},
        timeout=10,
    ).raise_for_status()

    # The index service refuses to create ANY index -- primary or otherwise -- until
    # its storage mode has been explicitly set once. Must happen after
    # setupServices and before the admin user is created.
    print("Setting indexer storage mode (plasma)...")
    requests.post(
        f"{MGMT}/settings/indexes",
        data={"storageMode": "plasma"},
        timeout=10,
    ).raise_for_status()

    print(f"Creating administrator user '{USERNAME}'...")
    requests.post(
        f"{MGMT}/settings/web",
        data={"username": USERNAME, "password": PASSWORD, "port": "SAME"},
        timeout=10,
    ).raise_for_status()


def ensure_bucket() -> None:
    r = requests.get(f"{MGMT}/pools/default/buckets/{BUCKET}", auth=AUTH, timeout=5)
    if r.status_code == 200:
        print(f"Bucket '{BUCKET}' already exists.")
        return
    print(f"Creating bucket '{BUCKET}'...")
    requests.post(
        f"{MGMT}/pools/default/buckets",
        auth=AUTH,
        data={
            "name": BUCKET,
            "bucketType": "couchbase",
            "ramQuotaMB": RAM_QUOTA_MB,
            "replicaNumber": 0,
            "flushEnabled": 0,
        },
        timeout=15,
    ).raise_for_status()
    time.sleep(3)  # allow bucket to warm up before scope/collection calls


def ensure_scope_and_collection() -> None:
    scopes_resp = requests.get(f"{MGMT}/pools/default/buckets/{BUCKET}/scopes", auth=AUTH, timeout=10)
    scopes_resp.raise_for_status()
    scopes = {s["name"]: s for s in scopes_resp.json().get("scopes", [])}

    if SCOPE not in scopes:
        print(f"Creating scope '{SCOPE}'...")
        requests.post(
            f"{MGMT}/pools/default/buckets/{BUCKET}/scopes",
            auth=AUTH, data={"name": SCOPE}, timeout=10,
        ).raise_for_status()
        collections: list[str] = []
    else:
        collections = [c["name"] for c in scopes[SCOPE].get("collections", [])]

    if COLLECTION not in collections:
        print(f"Creating collection '{COLLECTION}'...")
        requests.post(
            f"{MGMT}/pools/default/buckets/{BUCKET}/scopes/{SCOPE}/collections",
            auth=AUTH, data={"name": COLLECTION}, timeout=10,
        ).raise_for_status()
        time.sleep(3)


def ensure_primary_index(attempts: int = 20, delay_s: float = 3.0) -> None:
    try:
        requests.post(
            f"{MGMT}/settings/indexes", auth=AUTH, data={"storageMode": "plasma"}, timeout=10,
        )
    except requests.RequestException as exc:
        print(f"(non-fatal) could not set indexer storage mode: {exc}")

    print("Creating primary GSI index (if not present)...")
    query = f"CREATE PRIMARY INDEX IF NOT EXISTS ON `{BUCKET}`.`{SCOPE}`.`{COLLECTION}`"
    last_error = None
    last_attempt = 0
    for attempt in range(1, attempts + 1):
        last_attempt = attempt
        r = requests.post(
            f"{MGMT.replace('8091', '8093')}/query/service",
            auth=AUTH, data={"statement": query}, timeout=30,
        )
        if r.status_code == 200:
            print(f"Primary index ready (attempt {attempt}/{attempts}).")
            return
        last_error = f"{r.status_code}: {r.text[:300]}"
        print(f"Primary index not ready yet ({last_error}), retrying ({attempt}/{attempts})...")
        time.sleep(delay_s)
    print(f"WARNING: primary index creation did not succeed after {last_attempt} attempts: {last_error}")


def ensure_vector_index() -> None:
    index_path = os.path.join(os.path.dirname(__file__), "..", "couchbase-memory", "vector_index.json")
    with open(index_path) as f:
        definition = json.load(f)
    definition["sourceName"] = BUCKET

    name = definition["name"]
    existing = requests.get(f"{FTS}/api/index/{name}", auth=AUTH, timeout=5)
    if existing.status_code == 200:
        print(f"FTS vector index '{name}' already exists.")
        return

    print(f"Attempting to create FTS vector index '{name}' (requires Enterprise Edition or Capella)...")
    r = requests.put(
        f"{FTS}/api/index/{name}",
        auth=AUTH, json=definition, timeout=20,
        headers={"Content-Type": "application/json"},
    )
    if r.status_code not in (200, 201):
        print(f"WARNING: FTS vector index creation returned {r.status_code}: {r.text[:500]}")
        print(
            "Not fatal: the backend automatically falls back to a N1QL + "
            "cosine-similarity scan for memory recall (see "
            "backend/app/memory/couchbase_memory.py) if the index isn't available."
        )
    else:
        print("FTS vector index created -- native ANN vector search is active for agent memory.")


if __name__ == "__main__":
    wait_for_node()
    init_node()
    ensure_bucket()
    ensure_scope_and_collection()
    ensure_primary_index()
    ensure_vector_index()
    print("Agent memory store initialization complete.")
