# Serves Qwen 3 (8B) locally via Ollama's HTTP API (chat: /api/chat, embeddings:
# /api/embeddings) so the onboarding agent's reasoning and vector-memory embeddings
# never leave the Docker network -- important here specifically because this app
# routinely holds credentials for five different production databases at once.
FROM ollama/ollama:latest

ENV OLLAMA_HOST=0.0.0.0:11434
ENV QWEN_MODEL=qwen3:8b

# Trust an optional corporate/proxy root CA before entrypoint.sh pulls the model at
# container startup -- see scripts/setup-corporate-ca.sh at the repo root. A no-op
# on machines that don't need it.
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN if find /usr/local/share/ca-certificates/extra -name '*.crt' 2>/dev/null | grep -q .; then \
      apt-get update && \
      apt-get install -y --no-install-recommends ca-certificates && \
      update-ca-certificates && \
      rm -rf /var/lib/apt/lists/*; \
    fi

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 11434
ENTRYPOINT ["/entrypoint.sh"]
