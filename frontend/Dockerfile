# Build the UI, then serve it with an unprivileged nginx that terminates TLS
# (HTTPS on 8443 by default, HTTP on 8080 redirects to it) and proxies /api and
# /ws to the backend (see nginx.conf.template, tls.conf.template,
# tls-entrypoint.sh).
FROM node:20-slim AS build
WORKDIR /app

# Optional corporate/proxy root CA (see scripts/setup-corporate-ca.sh). No-op when certs/ is empty.
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN if find /usr/local/share/ca-certificates/extra -name '*.crt' 2>/dev/null | grep -q .; then \
      apt-get update && \
      apt-get install -y --no-install-recommends ca-certificates && \
      update-ca-certificates && \
      rm -rf /var/lib/apt/lists/*; \
    fi
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
# Leave empty to call the API same-origin through nginx (the default). Only set
# it when the UI is hosted separately from the backend.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
# The build context keeps each file's host permissions, and a checkout can be
# owner-only (umask 077, or files written by other tools). nginx runs as uid 101
# in the serve stage, so make the site and the config template world-readable
# here; COPY --from keeps these modes (and needs no BuildKit-only --chmod).
RUN npm run build && \
    chmod -R a+rX dist && \
    chmod 0644 nginx.conf.template tls.conf.template site.inc.template && \
    chmod 0755 tls-entrypoint.sh

FROM nginxinc/nginx-unprivileged:1.27-alpine AS serve
# openssl for the self-signed fallback. /etc/nginx/tls is owned by the nginx
# user and group-writable for gid 10001 (the backend's user) so that, with the
# directory shared between the two containers (docker-compose), a certificate
# uploaded under Settings -> Certificates can be dropped here by the backend.
USER root
RUN apk add --no-cache openssl && \
    mkdir -p /etc/nginx/tls && chown 101:10001 /etc/nginx/tls && chmod 2770 /etc/nginx/tls
USER 101
ENV BACKEND_URL=http://backend:8000 \
    TLS_ENABLED=true \
    HTTPS_PORT=8443 \
    TLS_HOSTNAME=localhost
COPY --from=build /app/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/tls.conf.template   /etc/nginx/templates/tls.conf.template
COPY --from=build /app/site.inc.template   /etc/nginx/templates/site.inc.template
COPY --from=build /app/tls-entrypoint.sh   /docker-entrypoint.d/25-tls.sh
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080 8443
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
