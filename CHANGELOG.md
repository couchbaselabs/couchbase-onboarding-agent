# Changelog

## Unreleased

### Added
- **HTTPS by default for the UI.** The frontend's nginx now terminates TLS itself (8443) and
  redirects plain HTTP (8080) to it, except when a reverse proxy / Ingress already terminated TLS
  (`X-Forwarded-Proto: https`) and for `/healthz`. With no certificate mounted at
  `/etc/nginx/tls/server.{crt,key}` it generates a self-signed one at start (SANs from
  `TLS_HOSTNAME`). Compose publishes `https://localhost:5173` (`5174` redirects) and keeps the
  generated cert in the `frontend_tls` volume; `certs/tls/` + a bind mount takes your own.
  The Helm chart (1.1.0) adds `frontend.tls.{enabled,existingSecret,hostnames,ipAddresses,days}`
  (on by default, generated cert kept in `<release>-frontend-tls`), `frontend.service.port` now
  defaults to 443 with plain HTTP on `frontend.service.httpPort` (8080) for Ingress backends.
  `TLS_ENABLED=false` restores the previous plain-HTTP behaviour.
- **RBAC: users and roles (Settings → RBAC & Roles).** Multiple local users, each with one role.
  Built-in roles `admin`, `operator` (runs migrations end to end, no settings/users) and `read_only`
  (watch + ask the assistant); custom roles from the permission catalog (`migrations:read`,
  `migrations:write`, `migrations:rollback`, `migrations:delete`, `sources:test`, `agent:chat`,
  `settings:write`, `users:manage`). Every API route is classified (`core/rbac.py`) and enforced
  centrally -- unknown routes are denied to non-admins by default -- and `403` is distinct from
  `401` (not throttled, no sign-out). Login takes a username (`admin` when omitted, so existing
  clients keep working); `/api/auth/users`, `/api/auth/roles`, `/api/auth/permissions` manage it.
  Guards: the last active administrator can't be demoted, disabled or deleted; nobody can disable or
  delete themselves; disabling, deleting, demoting or resetting a user's password signs them out.
  `auth.json` v1 (single password) is migrated in place into the `admin` user. The static
  `API_AUTH_TOKEN` acts as `admin`. The UI hides New migration from roles without
  `migrations:write` and shows the signed-in user and role in the sidebar; Settings is now tabbed.
- **LDAP / Active Directory sign-in (Settings → LDAP).** `ldap3`-based: ldaps:// or ldap://+StartTLS
  (verified against the public CAs plus the trust store), service-account or anonymous bind, user
  filter with `{username}` (escaped), groups from `memberOf` or a group-search fallback, and
  **group → role mappings** (first match wins; optional default role; otherwise denied). Directory
  users get a session with the mapped role and never become local users; local accounts still
  log in locally first. Presets for Active Directory and OpenLDAP; a Test button shows the bind
  result and a user's groups/role before saving. Bind password stored Fernet-encrypted.
- **Certificates (Settings → Certificates).** *Trusted CAs*: upload CA PEMs (leafs refused) that are
  merged with the system bundle into `<state>/certs/ca-bundle.pem` and become the default
  verification for requests, the Couchbase SDK, httpx (`SSL_CERT_FILE`) and LDAP -- a per-connection
  CA in the wizard still wins. *UI certificate*: upload a server certificate (+ chain, any order) and
  its key; the backend validates the pair and publishes it to the frontend -- a shared volume in
  Compose (`TLS_PUBLISH_DIR`, `frontend_tls` mounted at `/data/tls`) or the TLS Secret on Kubernetes
  (`TLS_PUBLISH_SECRET` via a ServiceAccount/Role scoped to that Secret, chart
  `frontend.tls.managedByApp`, default on). The frontend container watches its cert files and
  reloads nginx, so no restart is needed.
- **Log forwarding (Settings → Log forwarding).** A root-logger handler batches the application log
  and the `onboarding_agent.auth` audit trail to one destination: Splunk HEC, Datadog Logs v2,
  Elasticsearch/OpenSearch bulk, Grafana Loki push, or RFC 5424 syslog over UDP/TCP/TLS (octet
  counting). Minimum level, include app/audit, batch size and flush interval, extra static fields;
  bounded queue (oldest dropped), one retry per failed batch, never blocks requests. Test-event
  endpoint and delivery counters (`sent/failed/dropped/queued`, last error). Secrets encrypted.
- **Account row with Log out in the sidebar.** The bottom-left of the navigation now shows who the
  tab is signed in as (`admin` for a password session, `API token` otherwise) with a shortcut to the
  password settings and a **Log out** button that revokes the session, forgets the tab's token and
  returns to the login page (`frontend/src/components/nav/SessionRow.tsx`,
  `hooks/useAuthSession.ts`). Settings' sign-out button now shares that logic. Hidden when the
  deployment runs without authentication.
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
