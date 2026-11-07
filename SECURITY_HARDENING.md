# Security Hardening

This document tracks the security hardening applied to the Couchbase Labs
Onboarding Agent's own code and configuration - the API/migration engine
(`backend`), the dashboard (`frontend`, nginx), the Docker Compose stack and
the Helm chart - and maps each control to the control families in CIS
Benchmarks, NIST SP 800-53 / SP 800-123 / SP 800-190, DISA STIG control
objectives, and PCI DSS v4.0 that it addresses. It follows the same structure
as the Couchbase Agent Operations Manager's `SECURITY_HARDENING.md`, so the two
appliances can be reviewed side by side.

One thing to keep in view throughout: this appliance exists to hold live
credentials for production databases (five source types plus the destination
Couchbase cluster) and to read, write and delete their data. Its own attack
surface is deliberately small - one bearer token, one UI, no multi-user
accounts - and most of the controls below are about making sure that surface
stays small and that the credentials it holds can't leak through it.

## Summary

| Area | Control | Standards touched |
|---|---|---|
| API authentication | Single bearer token (`API_AUTH_TOKEN`), constant-time compare; token required on every `/api/*` route and both websocket endpoints | NIST IA-2/IA-5, PCI 4.0 §8.3 |
| Auth throttling | Per-source-IP lockout: 10 failures in 15 min -> 30 min lockout, `429` + `Retry-After`, before the token is compared | PCI 4.0 §8.3.4, NIST AC-7 |
| Auth audit logging | Every rejected token, missing token and lockout logged with the source IP (`onboarding_agent.auth`) | PCI 4.0 §10.2, NIST AU-2/AU-3 |
| Websocket token transport | Token sent as the `coa-auth` websocket subprotocol (a header), not `?token=`; `/ws/` access logging off in nginx for legacy clients | NIST SC-8/AU-9, PCI 4.0 §8.6.2 |
| HTTP security headers | CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-*`, `Cache-Control: no-store` on the API; HSTS when the request arrived over HTTPS; on both the API and the dashboard | CIS, PCI 4.0 §6.2, NIST SC-23/CM-6 |
| CORS | Deny-by-default outside development; explicit `CORS_ORIGINS` allowlist; the UI is same-origin via nginx | PCI 4.0 §6.2.4, NIST SC-23 |
| API docs exposure | `/docs`, `/redoc`, `/openapi.json` off outside development unless `ENABLE_API_DOCS=true` | CIS, NIST CM-7 |
| Credentials at rest | Source/destination secrets Fernet-encrypted in the state file (`STATE_ENCRYPTION_KEY`), masked in every API response, websocket frame and log line | PCI 4.0 §3.5/§8.3.2, NIST SC-28/IA-5 |
| Default-credential warnings | Startup warnings for a missing or short API token, the default memory-store password, an unset state-encryption key, docs or CORS left on in production | PCI 4.0 §8.3.1, CIS, NIST CM-6 |
| Query/identifier safety | N1QL values always bound as named parameters; every operator-typed bucket name validated against Couchbase's name rule, scope/collection names sanitized or rejected | PCI 4.0 §6.2.4, NIST SI-10 |
| TLS to data stores | Certificate verification on by default for every source, the destination, `cbbackupmgr` and the management REST API; pasted CA support; skip-verify flagged by validation | PCI 4.0 §4.2.1, NIST SC-8 |
| Non-root containers | Backend runs as uid 10001 (drops privileges in `entrypoint.sh`), UI runs `nginx-unprivileged` on 8080; Helm sets `runAsNonRoot`, `seccompProfile: RuntimeDefault` | CIS Docker §4, NIST SP 800-190 / AC-6 |
| Dropped Linux capabilities | `cap_drop: [ALL]` on backend, frontend and the memory-init job; backend adds back only the five needed to fix volume ownership before dropping root; Helm drops all | CIS Docker §5, NIST AC-6 |
| `no-new-privileges` | Set on every Compose service; `allowPrivilegeEscalation: false` in Helm | CIS Docker §5 |
| Network exposure | Every published Compose port (UI 5173, API 8000, Couchbase 8091-8096/11210, Ollama 11434) binds to `127.0.0.1`; Helm exposes only the UI Service/Ingress | PCI 4.0 §1, NIST SC-7, CIS |
| Pinned, used dependencies | Every Python dependency pinned with `==`; `npm ci` from a lockfile | PCI 4.0 §6.3, NIST SI-2/CM-2 |
| Secrets hygiene | `.env`, `*.pem`, `*.key`, `*.crt` git-ignored; `env.example` ships no real values; Compose refuses to start without `MEMORY_CB_PASSWORD` | PCI 4.0 §8.3.1, NIST IA-5 |

Detailed rationale for each follows. Sections are organized by layer:
application, data protection, containers/infrastructure, network, and
cryptography/transport. A final section lists what was reviewed and found
already compliant, and what remains a known, documented gap.

## Application-layer hardening

### Single-token API authentication

`backend/app/api/security.py` guards every router under `/api/*` and both
websocket endpoints with one shared bearer token (`API_AUTH_TOKEN`). The
comparison is `hmac.compare_digest`, so a wrong token takes the same time to
reject as a nearly-right one. `/api/health` (liveness probes) and `/api/config`
(tells the UI whether to ask for a token; returns nothing else) are the only
unauthenticated routes.

Leaving the token unset is a supported mode for a single-user laptop run and
nothing else; the Compose stack makes that safe by binding every port to
`127.0.0.1` (see Network exposure). The backend warns at startup when the
token is unset, and now also when it is shorter than 32 characters - a short
token in front of a service that can hand back decrypted production database
credentials is a weak link worth being loud about (PCI DSS v4.0 8.3.6 sets 12
characters as the floor for *passwords*; a machine-generated bearer token has
no reason to be anywhere near that short, so 32 random characters - `openssl
rand -hex 32` - is the floor the warning enforces).

There is deliberately no multi-user account system, no password store and no
session cookie: every browser call carries the token in an `Authorization`
header the UI adds from `sessionStorage` (per tab, cleared when the tab
closes). That shape is what makes the CORS and CSRF story below simple - a
browser never attaches a bearer header automatically the way it attaches a
cookie, so a cross-site page can't ride an operator's session.

### Brute-force throttling

Previously there was no limit on how many wrong tokens a caller could try.
`security.py` now keeps a per-source-IP failure window: 10 failures within
15 minutes lock that IP out for 30 minutes, during which every request -
including one with the *right* token - gets `429 Too Many Requests` with a
`Retry-After` header and never reaches the comparison. The thresholds follow
PCI DSS v4.0 8.3.4 ("not more than 10 attempts ... minimum lockout of 30
minutes"); they are configurable (`AUTH_MAX_FAILURES`,
`AUTH_FAILURE_WINDOW_S`, `AUTH_LOCKOUT_S`). The websocket endpoints go through
the same check and close with application code `4429` when locked out (the UI
treats `4401` and `4429` alike: prompt for the token again).

The client IP is read from the first hop of `X-Forwarded-For`, which the
bundled nginx and the Helm ingress set, with a fallback to the socket address.
That trusts exactly one proxy, which matches the shipped topologies. If the
API is ever exposed directly (port 8000 published to a network, no proxy),
set `TRUST_X_FORWARDED_FOR=false`, otherwise a caller could spoof its own
address and escape the lockout - `tests/test_hardening.py` covers both modes.

**Known limitation:** the throttle state is in-memory and per-process. It
resets on restart and isn't shared across replicas; the Helm chart runs a
single backend replica (the migration engine itself is single-instance), so
this matches the architecture.

### Authentication audit logging

Every rejected request - missing token, invalid token, and every lockout
trigger or locked-out attempt - is written to the `onboarding_agent.auth`
logger with the method, path and source IP (PCI DSS v4.0 10.2.1/10.2.4, NIST
AU-2/AU-3). The token itself is never logged. Successful calls are not logged
individually (uvicorn's access log already records them); the migration log
(`log_tail`) records who approved each migration and every rollback, cutover,
fallback and failback, which is the operational audit trail for the data
movements themselves.

### Websocket token transport

The UI used to append `?token=<token>` to the websocket URL - the only way to
"send a header" on a browser `WebSocket` without a subprotocol. A token in a
URL ends up in nginx's access log, in the browser's history and in any proxy
between the two. The UI now sends it as a websocket **subprotocol**
(`Sec-WebSocket-Protocol: coa-auth, <token>`), which is a request header; the
backend negotiates `coa-auth` back and never echoes the token. `?token=` is
still accepted for older clients, and nginx's `/ws/` location has
`access_log off` so a legacy client's token can't land in the log either.

### HTTP security headers

A `security_headers` middleware in `backend/app/main.py` sets on every API
response:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()`
- `Cross-Origin-Resource-Policy: same-origin`
- `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` (the API only ever returns JSON; skipped on the Swagger/ReDoc paths, which load their assets from a CDN)
- `Cache-Control: no-store` on `/api/*` (responses carry redacted but still sensitive migration state - hosts, bucket names, topology)
- `Strict-Transport-Security: max-age=31536000; includeSubDomains`, only when the request actually arrived over HTTPS (`request.url.scheme` or the first `X-Forwarded-Proto` hop). The bundled stack terminates TLS at an ingress or reverse proxy, if at all; HSTS over plain HTTP is meaningless.

The dashboard (`frontend/nginx.conf.template`) already sent `nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a CSP with
`script-src 'self'` and no `'unsafe-inline'` for scripts (its one inline
theme script was extracted to `public/theme-init.js` in an earlier pass, the
same change AOM made). This pass adds `Permissions-Policy`,
`Cross-Origin-Opener-Policy: same-origin`, `object-src 'none'`, `base-uri
'self'`, `form-action 'self'` and `font-src` to that CSP, and the same
conditional HSTS (via `$http_x_forwarded_proto`, repeated in every location
that has its own `add_header` lines, since nginx doesn't inherit them).
`style-src 'self' 'unsafe-inline'` remains: the React app sets inline
`style={{...}}` props throughout, so removing it is a re-architecture, not a
config change - a deliberate, scoped exception.

### CORS

`main.py` registered `CORSMiddleware` with a fixed allowlist of the Vite dev
server's origins (`localhost:5173`/`3000`) and `allow_credentials=True`. It was
never a wildcard, but the dev origins were applied in every environment.
`Settings.cors_origin_list` now returns an empty list - cross-origin access
off entirely - unless `ENVIRONMENT=development` or `CORS_ORIGINS` is set
explicitly. Nothing in the bundled stack needs it: nginx proxies `/api` and
`/ws` under the UI's own origin, and the Vite dev server proxies them the
same way. The startup warnings flag a non-empty allowlist outside development.

### Interactive API docs

FastAPI's `/docs`, `/redoc` and `/openapi.json` were served unauthenticated in
every environment. They reveal the full request/response schema - including
every connection-string and credential field name - to anyone who can reach
the port. They're now served only when `ENVIRONMENT=development` or
`ENABLE_API_DOCS=true` (NIST CM-7, "least functionality").

### Query and identifier safety

Every N1QL statement the loader, reconciler and fallback sync run binds caller
values with `QueryOptions(named_parameters=...)`; the only interpolated parts
are bucket, scope and collection names inside backticks. Those names now come
from operators too (the multi-target mapping lets you type a bucket, scope and
collection per container), so they are validated at the API boundary:

- bucket names (`TargetKeyspace.bucket`, `MigrationPlanCreate.destination_bucket`) must match Couchbase's own rule, `^[A-Za-z0-9._%-]{1,100}$` - a backtick or any other N1QL syntax is rejected with a 422 before anything is stored;
- explicit scope/collection names must be at most 251 characters and contain no backtick, and derived names go through `sanitize_couchbase_name()` as before.

`tests/test_hardening.py::test_bucket_names_are_validated` covers the
rejection cases.

### Default-credential and weak-configuration warnings

`startup_security_warnings()` in `main.py` runs at boot and logs a warning
for each of: `API_AUTH_TOKEN` unset, or shorter than 32 characters;
`MEMORY_CB_PASSWORD` left at a built-in/demo value; `STATE_ENCRYPTION_KEY`
unset outside development (the key then lives in the state volume, so a
backup of that volume can decrypt every stored credential); API docs enabled
outside development; CORS origins configured outside development. These are
warnings, not hard failures, so a single-user evaluation run keeps working
(PCI DSS v4.0 8.3.1, NIST CM-6).

## Data protection

### Credentials at rest, in transit to the browser, and in logs

- **At rest:** every field in `SECRET_FIELDS` (passwords, AWS secret/session keys, Cosmos keys, pasted CA PEMs) is Fernet-encrypted inside the JSON state file (`core/secrets.py`, `enc:v1:` prefix). The key comes from `STATE_ENCRYPTION_KEY` or is generated into the state volume with mode 0600 on first start; the Helm chart takes it from a Secret.
- **To the browser:** `redact_record()` masks the same fields to `********` in every REST response and websocket frame. `tests/test_security.py` asserts that a stored secret never appears in a response body or in the raw state file.
- **In logs and subprocesses:** `cbbackupmgr` invocations are logged with credentials redacted (`couchbase_native._redact`); the auth logger never logs the token; connection strings are logged only as the operator typed them.
- **To the assistant:** the local Qwen model receives migration state through the same redaction; credentials never leave the deployment (the LLM runs in the Compose network, not a hosted API).

### What the appliance connects to

By design, an authenticated operator points the agent at arbitrary database
hosts (connection strings, DynamoDB endpoint overrides, Cosmos endpoints,
Capella management API). That is the product, not an SSRF to close - which
is exactly why the API token, localhost-only ports and the throttling above
matter: anyone holding the token can make the backend open connections to
anything it can reach. Run it on a host whose network position matches the
databases it is meant to migrate, and nothing more.

## Container and infrastructure hardening

### Non-root containers

`backend/Dockerfile` creates a dedicated `app` user (uid/gid 10001). The
container starts as root only so `entrypoint.sh` can fix the ownership of
data volumes created by older, root-running versions, then drops to uid
10001 with `setuid`/`setgid` before `uvicorn` starts; on Kubernetes the chart
runs the pod as 10001 directly (`runAsNonRoot`, `fsGroup`). The UI image is
`nginxinc/nginx-unprivileged`, which runs master and workers as uid 101 and
listens on 8080, so it never needs `CAP_NET_BIND_SERVICE`. Both images make
the copied code world-readable so an owner-only checkout can't leave the
unprivileged process unable to read it (CIS Docker §4.1, NIST SP 800-190).

### Linux capabilities and privilege escalation

`docker-compose.yml` now sets `security_opt: [no-new-privileges:true]` on
every service (CIS Docker §5.25 - no setuid/setgid binary can grant extra
privileges, including one a future dependency might introduce; dropping
privileges *down* in the entrypoint is unaffected). The three images this
project builds get `cap_drop: [ALL]`: `frontend` and `memory-init` need
nothing back; `backend` adds back exactly `CHOWN`, `DAC_OVERRIDE`, `FOWNER`,
`SETUID` and `SETGID` - the set its ownership-fixing-then-drop-root entrypoint
uses, and nothing the application itself can use once it is uid 10001.
`couchbase-memory` (vendor Couchbase Server image) and `qwen-service` (vendor
Ollama image) keep their default capability sets - their internal privilege
needs aren't something this project controls, and a wrongly-dropped
capability on a stateful database is a worse failure mode than on a
stateless API - but both get `no-new-privileges`, which is safe
unconditionally. The Helm chart already set `allowPrivilegeEscalation:
false`, `capabilities.drop: [ALL]` and `seccompProfile: RuntimeDefault` on
the backend, frontend and memory pods.

### Resource limits and read-only filesystems (reviewed, not applied)

CIS Docker §5.10/5.11 recommend memory/CPU caps and a read-only root
filesystem. Neither is applied blind here: the backend's memory use scales
with the migration's concurrency and batch sizes, and the LLM container's
with the model, so a guessed ceiling would OOM-kill a real migration on a
smaller host. The backend writes to `/data/state`, `/data/scratch` (SQLite
key sets for reconciliation and fallback, materialized CA PEMs) and
`/data/cbbackupmgr-archives`, all already on named volumes; a read-only root
filesystem is therefore close, but needs `/tmp` and the Python/SDK write
paths audited first. Both are recorded as follow-ups for whoever sizes a
specific deployment; the Helm chart exposes `resources` for the same reason.

## Network exposure

Every port the Compose stack publishes binds to `127.0.0.1`: the UI (5173),
the API (8000, which the UI reaches over the Compose network, not the host
port), the memory Couchbase Server's console/data ports (8091-8096,
11210-11211) and Ollama (11434). Reaching the UI from another machine is a
deliberate change to the frontend mapping *and* setting `API_AUTH_TOKEN`,
as the compose file's header comment says. The Helm chart exposes only the
frontend Service (ClusterIP) and, optionally, an Ingress for it; the backend
and memory store are ClusterIP services the frontend proxies to (PCI DSS v4.0
Requirement 1, NIST SC-7).

## Cryptography and transport

- **To data stores:** TLS certificate verification is on for every source
  connector, the destination SDK connection, the Couchbase management REST
  API and `cbbackupmgr`. A private CA can be pasted per connection
  (materialized to the scratch directory with mode 0600); `tls_skip_verify`
  exists for throwaway test clusters and validation flags it as a warning.
  Both Couchbase connectors append `tls_verify=none` only when that flag is
  set. (PCI DSS v4.0 4.2.1, NIST SC-8.)
- **To the browser:** the shipped nginx serves plain HTTP on 8080 and is
  meant to sit behind a TLS terminator (the Helm chart's Ingress with `tls`,
  or a reverse proxy on the host). It sends HSTS once `X-Forwarded-Proto`
  says the request came over HTTPS, and nothing before that. Cipher-suite
  selection therefore belongs to the terminator; if you terminate TLS in
  this nginx instead, restrict it to TLS 1.2/1.3 with AEAD ciphers as AOM's
  `ui/nginx.conf.template` does.
- **Secrets at rest:** Fernet (AES-128-CBC + HMAC-SHA256, authenticated) via
  the `cryptography` package; key material is never derived from a
  password.

## Reviewed and already compliant

Worth stating explicitly, since "reviewed and found fine" is easy to mistake
for "not reviewed":

- **No wildcard CORS, no cookies.** There has never been an `allow_origins=["*"]` here, and there is no session cookie for a cross-site page to ride.
- **N1QL parameters.** Every value that comes from a caller is a named parameter; identifiers are validated/sanitized (above).
- **Secret redaction** in responses, websocket frames and the state file is tested (`tests/test_security.py`).
- **No debug/reload flags:** `uvicorn` runs without `--reload`; nothing sets `debug=True`.
- **Dependencies are exactly pinned** (`==` throughout `backend/requirements.txt`; the frontend installs with `npm ci` from `package-lock.json`).
- **Secrets never committed:** `.gitignore` excludes `.env`, `.env.*`, `*.pem`, `*.key`, `*.crt`; `env.example` carries placeholders only; Compose refuses to start without `MEMORY_CB_PASSWORD`.
- **Health endpoint** returns only the service name and version.
- **Request size** is capped at 5 MiB by nginx (`client_max_body_size`), enough for a pasted CA certificate and far below anything that could be used to exhaust the backend.
- **Helm chart** already runs every pod non-root with seccomp `RuntimeDefault`, drops all capabilities and disables privilege escalation; the backend's encryption key and API token come from a Secret.

## Known gaps

- **Single shared token, no per-operator identity.** Every operator presents the same `API_AUTH_TOKEN`; the audit trail records "who approved" only as the free-text name typed in the wizard. Per-user authentication (OIDC/LDAP, as AOM has) is the natural next step for a shared deployment. Until then, put the UI behind an SSO-enforcing reverse proxy or ingress if more than one person uses it.
- **Throttle state is per-process** (see above).
- **Token rotation** means restarting the backend with a new `API_AUTH_TOKEN`; there is no grace window where two tokens are valid.
- **Read-only root filesystem and resource limits** are recorded, not applied (see above).
- **`style-src 'unsafe-inline'`** stays in the UI's CSP for the reason given above.
- **Vendor images** (`couchbase:enterprise`, `ollama/ollama`) keep their default capability sets.
- **DISA STIG** alignment is at the control-objective level only; nothing here certifies a deployment against a specific STIG.

## Verifying these changes

```bash
# Security headers on the API (HSTS appears only with X-Forwarded-Proto: https)
curl -sI http://localhost:8000/api/health | grep -iE "x-frame|x-content-type|content-security|referrer-policy|permissions-policy|cache-control"
curl -sI -H "X-Forwarded-Proto: https" http://localhost:8000/api/health | grep -i strict-transport

# Security headers on the dashboard
curl -sI http://localhost:5173/ | grep -iE "x-frame|x-content-type|content-security|referrer-policy|permissions-policy"

# Docs are off outside development
ENVIRONMENT=production ... ; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/docs   # 404

# Lockout: 11 bad tokens from one address -> the last one is 429 with Retry-After
for i in $(seq 1 11); do
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/migrations -H "Authorization: Bearer wrong"
done
docker compose logs backend | grep "onboarding_agent.auth"

# Bucket-name validation
curl -s -X POST http://localhost:8000/api/migrations -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -H "Content-Type: application/json" -d '{"name":"x","destination_bucket":"a`b", ...}'   # 422

# Non-root process and capabilities inside the backend container
docker compose exec backend id            # uid=10001(app) gid=10001(app)
docker compose exec backend grep CapEff /proc/1/status

# Nothing listens beyond localhost
docker compose ps --format '{{.Name}} {{.Ports}}'   # every mapping starts with 127.0.0.1

# The automated checks
cd backend && pytest tests/test_hardening.py tests/test_security.py
```

## Standards referenced

- **CIS Benchmarks** - CIS Docker Benchmark (image and runtime configuration).
- **NIST SP 800-53 Rev. 5** - AC (Access Control), AU (Audit and Accountability), CM (Configuration Management), IA (Identification and Authentication), SC (System and Communications Protection), SI (System and Information Integrity).
- **NIST SP 800-123** - Guide to General Server Security.
- **NIST SP 800-190** - Application Container Security Guide.
- **DISA STIGs** - control-objective alignment only (see Known gaps).
- **PCI DSS v4.0** - Requirements 1, 3.5, 4.2, 6.2, 6.3, 8.3, 8.6 and 10.2.
