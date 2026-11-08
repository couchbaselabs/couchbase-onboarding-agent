#!/usr/bin/env bash
# docker compose wrapper that detects a GPU on this machine and gives it to the
# local LLM (qwen-service) automatically. Everything after the script name is
# passed to `docker compose` unchanged:
#
#   ./scripts/compose.sh up --build
#   ./scripts/compose.sh logs -f qwen-service
#   ./scripts/compose.sh down
#
# Detection order (first match wins), overridable with LLM_COMPUTE in .env or
# the environment (LLM_COMPUTE=cpu ./scripts/compose.sh up):
#
#   nvidia  Docker has the NVIDIA container runtime (Linux + NVIDIA Container
#           Toolkit, or Docker Desktop on Windows/WSL2) -> docker-compose.gpu.yml
#   rocm    Linux host with an AMD GPU (/dev/kfd present)   -> docker-compose.rocm.yml
#   host    Apple Silicon Mac: Docker Desktop can't pass the GPU through, but
#           Ollama installed on the Mac itself uses Metal. If `ollama` is on PATH,
#           the models are pulled there and the backend is pointed at it
#           instead of starting qwen-service        -> docker-compose.host-llm.yml
#   cpu     Anything else: plain docker-compose.yml, Ollama on CPU.
#
# Migration throughput never depends on the model; a GPU only makes the chat
# assistant answer faster (see README "GPU acceleration").
set -euo pipefail

cd "$(dirname "$0")/.."

# .env is read by compose for variable substitution; read the few keys we need
# here too without sourcing the whole file (it may contain arbitrary values).
env_get() {
  local key="$1" default="${2:-}"
  local val
  val="$(grep -E "^${key}=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  printf '%s' "${val:-$default}"
}

# Same precedence as compose itself: the environment wins over .env. An empty
# COMPOSE_PROFILES in the environment is meaningful ("no profiles"), hence ${-}.
compose_profiles="${COMPOSE_PROFILES-$(env_get COMPOSE_PROFILES llm)}"
llm_enabled="${LLM_ENABLED:-$(env_get LLM_ENABLED true)}"
requested="${LLM_COMPUTE:-$(env_get LLM_COMPUTE auto)}"

has_nvidia_runtime() {
  docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'
}

has_rocm() {
  [ "$(uname -s)" = "Linux" ] && [ -e /dev/kfd ] && [ -d /dev/dri ]
}

is_apple_silicon() {
  [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]
}

detect() {
  case "$requested" in
    nvidia|rocm|host|cpu) echo "$requested"; return ;;
    auto|"") ;;
    *) echo "LLM_COMPUTE must be auto, nvidia, rocm, host or cpu (got '$requested')" >&2; exit 2 ;;
  esac
  if has_nvidia_runtime; then echo nvidia
  elif has_rocm; then echo rocm
  elif is_apple_silicon && command -v ollama >/dev/null 2>&1; then echo host
  else echo cpu
  fi
}

files=(-f docker-compose.yml)
mode=cpu
if [[ ",${compose_profiles}," == *",llm,"* && "$llm_enabled" != "false" ]]; then
  mode="$(detect)"
  case "$mode" in
    nvidia) files+=(-f docker-compose.gpu.yml) ;;
    rocm)   files+=(-f docker-compose.rocm.yml) ;;
    host)   files+=(-f docker-compose.host-llm.yml) ;;
  esac
else
  mode="disabled"
fi

# The first argument that isn't a global compose flag is the subcommand.
subcommand=""
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    *) subcommand="$arg"; break ;;
  esac
done

if [[ "$mode" == "host" && ( "$subcommand" == "up" || "$subcommand" == "start" ) ]]; then
  chat_model="$(env_get QWEN_MODEL_NAME "${QWEN_MODEL_NAME:-qwen3:4b}")"
  embed_model="$(env_get QWEN_EMBEDDING_MODEL_NAME "${QWEN_EMBEDDING_MODEL_NAME:-nomic-embed-text}")"
  if ! curl -sf -o /dev/null http://127.0.0.1:11434/api/tags; then
    echo "LLM compute: host  -- starting Ollama on this Mac (Metal GPU)..." >&2
    # `ollama serve` is what the Ollama.app menu-bar app runs; starting it this way
    # works whether or not the app is installed. It keeps running after this script.
    (nohup ollama serve >/tmp/coa-ollama.log 2>&1 &)
    for _ in $(seq 1 30); do
      curl -sf -o /dev/null http://127.0.0.1:11434/api/tags && break
      sleep 1
    done
    curl -sf -o /dev/null http://127.0.0.1:11434/api/tags \
      || { echo "Ollama did not come up on 127.0.0.1:11434 (see /tmp/coa-ollama.log). Set LLM_COMPUTE=cpu to use the container instead." >&2; exit 1; }
  fi
  for model in "$chat_model" "$embed_model"; do
    [ -n "$model" ] || continue
    if ! ollama list 2>/dev/null | grep -q "^${model}"; then
      echo "Pulling ${model} on the host (first run only)..." >&2
      ollama pull "$model"
    fi
  done
fi

case "$mode" in
  nvidia)   echo "LLM compute: nvidia  (NVIDIA container runtime detected; qwen-service gets all GPUs)" >&2 ;;
  rocm)     echo "LLM compute: rocm    (AMD GPU detected; qwen-service uses the ROCm Ollama image)" >&2 ;;
  host)     echo "LLM compute: host    (Apple Silicon; backend uses Ollama on this Mac, qwen-service not started)" >&2 ;;
  cpu)      echo "LLM compute: cpu     (no usable GPU found; set LLM_COMPUTE to override)" >&2 ;;
  disabled) echo "LLM compute: n/a     (assistant disabled in .env)" >&2 ;;
esac

exec docker compose "${files[@]}" "$@"
