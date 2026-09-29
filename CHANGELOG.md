# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [1.1.0] - Unreleased

### Added
- Gap-free bulk copy + continuous sync for every source: the change position is recorded before the bulk
  copy and changes made during the copy are replayed.
- Continuous sync for Couchbase Community Edition and Capella sources (CAS polling).
- DynamoDB Streams shard-lineage following; Redis changed-key tracking and re-scan after restarts; Cosmos DB
  and Cassandra start positions.
- Micro-batched change application with per-event retries, failed-write tracking, automatic stream recovery,
  checkpoint persistence and resume (manual and after restarts).
- Cutover that drains the stream and verifies per-container counts.
- Delete reconciliation with safety limits.
- Rollback with a dry-run plan: drops collections the migration created, removes tagged documents, restores
  overwritten documents from a pre-image journal; resumable; confirmation required after cutover.
- Post-cutover fallback sync (Couchbase back to the source) and "fall back to source".
- Light and dark mode (follows the OS by default).
- Optional API bearer token; credentials masked in responses and encrypted at rest; TLS verification on by
  default with pasted CA certificates.
- Helm chart, non-root images, same-origin UI proxy, CI, tests.

### Changed
- License changed to MIT for open-source release.
- Docker Compose binds every port to localhost and requires `MEMORY_CB_PASSWORD`.

### Fixed
- Writes made at the source during a bulk copy were missed by "bulk copy + continuous sync".
- Rollback could delete destination documents that existed before the migration and had the same key.
- Source and destination credentials were returned by the API and websocket and stored in plain text.
- TLS certificate verification was disabled for management REST calls and `cbbackupmgr`.

## [1.0.0]

- Initial release.
