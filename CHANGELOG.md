# Changelog

## Unreleased

### Added
- **GPU auto-detection for the local LLM.** `./scripts/compose.sh` wraps `docker compose` and
  picks the right override for the machine it runs on: NVIDIA (`docker-compose.gpu.yml`), AMD
  ROCm (new `docker-compose.rocm.yml`, ROCm Ollama image via `OLLAMA_IMAGE_TAG` build arg) or,
  on Apple Silicon, an Ollama running on the Mac itself (new `docker-compose.host-llm.yml`,
  Metal) -- CPU otherwise. `LLM_COMPUTE=auto|nvidia|rocm|host|cpu` forces a mode. `qwen-service`
  now logs `==> LLM compute: GPU|CPU` (with device name/memory) at startup, and once a model is
  loaded `/api/agent/status` reports `compute` (`gpu`/`cpu`/`partial`) from Ollama's `/api/ps`,
  shown as a tag on the sidebar's assistant status.

## 1.0.0 - 2026-11-07

First tagged release of the Couchbase Labs Onboarding Agent.

### Added
- **Multi-target destination mapping.** Each source container maps to one or more
  `bucket.scope.collection` targets (`ContainerMigrationSpec.targets`); the first is the
  primary copy, extra targets receive the same writes. Buckets named anywhere in the plan
  are provisioned if missing; verification, rollback, cbbackupmgr restore and XDCR work
  per target bucket. New mapping editor in the wizard's Destination step.
- **Bi-directional replication with failback.** After falling back to the source, forward
  sync resumes from the change position recorded at fall-back time ("Return to
  Couchbase"), with no new bulk copy; another cutover completes the round trip
  (`POST /api/migrations/{id}/failback`, `cutover_history`, `failbacks`).
- **Reverse XDCR** fallback for Couchbase Enterprise sources with a self-managed
  destination.
- **Batched reverse writes** per source (`apply_reverse_batch`): MongoDB `bulk_write`,
  DynamoDB `BatchWriteItem`, Redis pipelines, Cassandra `execute_concurrent`, Couchbase
  `upsert_multi`/`remove_multi`; the forward loader uses `upsert_multi`/`remove_multi` too.
- **Security hardening** (see `SECURITY_HARDENING.md`): per-IP auth throttling with an
  audit log, websocket token as a subprotocol instead of a query string, security
  headers on the API and UI, CORS and API docs off outside development, bucket-name
  validation, startup warnings for weak defaults, `no-new-privileges` and dropped
  capabilities in Compose. `SECURITY.md` reporting policy.

### Changed
- Product name in the UI is now **Couchbase Labs Onboarding Agent**.
- **Optional local LLM.** `COMPOSE_PROFILES=` + `LLM_ENABLED=false` (or Helm `llm.enabled: false`)
  runs the whole stack without the Ollama container: no model download, chat off with a clear
  message, bottleneck alerts still delivered, memory recall by recency. Nothing on a migration's
  path depends on the model.
- **Smaller default models.** The assistant now runs on `qwen3:4b` (was `qwen3:8b`) and agent
  memory embeds with a dedicated `nomic-embed-text` model (768 dims; was the 8B chat model's
  4096-dim hidden state): ~2.9 GB of downloads instead of ~5.2 GB and about half the RAM. The
  memory-init job recreates the vector index when the embedding length changes
  (`MEMORY_EMBEDDING_DIMS`); memories embedded by an earlier version aren't recalled until
  re-embedded or cleared (see README "Changing the model").
- Rollback state records keyspaces as `bucket.scope.collection`; records from earlier
  builds are upgraded on read.
- Fallback sync pages destination keys and only advances its watermark past applied
  changes; rejected writes are retried, then recorded as failed events.

### Known gaps
- Single shared API token (no per-operator identity).
- Reverse XDCR, DynamoDB `BatchWriteItem` and Cassandra `execute_concurrent` paths are
  covered by tests against fakes but have not yet been exercised against live services.
