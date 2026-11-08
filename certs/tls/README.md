# TLS certificate for the UI

The frontend container serves HTTPS. Without a certificate here it generates a
self-signed one on first start (SANs from `TLS_HOSTNAME` in `.env`) and keeps it
in the `frontend_tls` volume, so the browser warns until you trust it.

To use your own certificate put these two files here and switch the frontend's
volume in `docker-compose.yml` to the `./certs/tls:/etc/nginx/tls:ro` bind mount:

- `server.crt` -- PEM certificate (full chain, leaf first)
- `server.key` -- PEM private key

On Kubernetes use `frontend.tls.existingSecret` (a `kubernetes.io/tls` Secret)
instead. Both files are git-ignored.
