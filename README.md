# Couchbase Onboarding Agent
AI agent with a UI driven wizard and dashboard **for migrating** **MongoDB**, **Amazon DynamoDB**, **Redis**, **Apache
Cassandra**, **Microsoft Azure Cosmos DB**, or another **Couchbase cluster** (**Community
Edition**, **Enterprise Edition**, or **Capella**) into **Couchbase Server (Enterprise Edition)**
or **Couchbase Capella**.

The **Couchbase Onboarding Agent** actively monitors migrations and performs **Bottleneck Detection & Auto-throttling**. While a migration is actively running, a monitor watches live throughput and batch outcomes for stalled or degraded transfer rates, source-side rate-limiting (e.g. DynamoDB's provisioned-throughput errors, MongoDB Atlas connection storms), and destination backpressure (elevated Couchbase upsert failures). Source-throttling and destination-backpressure findings are auto-remediated by reducing pipeline concurrency live, with no restart required; stalled or degraded throughput is surfaced as a diagnosis and suggestion, since a concurrency change alone can't fix a dropped connection or a genuine network problem.
***
<img width="1470" height="811" alt="image" src="https://github.com/user-attachments/assets/917b19ef-0db2-44f5-9dae-6d6c764b1027" />

## Demo Videos

Video: Couchbase Entperise on EC2 to Couchbase Capella migration with XDCR, for a phased migration over time

https://erikhinderer.github.io/couchbase-onboarding-agent/mongodb-atlas_migration-to_couchbase-capella.html

Video: MongoDB Atlas to Couchbase Capella migration

https://erikhinderer.github.io/couchbase-onboarding-agent/mongodb-atlas_migration-to_couchbase-capella.html

## Migrations Supported
- MongoDB (3.6 - 8.0)
- DynamoDB (SaaS)
- Redis (5.0+)
- Cassandra (2.1 - 4.x)
- Cosmos DB (SaaS)
- Couchbase (CE) (7.2 - 8.0.2)
- Couchbase (EE) (7.2 - 8.0.2)
- Couchbase Capella (SaaS)

## Couchbase Onboarding Agent AI Capabilities

The agent runs on a local LLM (Qwen, served via an Ollama-compatible API in its own container)
rather than a third-party cloud model -- source database credentials, schema samples, and data
never leave the deployment, which matters for a tool that handles production credentials across
eight source systems.

- **Episodic memory for historical reference.** Every significant migration event -- validation
  results, connector-introspection findings, CDC failures, rollback reasons, and user Q&A -- is
  embedded and written to a Couchbase collection. When you ask the assistant something, it first
  recalls similar past events via native Couchbase Vector Search (falling back to a brute-force
  N1QL scan if the vector index isn't ready yet) and grounds its answer in what actually happened
  before, not just the current conversation.
- **Migration strategy guidance.** The wizard's "Destination & Mode" step combines your cutover
  preference (all-at-once vs. phased) with the source's already-introspected topology -- data
  size, container count, and whether it supports continuous change-data-capture at all -- to
  recommend one-time full load, continuous replication, or a bulk-copy-plus-sync hybrid. That
  recommendation is a deterministic, rule-based calculation (not an LLM call) so it isn't exposed
  to model latency or a hallucinated answer on a step that's on the critical path of setting up a
  migration; the conversational assistant can then explain the reasoning behind it, or the
  tradeoffs of overriding it, in plain language.
- **Bottleneck Detection & Auto-throttling.** While a migration is actively running, a monitor
  watches live throughput and batch outcomes for stalled or degraded transfer rates, source-side
  rate-limiting (e.g. DynamoDB's provisioned-throughput errors, MongoDB Atlas connection storms),
  and destination backpressure (elevated Couchbase upsert failures). Source-throttling and
  destination-backpressure findings are auto-remediated by reducing pipeline concurrency live, with
  no restart required; stalled or degraded throughput is surfaced as a diagnosis and suggestion,
  since a concurrency change alone can't fix a dropped connection or a genuine network problem.
- **Real-time advisor at any stage.** The chat panel is available whether a migration is still
  being planned, actively validating, mid-transfer, replicating, or already rolled back -- every
  question is grounded in that specific migration's live phase, stats, and validation report, so
  answers reference what's actually happening rather than generic advice.

## Migration pipeline modes

| Mode | User-facing label | What happens | Terminal state |
|---|---|---|---|
| `full_load` | **One-time migration** | Every included container is extracted and loaded into Couchbase once | `COMPLETE` after transfer + verification |
| `cdc_live` | **Continuous replication** | Change-data-capture starts immediately and stays running | `REPLICATING` (ongoing) until stopped |
| `full_load_and_cdc` | **Bulk copy + continuous sync** | A full load for existing data, then change-data-capture takes over the ongoing delta | `REPLICATING` (ongoing) until stopped |

For a Couchbase source, these three modes map onto Couchbase's own tools instead of the
custom pipeline: `full_load` runs `cbbackupmgr backup` + `restore`, `cdc_live` starts an XDCR
replication directly, and `full_load_and_cdc` runs `cbbackupmgr` first and then starts XDCR for
the ongoing delta -- see "Why Couchbase sources are different" above.

The "Ask the agent" recommendation on the Destination & Mode step
(`backend/app/core/recommendation.py`) is a fast, deterministic rule engine, not a live LLM
call -- a wizard step on the critical path of setting
up a migration shouldn't be exposed to LLM latency or a hallucinated recommendation.

## Quick start

```bash
cp env.example .env
# edit .env: set MEMORY_CB_PASSWORD, and CAPELLA_API_TOKEN/CAPELLA_ORG_ID if you want
# automatic destination bucket provisioning on Capella.
./scripts/setup-corporate-ca.sh
docker compose up --build
```

- UI: http://localhost:5173
- API: http://localhost:8000 (docs at `/docs`)
- Couchbase EE admin console (agent memory): http://localhost:8091
- Qwen / Ollama API: http://localhost:11434

First boot pulls the Qwen model (`qwen3:8b` by default) and initializes the Couchbase
Enterprise Edition memory store -- this can take a few minutes; subsequent starts are fast
(cached in the `ollama_data` / `couchbase_memory_data` volumes).

## Architecture

| Component | Tech | Purpose |
|---|---|---|
| `frontend/` | React + TypeScript + Vite | Dark-mode UI: setup wizard, topology diagram, live stats dashboard, agent chat |
| `backend/` | FastAPI (Python) + five source SDKs (pymongo, boto3, redis, cassandra-driver, azure-cosmos) + the Couchbase SDK (for the destination and for CE/EE/Capella as a source) + `cbbackupmgr`/XDCR for Couchbase-to-Couchbase moves | REST + WebSocket API, validation, extract/transform/load pipeline, CDC |
| `qwen-service/` | Ollama serving Qwen 3, 8B | Local LLM for the in-app assistant and memory embeddings -- nothing leaves the Docker network |
| `couchbase-memory/` | Couchbase Enterprise Edition (developer license) | Agent long-term memory (past validations, decisions, bottleneck findings), recalled via native vector search |
| `scripts/init_memory.py` | Python | One-shot bootstrap: creates the memory bucket/scope/collection and the FTS vector index |

> **`couchbase-memory` is not a migration source or destination.** It's the onboarding
> agent's own memory store. Your actual source database (MongoDB/DynamoDB/Redis/
> Cassandra/Cosmos DB/Couchbase) and destination Couchbase cluster or Capella project are external
> systems, configured per-migration in the wizard -- nothing about them lives in this
> Docker Compose stack. See the README for the Enterprise Free license terms that apply to the `couchbase:enterprise-*` image used here.

## Data Migration Methods

Couchbase-to-Couchbase migrations use Couchbase's own native SDK tools -- [`cbbackupmgr`](#connector-implementation-depth) for a one-time full load, XDCR for continuous replication, or both for hybrid.

MongoDB, Amazon DynamoDB, Redis, Apache Cassandra ad Microsoft Azure Cosmos DB use Connectors on per-document pipeline.


### Connector abstraction

Every source type implements a common `SourceConnector` interface
(`backend/app/core/connectors/base.py`):

- `test_connection()` -- introspects the source and returns a topology snapshot: server
  version/edition, per-container (collection/table/keyspace/container) estimated document
  count and size, sample field names, and whether continuous change-data-capture is
  available right now.
- `extract()` -- a full, batched read of the selected containers, transforming
  source-native types (BSON, DynamoDB's AttributeValue JSON, Cassandra's uuid/decimal/blob,
  ...) into JSON-safe Couchbase documents.
- `stream_changes()` -- yields ongoing change events for continuous replication, using each
  source's own native change-capture mechanism.

`backend/app/core/couchbase_loader.py` is the other half of the pipeline: it writes
`SourceDocument` batches into Couchbase via the Python SDK, one scope/collection per source
container, with `asyncio`-bounded concurrency that `MigrationEngine`'s bottleneck-detection
loop can throttle down live in response to destination backpressure or source rate-limiting
-- the direct analogue of cbbackupmgr `--threads` auto-throttle, just
applied to a worker pool this app owns outright instead of a subprocess it launches.

**Couchbase sources use the Couchbase SDK.** A
Couchbase-to-Couchbase migration routes to `backend/app/core/couchbase_native.py`, which shells out to `cbbackupmgr`
and/or drives XDCR through the source cluster's REST API.

- **It is not read-only against the source.** `cbbackupmgr backup` reads via the source's own
  backup mechanism, but setting up XDCR *does* modify the source cluster's
  replication topology (it creates a remote-cluster reference and a replication on the
  source).
- **There's no per-document `_migration` tag and no document-level rollback.** cbbackupmgr and
  XDCR move data at the bucket/collection level, not document-by-document, so verification is
  destination item-count-based rather than tag-based, and "rollback" for a native migration
  means tearing down the XDCR replication (and, if requested, the destination bucket) rather
  than purging tagged documents.
- **Continuous/hybrid replication (XDCR) is only wired up for a self-managed Enterprise
  Edition source.** Community Edition has no XDCR at all. A Capella source can only do a
  one-time `cbbackupmgr` load today -- XDCR-from-Capella isn't implemented (see
  "Connector implementation depth" below for why).
- **XDCR requires the source and destination buckets to have the same vBucket count --
  see "XDCR requires matching vBucket counts" below.**
- Verified against a live self-managed Enterprise Edition source and a live Capella
  destination on 2026-07-30: `cbbackupmgr backup`/`restore` (one-time full load) completed
  successfully end-to-end. Continuous/hybrid (XDCR) replication itself is still pending a
  clean live run -- the first attempts surfaced the vBucket-count mismatch documented below,
  now fixed.

### XDCR requires matching vBucket counts

This one is easy to hit by surprise and worth calling out on its own: XDCR refuses to
replicate between two buckets with different vBucket counts, and **a bucket's vBucket count
can never be changed after it's created** -- there is no migration path for an
already-created bucket, only drop-and-recreate.

Every self-managed Couchbase Server source this app supports (7.2-8.0.2) uses the
traditional **1024-vBucket** layout. But Couchbase Server 8.0 introduced a newer Magma
storage option that defaults new buckets to **128 vBuckets** instead -- and that's exactly
what you get if you create a Capella (or self-managed 8.0+) bucket through the UI without
explicitly choosing "1024 vBuckets" under its storage-backend settings. Point continuous or
hybrid replication at a 128-vBucket destination bucket from a 1024-vBucket source and XDCR
setup fails outright:

```
Failed to create XDCR replication: 400 {"errors":{"toBucket":"The number of vbuckets in
source cluster, 1024, and target cluster, 128, does not match. This configuration is not
supported."}}
```

Two things guard against this:

- **Auto-provisioning always requests 1024.** `CapellaClient.create_bucket()`
  (`backend/app/core/capella_client.py`) explicitly sets `numVBuckets: 1024` whenever this
  app creates a destination bucket for you, so a bucket it provisions itself is always
  XDCR-compatible with a self-managed source.
- **The validator checks it before approval, not after a wasted backup+restore.** The
  `XDCR_VBUCKET_COMPAT` check (`backend/app/core/validator.py`) runs whenever the strategy is
  continuous/hybrid and the source is a Couchbase Enterprise Edition cluster. It compares the
  source bucket's vBucket count (read via the classic REST bucket-detail endpoint,
  `CouchbaseSourceConnector.get_vbucket_count()`) against the destination bucket's, if that
  bucket already exists (`CouchbaseClusterClient.get_vbucket_count()` -- the same classic
  REST call already used for destination topology, which works against Capella too, not just
  self-managed clusters). A destination bucket that doesn't exist yet is treated as
  informational, not a failure, since auto-provisioning will create it correctly. A mismatch
  on an *already-existing* destination bucket is a hard (red) validation failure that blocks
  approval, with a message telling you to drop and recreate the bucket with a matching
  vBucket count -- catching this at validation time, before approving the migration, avoids
  running a full `cbbackupmgr` backup+restore (which can take minutes) only to have XDCR
  setup fail afterward.

### Source -> Couchbase data modeling

| Source concept | Couchbase document key | Notes |
|---|---|---|
| MongoDB document (`_id`) | `collection::<_id>` | BSON types (ObjectId, Date, Decimal128, Binary) converted to JSON-safe values |
| DynamoDB item (partition[+sort] key) | `table::<pk>[::<sk>]` | Read via the table's real `KeySchema`, not guessed per item |
| Redis key | `redis::<key>` | Grouped into logical "containers" by the segment before the first `:` in the key name; value wrapped as `{redis_type, value, ttl}` |
| Cassandra row (partition+clustering key) | `table::<pk>[:<ck>]` | Row read via `dict_factory`; collection/counter columns preserved as JSON arrays/objects |
| Cosmos DB item (`id` + partition key) | `container::<pk>::<id>` | System properties (`_rid`, `_self`, `_etag`, `_attachments`) stripped; `_ts` kept as `_cosmos_ts` |

Every migrated document from these five sources also gets a `_migration` envelope
(`migration_id`, `source_container`, `migrated_at`) used for verification counts and rollback
purges.

A **Couchbase source doesn't appear in this table** -- cbbackupmgr and XDCR move documents at
the bucket/collection level, unmodified, so there's no key remapping or `_migration` envelope
to speak of; verification is a destination item-count comparison instead.

### Connector implementation depth

**MongoDB is the reference-depth connector** -- the pattern DynamoDB, Redis, Cassandra, and
Cosmos DB follow (Couchbase is the one source that doesn't follow this pattern at all --
see below):

- Full introspection (server version, replica-set detection, per-collection `collStats`,
  sample fields).
- Full batched extraction via `find()`.
- Continuous sync via native MongoDB **Change Streams** (resumable, one watcher thread per
  collection, running concurrently).

Those four are **working, but intentionally lighter on edge-case hardening**:

- **Amazon DynamoDB** -- introspection via `describe_table`; extraction via paginated
  `Scan`; continuous sync via **DynamoDB Streams**. Does not follow shard splits/merges --
  a shard that closes mid-run stops producing events until the migration restarts (at which
  point the current shard set is picked up fresh). A production deployment against a
  high-throughput table would want the Kinesis Client Library-style shard-tree-following
  logic AWS's own DynamoDB Streams Kinesis Adapter provides.
- **Redis** -- keys grouped into logical containers by their `prefix:` naming convention;
  full extraction via `SCAN` with type-aware reads (`GET`/`HGETALL`/`LRANGE`/`SMEMBERS`/
  `ZRANGE`/`XRANGE`); continuous sync via **keyspace notifications** (pub/sub). This is
  Redis's only built-in change-notification mechanism short of speaking the replication
  protocol as a replica, and it is **not durable** -- pub/sub has no backlog, so any event
  published while this app isn't actively subscribed (restart, network blip) is lost, not
  merely delayed. There is no resumable checkpoint for this connector for that reason.
- **Apache Cassandra** -- introspection via driver metadata plus `system.size_estimates`
  (Cassandra's own approximate, per-node partition-count table); full extraction via a paged
  `SELECT *`; continuous sync is **polling-based**, not log-based: Cassandra's real CDC
  feature writes raw commit-log segments to a `cdc_raw` directory on each node's local
  filesystem, which requires an agent co-located with every node -- not something this
  centrally-running app has access to. Instead, this connector re-scans each table on an
  interval and uses `WRITETIME(<column>)` to find rows written since the last poll. Trade-
  offs: every poll re-scans the whole table (no server-side "changed since" filter exists),
  and **deletes are not detected at all** -- re-run a full load to reconcile deletes.
- **Microsoft Azure Cosmos DB** -- closest to reference depth. Its change feed is native,
  durable, and resumable via continuation tokens by default on every container, so
  continuous sync here doesn't carry Redis's or Cassandra's caveats. The one limitation is
  inherent to Cosmos DB itself: the default "Latest Version" change feed mode **does not
  surface deletes** (Cosmos does offer a newer "All Versions and Deletes" mode on some API
  versions, intentionally not used here to keep one code path working across accounts).
- **Couchbase Server (Community Edition, Enterprise Edition, or Capella)** -- the one source
  that **doesn't** follow the custom connector pattern above. `backend/app/core/connectors/couchbase_source.py`
  only handles introspection: server/cluster version and edition, and per-bucket
  scope/collection listing via the SDK's `collections().get_all_scopes()` (works uniformly
  across all three variants). Its `extract()` is intentionally dead code -- it raises
  immediately, because `MigrationEngine` routes any Couchbase source straight to
  `backend/app/core/couchbase_native.py` instead (`cbbackupmgr` for one-time loads, XDCR for
  continuous replication; see "Why Couchbase sources are different" above). `supports_cdc` is
  `True` only for a self-managed **Enterprise Edition** source, since that's the only variant
  XDCR is wired up for here:
  - **Community Edition** -- one-time `cbbackupmgr` load only; CE has no XDCR at all.
  - **Enterprise Edition** (self-managed) -- one-time `cbbackupmgr` load, continuous XDCR, or
    both.
  - **Capella** -- one-time `cbbackupmgr` load only today. XDCR-from-Capella isn't
    implemented: `couchbase_native.py`'s `XdcrManager` drives the classic cluster-manager REST
    API (`/pools/default/remoteClusters`, `/controller/createReplication`), which Capella
    likely doesn't expose to external callers the same way a self-managed cluster does --
    mirroring why the destination side already needs a separate `CapellaClient`. This is a
    known, documented gap, not a silent assumption.

### Bottleneck detection & auto-throttle

`backend/app/core/bottleneck_detector.py` watches the extract/load pipeline for stalled or
degraded throughput, source rate-limiting errors, and Couchbase write backpressure (elevated
upsert failure rates). Because this pipeline is an `asyncio` worker pool this app owns
outright, **both** resource-
pressure bottlenecks *and* thread-count lever have a direct analogue
here: `CouchbaseLoader`'s concurrency can be reduced live, without restarting anything, in
response to `SOURCE_THROTTLED` or `DEST_BACKPRESSURE` findings. Stalled/degraded throughput
findings stay diagnosis-and-suggestion only in the Ask The Agent panel, same rationale: a concurrency change doesn't fix a dead connection or a real network problem.

## Wizard flow

1. **Source** -- pick a source type, fill in its connection fields (only the relevant ones
   are shown), and click **Test & introspect source**. On success you'll see a `Connected ·
   N containers · <version>` badge with the detected containers, estimated counts, and
   whether continuous sync is currently available.
2. **Destination & Mode** -- Couchbase connection details (check **This endpoint is a
   Couchbase Capella cluster** to force TLS and reveal the optional project/cluster ID
   fields for bucket auto-provisioning), the destination bucket name, an **Ask the agent**
   card that recommends a replication mode from a cutover-vs-phased question, the
   replication mode selector itself (continuous modes are disabled if the source doesn't
   currently support change-data-capture), per-container include/exclude checkboxes, and a
   concurrency setting. **Create & validate** creates the migration record and immediately
   runs validation.
3. **Validate** -- source connectivity/edition, destination connectivity/capacity, CDC
   availability for the chosen mode, container-name-sanitization collisions, an average
   document-size sanity check against Couchbase's 20 MiB limit, network latency, TLS
   configuration, and (for continuous/hybrid Couchbase-source migrations only) an XDCR
   vBucket-count compatibility check between source and destination -- see "XDCR requires
   matching vBucket counts" above. Failed (red) checks block **Continue**; warnings (yellow)
   don't.
4. **Review & Approve** -- a summary card, an approver name field, and **Approve & view
   migration**, which takes you to the migration's detail page.
5. **Start** -- on the detail page, once the migration is `approved`, click **Start
   migration** (labeled **Start replication** for the two continuous modes). Live
   throughput/mutations-per-second/error-rate stream over the websocket onto the same
   topology diagram used throughout the wizard. One-time migrations run to `complete` on
   their own; continuous modes settle into `replicating` and stay there until you **Cutover
   & complete** or **Stop replication** from this same page.

## Configuration notes

- **Swapping the LLM**: point `QWEN_BASE_URL` at any Ollama-compatible server; the backend
  only calls `/api/chat` and `/api/embeddings`.
- **Scaling beyond one API replica**: `MigrationStore` (`backend/app/core/store.py`)
  persists to a JSON file for simplicity. Swap it for a
  Couchbase collection or Postgres table if you need multiple backend replicas.
- **Capella reachability**: Capella requires the backend container's egress IP to be
  allow-listed on the destination cluster (Capella project -> Allowed IPs) and connections
  over `couchbases://`.
- **Source reachability**: each source database needs to accept connections from this
  agent's egress IP -- security group / firewall / IP allow-list rules per source type, the
  same operational requirement documents for Couchbase source clusters.
- **Container-name collisions**: source container names are sanitized into valid Couchbase
  scope/collection names (`sanitize_couchbase_name` in `backend/app/core/couchbase_client.py`);
  the validator's `NAMING_COMPAT` check flags any two source containers that would collide
  after sanitization, and the wizard lets you set an explicit target scope/collection name
  per container to resolve it.
- **Cassandra CDC poll interval / DynamoDB scan page size / etc.**: tunable via environment
  variables in `backend/app/config.py` (`CASSANDRA_CDC_POLL_INTERVAL_S`,
  `DYNAMODB_SCAN_PAGE_SIZE`, `REDIS_SCAN_COUNT`, `COSMOSDB_CHANGE_FEED_POLL_INTERVAL_S`, ...).
- **Couchbase-source migrations**: `CBBACKUPMGR_PATH` (defaults to `cbbackupmgr` on `$PATH` --
  the backend image's multi-stage build copies the binary out of the official
  `couchbase:enterprise-*` image), `COUCHBASE_BACKUP_ARCHIVE_DIR` (defaults to
  `/data/cbbackupmgr-archives`, backed by the `cbbackupmgr_archives` Docker volume), and
  `XDCR_POLL_INTERVAL_S` (how often the backend polls `/pools/default/tasks` for XDCR
  progress).

### Restarting the backend mid-migration

`MigrationStore` (`backend/app/core/store.py`) persists migration records to a JSON file
that survives a backend restart, but the `asyncio` background task actually *driving* a
migration (the full load, the XDCR-progress poll loop, a rollback) does not -- restarting
the backend (a code change, a crash, `docker compose up --build` while something is running)
kills that task, leaving the record frozen at whatever phase was last saved (`migrating`,
`replicating`, `verifying`, `rolling_back`, `validating`) even though nothing is actually
progressing it anymore.

On startup, `_reconcile_orphaned_migrations()` (`backend/app/main.py`) scans for exactly this
and marks any such record `failed` instead of leaving it stuck showing stale-but-real-looking
progress. **For a Couchbase-source (XDCR) migration that was `replicating`, this is
informational, not a cleanup**: XDCR itself runs on the *source* cluster, entirely
independent of this app's process lifecycle, so restarting the backend does **not** stop it --
the replication (and its remote-cluster reference) may well still be live on the source
cluster afterward. Reconciliation intentionally does not try to tear that down automatically
(it can't tell whether you wanted it stopped); check the source cluster's XDCR admin UI/REST
API yourself and remove it manually if it's still running. This exact scenario is what caused
a `cannot find remote cluster` error on a *subsequent* migration attempt on 2026-07-30, before
this reconciliation step existed.

### `cannot find remote cluster` on a brand-new XDCR migration

A `cannot find remote cluster` failure can happen on a **fresh** migration too, with no orphaned
app state involved at all -- confirmed live on 2026-07-30 against a guaranteed-clean state (a full
`docker compose down -v` before the run) still hitting this error on the very first attempt.

The real cause: `XdcrManager` (`backend/app/core/couchbase_native.py`) names each migration's
remote-cluster reference uniquely (`onboarding-agent-<migration id>`), but Couchbase's
`POST /pools/default/remoteClusters` refuses to register a *second* reference pointing at a
destination host that's already registered under a *different* name -- and the 400 it returns for
that case also happens to contain the substring "already exists," which the code used to treat as
"nothing to do, a prior attempt already registered this." In practice that meant: once one
migration to a given destination succeeded (or even just got as far as registering its reference),
every later migration to that *same* destination would generate a new unique name, silently fail
to register it (masked by the "already exists" short-circuit), and then fail on
`createReplication()` because the name it was actually looking for was never created -- only the
old one was. Checking the source cluster's own XDCR admin page (Couchbase Web Console ->
XDCR) confirms this directly: exactly one lingering `onboarding-agent-*` reference from an earlier
run, under a name the current migration was never going to find. This also explains why wiping
this app's own state (`docker compose down -v`) never fixed it -- the stale reference lives on the
*source cluster*, entirely outside this app's control.

`create_remote_cluster_ref()` now tells the two cases apart: on a 400 "already exists," it checks
what's actually registered. If the exact intended name is already there, it's a genuine no-op. If
a *different* `onboarding-agent-*` reference is squatting on the same destination host instead, it
removes that stale reference and retries registration once -- self-healing, and scoped only to
references this app's own naming scheme created (never a reference you registered by hand).
`_start_xdcr()` (`backend/app/core/migration_engine.py`) separately also retries
`create_replication()` itself a few times with a short delay as a safety net for the rarer case of
a genuine cluster-side config-propagation lag. Any other kind of failure (bad credentials,
unreachable host, etc.) still raises immediately without retrying.

If you hit this on a version of the app from before this fix, delete the stale reference by hand:
Couchbase Web Console (on the *source* cluster) -> XDCR -> click the lingering
`onboarding-agent-*` entry under Remote Clusters -> Delete.

### Troubleshooting a build failure behind a corporate proxy

See the README's "Troubleshooting a build failure behind
a corporate proxy" section. In short:

```bash
./scripts/setup-corporate-ca.sh
docker compose build --no-cache
docker compose up
```

### Troubleshooting first boot

`couchbase-memory` can legitimately take a couple of minutes to come up on a cold boot. If a
*previous* `docker compose up` was interrupted partway through, it can be left in a
partially-initialized state that never becomes healthy on restart:

```bash
docker compose down -v   # wipes the named volumes
docker compose up --build
```

## Development

```bash
# Backend
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload

# Frontend
cd frontend && npm install
npm run dev
```

Backend Python is standard `ast`/mypy-friendly style; frontend is TypeScript strict-mode
(`npm run build` runs `tsc -b && vite build`).

## Adding a new source

1. Implement `SourceConnector` in `backend/app/core/connectors/<name>.py` (see
   `mongodb.py` for the reference-depth pattern, or `redis_connector.py`/
   `cassandra_connector.py` for a lighter one built around a source with a non-obvious or
   non-durable change-capture story).
2. Register it in `backend/app/core/connectors/registry.py`.
3. Add the enum value to `SourceType` (`backend/app/models/enums.py`) and its connection
   fields to `SourceConnectionConfig` (`backend/app/models/schemas.py`).
4. Add its field list to `/api/source-types` (`backend/app/main.py`) and a form section to
   `SourceConfigForm.tsx` (`frontend/src/components/wizard/`).
5. Add the SDK to `backend/requirements.txt`.

No other backend code needs to change -- `MigrationEngine`, `MigrationValidator`,
`CouchbaseLoader`, and the API routes are all written against the `SourceConnector`
interface, not any specific source.

**Exception:** a new *Couchbase* source variant (there are three today -- Community,
Enterprise, Capella, all sharing one `CouchbaseSourceConnector`) additionally needs a case in
`MigrationEngine.run_migration()`'s `COUCHBASE_SOURCE_TYPES` check
(`backend/app/models/enums.py`) and, if it should support continuous replication, wiring in
`backend/app/core/couchbase_native.py`'s `XdcrManager` -- it does not go through the custom
`SourceConnector.extract()`/`stream_changes()` path at all. See "Why Couchbase sources are
different" above before adding one.
