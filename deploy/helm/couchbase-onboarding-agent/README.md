# couchbase-onboarding-agent Helm chart

Deploys the Couchbase Onboarding Agent: the API/migration engine, the UI (nginx, same-origin proxy to the
API), and optionally an in-cluster Ollama for the assistant and a single-node agent-memory Couchbase for
evaluation.

## Install

```bash
helm install onboarding deploy/helm/couchbase-onboarding-agent -n migrations --create-namespace
```

Get the generated API token (the UI asks for it) and open the UI:

```bash
kubectl get secret onboarding-couchbase-onboarding-agent-secrets -n migrations \
  -o jsonpath='{.data.API_AUTH_TOKEN}' | base64 -d; echo
kubectl port-forward -n migrations svc/onboarding-couchbase-onboarding-agent-frontend 8443:443
```

Then browse to <https://localhost:8443>. The UI is HTTPS by default: the frontend's nginx terminates
TLS with a self-signed certificate the chart generates on first install (kept in Secret
`<release>-frontend-tls`), so the browser warns until you trust it. Put the names and IPs users will
browse to into the SANs with `--set frontend.tls.hostnames={ui.example.com}` /
`--set frontend.tls.ipAddresses={10.0.0.5}`, or hand it your own `kubernetes.io/tls` Secret with
`frontend.tls.existingSecret`. Plain HTTP on `frontend.service.httpPort` (8080) redirects to HTTPS.
A certificate can also be uploaded in the running app (Settings → Certificates): with
`frontend.tls.managedByApp` (default `true`) the backend gets a ServiceAccount allowed to update exactly that
Secret and nginx reloads when the kubelet syncs it.

The first start downloads the LLM (several GB); the assistant reports "waiting" until it's ready.
Migrations work without it.

## What gets deployed

| Component | Kind | Notes |
|---|---|---|
| backend | Deployment (1 replica, `Recreate`) + Service | Owns the migration state file; keep a single replica |
| state | PVC (RWO, kept on uninstall) | Encrypted migration records, checkpoints, fallback state |
| archives | PVC (RWO) | `cbbackupmgr` archives for Couchbase-source bulk copies |
| frontend | Deployment + Service | nginx: HTTPS on 8443 (`frontend.tls`), HTTP on 8080 (redirects, or serves behind an Ingress); proxies `/api` and `/ws` to the backend |
| frontend-tls | Secret (`kubernetes.io/tls`, kept on uninstall) | Generated self-signed certificate; skipped with `frontend.tls.existingSecret` |
| llm | Deployment + PVC + Service | `llm.enabled`, skipped when `llm.externalUrl` is set |
| memory | StatefulSet + headless Service + init Job | `memory.bundled.enabled`; evaluation only (see licensing) |
| secrets | Secret (kept on uninstall) | API token, credential encryption key, memory password, Capella token |
| ingress | Ingress | `ingress.enabled` |

Backend and UI pods run as non-root with all capabilities dropped and the `RuntimeDefault` seccomp profile.

## Production checklist

```yaml
# values-prod.yaml
secrets:
  existingSecret: onboarding-secrets     # API_AUTH_TOKEN, STATE_ENCRYPTION_KEY, MEMORY_CB_PASSWORD, CAPELLA_API_TOKEN
memory:
  bundled:
    enabled: false                       # the couchbase:enterprise image is free for dev/test only
  external:
    connectionString: couchbases://cb.memory.example.com
  password: ""                           # comes from existingSecret
llm:
  externalUrl: http://ollama.ai.svc:11434   # or keep llm.enabled with GPU nodes
ingress:
  enabled: true
  className: nginx
  hosts:
    - host: onboarding.example.com
      paths: [{ path: /, pathType: Prefix }]
  tls:
    - secretName: onboarding-tls
      hosts: [onboarding.example.com]
frontend:
  tls:
    enabled: false                       # the Ingress terminates TLS; the pod can stay plain HTTP
backend:
  persistence:
    state:
      storageClassName: premium-rwo
    archives:
      size: 500Gi                        # largest Couchbase source bucket you'll copy
```

- **Back up the state PVC together with `STATE_ENCRYPTION_KEY`.** The key decrypts the source and
  destination credentials stored with each migration. Both the state PVC and the generated Secret carry
  `helm.sh/resource-policy: keep`, so `helm uninstall` doesn't delete them.
- **Egress.** The backend needs network access to every source and destination (allow-list the cluster's
  egress IPs there). If you use NetworkPolicies, allow backend egress to those endpoints, to the LLM service
  and to agent memory; the frontend only needs the backend.
- **Websockets through the ingress** need long read timeouts (defaults set for ingress-nginx).
- **Single replica.** Migration state is a single-writer file and running syncs live in the backend
  process, so `backend.replicaCount` must stay 1. Restarts are safe: running migrations resume from
  their checkpoints (`backend.sync.autoResumeOnStartup`).
- **GPU for the LLM**: add `nvidia.com/gpu: 1` under `llm.resources.limits` and the matching
  `llm.nodeSelector` / `llm.tolerations`.

## Images

`backend`, `frontend` and `memory-init` are built from this repository and published to
`ghcr.io/couchbaselabs/` for each release tag. To use your own registry:

```bash
REG=registry.example.com/team TAG=1.0.0
docker build -t $REG/couchbaselabs/couchbase-onboarding-agent-backend:$TAG backend
docker build -t $REG/couchbaselabs/couchbase-onboarding-agent-frontend:$TAG frontend
docker build -t $REG/couchbaselabs/couchbase-onboarding-agent-memory-init:$TAG -f scripts/Dockerfile.init .
docker push ...   # all three
helm install onboarding deploy/helm/couchbase-onboarding-agent --set global.imageRegistry=$REG
```

The backend image copies `cbbackupmgr` from the `couchbase:enterprise` image, which is subject to
Couchbase's license terms.

## Values

| Key | Default | Description |
|---|---|---|
| `global.imageRegistry` | `ghcr.io` | Registry for the backend, frontend and memory-init images |
| `secrets.existingSecret` | `""` | Use your own Secret instead of the generated one |
| `secrets.apiAuthToken` / `stateEncryptionKey` | generated | Set explicitly to manage them yourself |
| `backend.persistence.state.size` | `2Gi` | State PVC size |
| `backend.persistence.archives.enabled` / `size` | `true` / `50Gi` | cbbackupmgr archive PVC |
| `backend.sync.*` | see values.yaml | Continuous sync tuning (batching, failures, reconciliation, cutover) |
| `backend.extraEnv` | `[]` | Any other backend setting (`backend/app/config.py`) |
| `frontend.service.type` | `ClusterIP` | Use `LoadBalancer` to expose without an ingress |
| `ingress.*` | disabled | Ingress to the frontend |
| `llm.enabled` / `llm.externalUrl` / `llm.model` | `true` / `""` / `qwen3:4b` | Assistant model endpoint; `enabled: false` with no `externalUrl` runs without the assistant (`LLM_ENABLED=false`) |
| `llm.embeddingModel` / `llm.embeddingDims` | `nomic-embed-text` / `768` | Agent-memory embedding model and its vector length (change together) |
| `memory.bundled.enabled` | `true` | Run an evaluation agent-memory node |
| `memory.external.connectionString` | `""` | Agent-memory cluster when not bundled |

See [`values.yaml`](values.yaml) for resources, security contexts, node selection and the rest.

## Upgrades

`helm upgrade` keeps generated secrets (read back from the existing Secret) and recreates the backend pod;
running migrations resume from their checkpoints. The memory init Job re-runs and is idempotent.
