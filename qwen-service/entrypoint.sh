#!/usr/bin/env bash
set -e

# Start the Ollama server in the background, then pull the Qwen model on first
# boot (cached in the ollama_data volume thereafter -- see docker-compose.yml).
ollama serve &
SERVER_PID=$!

echo "Waiting for Ollama server to be ready..."
until ollama list >/dev/null 2>&1; do
  sleep 1
done

# Chat model and the (separate, much smaller) embedding model.
for model in "${QWEN_MODEL}" "${QWEN_EMBEDDING_MODEL}"; do
  [ -n "$model" ] || continue
  if ! ollama list | grep -q "${model}"; then
    echo "Pulling ${model} (first run only, cached afterwards)..."
    ollama pull "${model}"
  fi
done

wait $SERVER_PID
