# Couchbase Onboarding Agent

Migrate **MongoDB**, **Amazon DynamoDB**, **Redis**, **Apache Cassandra**, **Azure Cosmos DB**, or another
**Couchbase** cluster (Community Edition, Enterprise Edition, or Capella) into **Couchbase Server** or
**Couchbase Capella** -- with a guided wizard, a live dashboard, and an AI assistant that runs on a local
model.

Every source supports the three migration modes, including **bulk copy + continuous sync** with no gap
between the two, resumable checkpoints, a precise **rollback** of the destination, and a **fallback sync**
that keeps the source current after cutover so applications can move back without losing writes.

<img width="1470" height="811" alt="Couchbase Onboarding Agent dashboard" src="https://github.com/user-attachments/assets/917b19ef-0db2-44f5-9dae-6d6c764b1027" />

- [What it does](#what-it-does)
- [Supported sources](#supported-sources)
- [Quick start (Docker Compose)](#quick-start-docker-compose)
- [Kubernetes (Helm)](#kubernetes-helm)
- [How bulk copy + continuous sync works](#how-bulk-copy--continuous-sync-works)
- [Cutover, rollback and fallback](#cutover-rollback-and-fallback)
- [Security](#security)
- [Source prerequisites and notes](#source-prerequisites-and-notes)
- [Configuration](#configuration)
- [Operations and troubleshooting](#operations-and-troubleshooting)
- [Development](#development)

## What it does

- **Wizard**: connect and introspect the source (containers, estimated sizes, what continuous sync gives
  you on that source), configure the destination, pick a mode, validate, approve.
- **Three modes** for every source:

  | Mode | What happens | Ends |
  |---|---|---|
  | One-time migration (`full_load`) | Bulk copy of every included container, then per-container count verification | `complete` |
  | Continuous replication (`cdc_live`) | Change capture only, no separate bulk copy: starts from now on MongoDB, from what the stream retains on DynamoDB (24 h), and from the full data set on the polling/feed-based sources | at cutover |
  | Bulk copy + continuous sync (`full_load_and_cdc`) | Change position recorded, bulk copy, catch-up on changes made during the copy, then live sync | at cutover |

- **Validation** before approval: connectivity, versions, change-capture availability and how it behaves,
  collection-name collisions, document size, TLS and certificate verification, delete propagation,
  rollback safety (whether target collections already exist), and XDCR vBucket compatibility.
- **Live dashboard**: sync stage, throughput, replication lag, checkpoints, stream restarts,
  reconciliation results, failed writes, verification counts, bottleneck findings.
- **Bottleneck detection and auto-throttling**: source rate-limiting and destination backpressure reduce
  write concurrency live; stalled or degraded throughput is diagnosed in the assistant panel.
- **Assistant** (Qwen via an Ollama-compatible API, running locally): answers questions grounded in the
  migration's live state and recalls similar past events from a Couchbase-backed vector memory. Source
  credentials, samples and data never leave your deployment.
- **Light and dark mode**, following the OS setting by default (Appearance switch in the sidebar).

## Supported sources

| Source | Versions | Bulk copy | Continuous sync | Resumes from checkpoint | Deletes | Fallback after cutover |
|---|---|---|---|---|---|---|
| MongoDB (Atlas or self-managed) | 3.6 - 8.0 | `find()` scan | Change streams (replica set or sharded cluster) | Yes | In stream | Yes |
| Amazon DynamoDB | current API | Paginated `Scan` | DynamoDB Streams, following shard splits | Yes | In stream | Yes |
| Redis (OSS, Cloud, Enterprise) | 5.0+ | Type-aware `SCAN` | Keyspace notifications + re-read of changed keys | Re-scans after a restart | In stream | Yes |
| Apache Cassandra / DSE | 2.1 - 4.x | Paged `SELECT *` | `WRITETIME()` polling | Yes | Reconciliation | Yes |
| Azure Cosmos DB (SQL API) | current API | Cross-partition query | Change feed (latest version mode) | Yes | Reconciliation | Yes |
| Couchbase Community Edition | 7.2 - 8.0.2 | `cbbackupmgr` | CAS polling via the Query service | Yes | Reconciliation | Yes |
| Couchbase Enterprise Edition | 7.2 - 8.0.2 | `cbbackupmgr` | XDCR | Yes (XDCR) | In stream | Yes |
| Couchbase Capella (as a source) | current | `cbbackupmgr` | CAS polling via the Query service | Yes | Reconciliation | Yes |

"Reconciliation" means a periodic key comparison removes documents deleted at the source (see
[below](#delete-reconciliation)). Destinations: Couchbase Server Enterprise Edition 7.2 - 8.0.2 or
Couchbase Capella.

## Quick start (Docker Compose)

Requirements: Docker with Compose v2, ~12 GB of free memory (the local model and the agent-memory
Couchbase node), and network access from this machine to your source and destination.

```bash
cp env.example .env
# Edit .env:
#   MEMORY_CB_PASSWORD   required -- admin password for the agent-memory container
#   API_AUTH_TOKEN       recommended -- e.g. `openssl rand -hex 32`; the UI asks for it
docker compose up --build
```

Then open <http://localhost:5173>. The API is at <http://localhost:8000> (OpenAPI docs at `/docs`). All
ports bind to `127.0.0.1`.

The first start pulls the Qwen model (`qwen3:8b`, ~5 GB) and initializes the agent-memory node; later
starts are fast. Behind a TLS-inspecting corporate proxy, run `./scripts/setup-corporate-ca.sh` before
building (see [troubleshooting](#build-fails-behind-a-corporate-proxy)).

> The agent-memory container uses the `couchbase:enterprise` image, which is free for development and
> evaluation under Couchbase's Enterprise Free license and requires a subscription in production (see
> [couchbase.com/legal](https://www.couchbase.com/legal/agreements/)). For production, point `MEMORY_CB_*`
> at a cluster you're licensed for, or at Capella. The same applies to the `cbbackupmgr` binary the backend
> image copies from that image for Couchbase-to-Couchbase migrations.

## Kubernetes (Helm)

A chart lives in [`deploy/helm/couchbase-onboarding-agent`](deploy/helm/couchbase-onboarding-agent). It runs
the backend (single replica, persistent state volume), the UI behind nginx, and optionally an in-cluster
Ollama and an evaluation agent-memory node, with generated secrets and non-root pods.

```bash
helm install onboarding deploy/helm/couchbase-onboarding-agent -n migrations --create-namespace
kubectl get secret onboarding-couchbase-onboarding-agent-secrets -n migrations \
  -o jsonpath='{.data.API_AUTH_TOKEN}' | base64 -d; echo
kubectl port-forward -n migrations svc/onboarding-couchbase-onboarding-agent-frontend 8080:80
```

For production -- external agent memory, an existing LLM endpoint, ingress with TLS, your own image
registry, backups -- see the [chart README](deploy/helm/couchbase-onboarding-agent/README.md).

## How bulk copy + continuous sync works

```
record change position ──▶ bulk copy ──▶ catch up ──▶ streaming ──▶ cutover (drain, verify)
        (no gap)            (idempotent)   (replay)     (live)
```

1. **Record the change position first.** Before reading any data, the connector records "now" in the
   source's change log: a MongoDB change-stream resume token or cluster time, a DynamoDB capture time (shards
   are then read from their start, skipping older records), a Cosmos DB change-feed start time, a Cassandra
   `WRITETIME` watermark, a Couchbase CAS watermark, or -- for Redis, whose notifications aren't durable -- a
   live subscription that records which keys change.
2. **Bulk copy.** Containers are read in batches and written with bounded, auto-throttled concurrency.
   Writes are upserts with deterministic keys, so re-running a copy (after a failure or restart) is safe.
3. **Catch up.** The change stream starts from the recorded position, so every write made during the copy
   is replayed. Replaying a change the copy already saw is harmless -- which makes the pipeline
   at-least-once with idempotent writes, i.e. no gap and no duplicates in the destination.
4. **Streaming.** Changes are applied in small batches; within a batch the last change per key wins.
   - Each write is retried; one that still fails is recorded under *Writes that failed after retries* and
     doesn't block the stream. If the destination rejects an entire batch, the stream restarts from the
     last checkpoint instead of skipping past it.
   - The checkpoint is saved after each batch is written (at most once a second), so a crash replays about
     a second of changes.
   - A dropped stream reconnects from the checkpoint with exponential backoff. After
     `CDC_MAX_CONSECUTIVE_FAILURES` in a row the migration is marked failed; **Resume from checkpoint**
     continues where it stopped once the cause is fixed.
   - After a backend restart, running migrations resume automatically from their checkpoints
     (`AUTO_RESUME_ON_STARTUP`).
5. **Cutover.** See below.

### Delete reconciliation

Some change-capture mechanisms never report deletes (Cassandra polling, the Cosmos DB change feed,
Couchbase CAS polling), and any stream can miss events during an outage. While a migration is streaming,
reconciliation runs every `reconcile_interval_minutes` (set in the wizard; default 60, 0 = manual) and on
demand from the dashboard. It lists source keys (keys only), compares them with what the migration wrote,
and removes destination documents whose key no longer exists at the source. Safety rules: documents written
after the run started are never touched, each delete is CAS-guarded and re-checks the document, an empty
source listing deletes nothing, and a run that would delete more than `RECONCILE_MAX_DELETE_FRACTION` of a
collection stops for confirmation.

### What gets written

| Source record | Couchbase document key | Notes |
|---|---|---|
| MongoDB document (`_id`) | `collection::<_id>` | ObjectId, dates, Decimal128 and binary converted to JSON; original types recorded for fallback |
| DynamoDB item | `table::<pk>[::<sk>]` | From the table's `KeySchema`; sets, binary and numbers recorded for fallback |
| Redis key | `redis::<key>` | Grouped into containers by the prefix before the first `:`; value stored as `{redis_type, value, ttl}` |
| Cassandra row | `table::<pk>[::<ck>...]` | Collections and counters preserved as JSON |
| Cosmos DB item | `container::<pk>::<id>` | `_rid`, `_self`, `_etag`, `_attachments` dropped; `_ts` kept as `_cosmos_ts` |
| Couchbase document | unchanged | Same key, scope and collection; document body unmodified |

Documents from non-Couchbase sources carry a `_migration` field (migration id, source container, write
time, and the source identity and type hints fallback needs). Verification, reconciliation and rollback
select by it. Each source container becomes a collection (a scope named after the database/keyspace by
default); the wizard lets you set explicit names, and validation flags names that would collide.

## Cutover, rollback and fallback

### Cutover

1. Stop application writes to the source (or put it in read-only mode).
2. Click **Cut over**. The sync applies the remaining changes until the stream reports it's caught up (up to
   `CUTOVER_DRAIN_TIMEOUT_S`), then verifies per-container counts -- source keys vs. destination documents --
   and shows the result in the *Verification* table.
3. Point applications at Couchbase. The migration is `complete`.

**Pause sync** stops without cutover; the source stays the system of record and **Resume** continues from
the checkpoint.

### Rollback (undo the destination)

**Roll back...** shows a dry-run plan before anything happens. Forward migration never writes to the source,
so rollback only undoes the destination:

- Collections the migration **created** are dropped (the migration records which collections existed before
  its first write).
- In collections that **already existed**, documents the migration wrote are deleted by their `_migration`
  tag, and documents it **overwrote or deleted** are restored from a per-migration pre-image journal
  (`onboarding_agent.rb_<id>`). The journal stores the first version the migration touched, so it restores
  exactly what was there before. Turn journaling off in the wizard only if you don't need this.
- The destination bucket is dropped only if the migration created it and you ask for it (self-managed
  clusters).
- Each step is recorded as it completes; if a rollback fails partway, running it again continues from there.

Rolling back **after cutover** removes what applications have written to those documents since, so the UI
requires typing the migration name. Prefer fallback sync in that case.

For Couchbase sources the bulk copy is a `cbbackupmgr` restore, which carries no tag: restoring into
collections that already existed can't be undone document by document, and validation warns about it. Use
new collections if you need a clean rollback.

### Fallback sync (after cutover)

**Start fallback sync** writes every change made in Couchbase since cutover back to the source -- upserts in
CAS order and deletes -- restoring source-native types (ObjectId, dates, sets, binary, Redis types and TTLs)
from the recorded hints. If you need to move applications back:

1. Stop writes to Couchbase.
2. Click **Fall back to source...** and confirm. A final drain runs, the migration becomes `fell_back`, and
   the source is the system of record again with nothing lost.

Or **Stop fallback sync** to stay on Couchbase. Fallback is the only path that writes to the source, and only
when an operator starts it; the source user needs write permissions for it.

## Security

- **API token.** Set `API_AUTH_TOKEN` to require `Authorization: Bearer <token>` on every API call and a
  `?token=` on websockets (the UI prompts for it and keeps it for the browser tab). Only `/api/health` and
  `/api/config` are open. Without a token the API is open to anyone who can reach it -- acceptable only on
  a single-user machine with ports bound to localhost, which is what Compose does.
- **Credentials.** Source and destination credentials are masked (`********`) in every API response and
  websocket frame, and encrypted at rest in the state file with a Fernet key (`STATE_ENCRYPTION_KEY`, or a
  key generated next to the state file on first start). Back up the key together with the state volume.
- **TLS.** Certificate verification is on for every TLS connection, including `cbbackupmgr` and the
  Couchbase management REST API. Paste a CA certificate in the wizard for private CAs; skipping
  verification is available for test clusters and flagged by validation.
- **Least privilege.** Forward migration needs read access at the source (plus change-stream/stream/
  change-feed read permissions, and `CONFIG GET` on Redis for introspection). Fallback sync needs write
  access. The destination user needs to create scopes, collections and indexes, and read/write documents.
  Couchbase Enterprise Edition sources need XDCR admin rights on the source to set up replication.
- **Containers**: the backend and UI images run as non-root users, and the UI sets a restrictive
  Content-Security-Policy.

Report vulnerabilities privately -- see [SECURITY.md](SECURITY.md).

## Source prerequisites and notes

**MongoDB.** Change streams need a replica set or sharded cluster; a standalone `mongod` supports one-time
migration only (convert it with `rs.initiate()` for continuous sync). Keep the oplog window longer than the
bulk copy takes, or the resume position can age out. Atlas shared tiers reject `noCursorTimeout` cursors;
the connector falls back automatically.

**DynamoDB.** Enable Streams on each table with `NEW_IMAGE` or `NEW_AND_OLD_IMAGES`. Stream records are kept
for 24 hours, so a bulk copy (or an outage) longer than that loses changes -- the stream reports it and the
container needs a new bulk copy. Shards are re-discovered every `DYNAMODB_SHARD_DISCOVERY_INTERVAL_S`, and a
child shard is read only after its parent, preserving per-item order across splits.

**Redis.** Configure keyevent notifications on the source, e.g. `CONFIG SET notify-keyspace-events KEA`.
Notifications aren't durable, so the connector re-reads changed keys rather than trusting message contents,
and re-scans every key after a restart or dropped subscription (no changes lost; expect a re-scan's worth of
reads). Past `REDIS_DIRTY_KEY_LIMIT` changed keys during a bulk copy it re-scans instead. Redis Cluster
publishes notifications per node: point the connector at a single primary or a non-clustered endpoint.

**Cassandra.** Continuous sync polls each table using `WRITETIME()` across all regular columns, because
commit-log CDC needs an agent on every node. Each poll scans the whole table, so
`CASSANDRA_CDC_POLL_INTERVAL_S` trades cost for latency. Tables whose only non-key columns are counters or
non-frozen collections can't be polled (introspection flags them). Deletes arrive through reconciliation.

**Cosmos DB.** Uses the change feed in latest-version mode (durable, resumable by continuation token); it
doesn't report deletes, so they arrive through reconciliation. Continuous replication without a bulk copy
reads the change feed from the beginning, which copies the current version of every item.

**Couchbase Enterprise Edition.** Bulk copy runs `cbbackupmgr backup` + `restore` scoped to the source bucket;
continuous sync creates an XDCR replication **on the source cluster** (a remote-cluster reference named
`onboarding-agent-<migration id>`), torn down at cutover, pause or rollback. XDCR requires the source and
destination buckets to have the same vBucket count, which can't be changed after a bucket is created --
Couchbase Server 8.0 defaults new Magma buckets to 128 vBuckets while 7.x sources use 1024. Buckets this app
creates always request 1024, and validation blocks approval on a mismatch with an existing bucket. The
backend clears stale `onboarding-agent-*` remote-cluster references that point at the same destination
before creating a new one.

**Couchbase Community Edition and Capella (as sources).** No XDCR from here, so continuous sync polls the
Query service for documents whose `META().cas` moved past a watermark (re-reading a short overlap window to
absorb clock skew between nodes). Create an index first on large collections:
`CREATE INDEX idx_onboarding_cas ON bucket.scope.collection(META().cas)`. The bulk copy (`cbbackupmgr`) copies
the whole bucket; continuous sync follows only the included collections.

**Reachability.** Allow-list the backend's egress IP on each source and on the destination (Capella:
project > Allowed IPs). Capella requires `couchbases://`.

## Configuration

Backend settings are environment variables (see [`backend/app/config.py`](backend/app/config.py) for all of
them and [`env.example`](env.example) for a starting `.env`).

| Variable | Default | Purpose |
|---|---|---|
| `API_AUTH_TOKEN` | empty | Require this bearer token on the API and websockets |
| `STATE_ENCRYPTION_KEY` | generated | Fernet key for credentials in the state file |
| `MIGRATION_STATE_FILE` | `/data/state/migrations.json` | Migration records, checkpoints (single backend replica) |
| `AUTO_RESUME_ON_STARTUP` | `true` | Resume running migrations after a restart |
| `CDC_BATCH_SIZE` / `CDC_BATCH_MAX_WAIT_MS` | `200` / `500` | Change micro-batch size and flush interval |
| `CDC_WRITE_RETRIES` | `3` | Retries per change before it's recorded as failed |
| `CDC_MAX_CONSECUTIVE_FAILURES` | `20` | Stream restarts in a row before the migration fails |
| `RECONCILE_MAX_DELETE_FRACTION` | `0.2` | Reconciliation stops for confirmation above this share of deletes |
| `CUTOVER_DRAIN_TIMEOUT_S` | `120` | How long cutover waits for the stream to catch up |
| `VERIFY_SOURCE_COUNTS` | `true` | Count source keys during verification (a key scan per container) |
| `CASSANDRA_CDC_POLL_INTERVAL_S` | `30` | Cassandra poll interval |
| `COUCHBASE_CDC_POLL_INTERVAL_S` / `COUCHBASE_CDC_OVERLAP_S` | `5` / `10` | CAS polling interval and overlap window |
| `FALLBACK_POLL_INTERVAL_S` | `5` | Fallback sync poll interval |
| `QWEN_BASE_URL`, `QWEN_MODEL_NAME` | `http://qwen-service:11434`, `qwen3:8b` | Any Ollama-compatible endpoint (`/api/chat`, `/api/embeddings`) |
| `MEMORY_CB_*` | bundled node | Agent-memory cluster (not a migration source or destination) |
| `CAPELLA_API_TOKEN`, `CAPELLA_ORG_ID` | empty | Auto-provision Capella destination buckets |
| `CBBACKUPMGR_PATH`, `COUCHBASE_BACKUP_ARCHIVE_DIR` | `cbbackupmgr`, `/data/cbbackupmgr-archives` | Couchbase-to-Couchbase bulk copy |

The UI calls the API on its own origin (`/api`, `/ws`), proxied by nginx in the container and by Vite in
development. Set `VITE_API_BASE_URL` at build time only if you host the UI separately.

## Operations and troubleshooting

**State and backups.** One backend replica owns the state file (encrypted records, checkpoints,
fallback key lists) on the `state_data` volume or the chart's state PVC. Back it up with the encryption key.
Destination-side rollback data (the pre-image journal) lives in the destination bucket itself.

**Restarts.** Running migrations resume from their checkpoints on startup. A `cbbackupmgr` bulk copy that
was interrupted is re-run (incrementally) on resume. An interrupted rollback is marked failed; running it
again continues from the last completed step. XDCR runs on the source cluster and keeps replicating while
the backend is down; resume picks the replication back up.

#### Build fails behind a corporate proxy

TLS-inspecting proxies (Zscaler, Netskope, ...) break `pip`, `npm` and the model pull because each checks its
own CA bundle. On macOS, `./scripts/setup-corporate-ca.sh` exports the proxy root CA into each build context's
`certs/` (git-ignored); on Linux, copy your organization's root CA `.crt` there. Then
`docker compose build --no-cache`.

#### First boot never becomes healthy

The agent-memory node can take a couple of minutes on a cold start. If an earlier `docker compose up` was
interrupted during initialization, reset its volumes: `docker compose down -v && docker compose up --build`
(this also removes migration state).

#### `cannot find remote cluster` (Couchbase Enterprise Edition sources)

Usually a stale `onboarding-agent-*` remote-cluster reference on the source cluster from an older version of
this app. Delete it in the source's Web Console (XDCR > Remote Clusters) and start again.

## Development

```bash
# Backend (Python 3.10+)
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check app tests
pytest
MIGRATION_STATE_FILE=./.data/state.json SCRATCH_DIR=./.data/scratch uvicorn app.main:app --reload

# Frontend (Node 20)
cd frontend
npm ci
npm run dev        # http://localhost:5173, proxies /api and /ws to localhost:8000
npm run build      # type-checks (tsc -b) and builds
```

Tests use in-memory fakes for the engine (sync ordering, retries, cutover, rollback), `moto` for DynamoDB
Streams and `fakeredis` for Redis; no live databases are needed. CI runs lint, tests, the UI build and
`helm lint`.

| Path | What's there |
|---|---|
| `backend/app/core/migration_engine.py` | Orchestration: bulk copy, sync loop, cutover, resume, rollback, fallback |
| `backend/app/core/connectors/` | One connector per source (`base.py` is the interface) |
| `backend/app/core/couchbase_loader.py` | Destination writes, pre-image journal, rollback primitives |
| `backend/app/core/reconcile.py`, `fallback.py` | Delete reconciliation; post-cutover reverse sync |
| `backend/app/core/couchbase_native.py` | `cbbackupmgr` and XDCR for Couchbase sources |
| `backend/app/core/validator.py` | Pre-approval checks |
| `frontend/src/` | React + TypeScript UI (`theme/` holds the light and dark tokens) |
| `deploy/helm/` | Helm chart |

### Adding a source

1. Implement `SourceConnector` in `backend/app/core/connectors/<name>.py`: `test_connection()`, `extract()`,
   and for continuous sync `capture_change_position()`, `stream_changes()` (yield a heartbeat while idle),
   `list_keys()` and optionally `apply_reverse()`; declare `capabilities`.
2. Register it in `connectors/registry.py`, add a `SourceType` value and its connection fields
   (`models/schemas.py`, `main.py`'s `/api/source-types`), and a form section in
   `frontend/src/components/wizard/SourceConfigForm.tsx`.
3. Add the SDK to `backend/requirements.txt` and tests with a fake or an in-process emulator.

The engine, validator, loader, reconciliation, rollback and fallback work against the interface, so nothing
else needs to change.

## Contributing and license

Released under the [MIT License](LICENSE)
