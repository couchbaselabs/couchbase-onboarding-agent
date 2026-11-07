# Changelog

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
- Rollback state records keyspaces as `bucket.scope.collection`; records from earlier
  builds are upgraded on read.
- Fallback sync pages destination keys and only advances its watermark past applied
  changes; rejected writes are retried, then recorded as failed events.

### Known gaps
- Single shared API token (no per-operator identity).
- Reverse XDCR, DynamoDB `BatchWriteItem` and Cassandra `execute_concurrent` paths are
  covered by tests against fakes but have not yet been exercised against live services.
