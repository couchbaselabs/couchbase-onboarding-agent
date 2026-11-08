#!/usr/bin/env bash
set -e

# Start the Ollama server in the background, then pull the Qwen model on first
# boot (cached in the ollama_data volume thereafter -- see docker-compose.yml).
# Server output is teed to a file so we can report which compute Ollama picked.
OLLAMA_LOG=/tmp/ollama-server.log
ollama serve > >(tee "$OLLAMA_LOG") 2>&1 &
SERVER_PID=$!

echo "Waiting for Ollama server to be ready..."
until ollama list >/dev/null 2>&1; do
  sleep 1
done

# ---- Report GPU detection -------------------------------------------------
# Ollama probes for CUDA/ROCm devices at startup and logs one "inference compute"
# line per device it will use (library=cuda|rocm), or a "no compatible GPUs were
# discovered" line. Surface that as a single unambiguous status line, because the
# usual failure mode is a GPU that exists on the host but was never passed into
# the container (missing docker-compose.gpu.yml / NVIDIA Container Toolkit).
report_compute() {
  # Give the probe a moment; it finishes before the API answers but log flushing lags.
  local tries=0
  until grep -qE 'inference compute|no compatible GPUs|total unused' "$OLLAMA_LOG" 2>/dev/null || [ $tries -ge 20 ]; do
    sleep 0.5; tries=$((tries+1))
  done
  local gpus
  gpus="$(grep -E 'inference compute' "$OLLAMA_LOG" 2>/dev/null | grep -vE 'library=cpu' || true)"
  if [ -n "$gpus" ]; then
    echo "==> LLM compute: GPU"
    # name="NVIDIA ..." library=cuda total="24.0 GiB" available="23.1 GiB"
    echo "$gpus" | sed -E 's/.*(library=[a-z]+).*(name="[^"]*").*(total="[^"]*").*/    \2 \1 \3/'
  else
    echo "==> LLM compute: CPU (no GPU visible to this container)"
    if [ -e /dev/nvidiactl ] || [ -e /dev/kfd ]; then
      echo "    A GPU device node is present but Ollama couldn't use it -- check driver/runtime versions."
    else
      echo "    To use a GPU, start the stack with ./scripts/compose.sh (auto-detects NVIDIA/ROCm/Apple"
      echo "    Silicon) or add docker-compose.gpu.yml / docker-compose.rocm.yml by hand. See README 'GPU acceleration'."
    fi
  fi
}
report_compute

# Chat model and the (separate, much smaller) embedding model.
for model in "${QWEN_MODEL}" "${QWEN_EMBEDDING_MODEL}"; do
  [ -n "$model" ] || continue
  if ! ollama list | grep -q "${model}"; then
    echo "Pulling ${model} (first run only, cached afterwards)..."
    ollama pull "${model}"
  fi
done

wait $SERVER_PID
