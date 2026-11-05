#!/bin/sh
# Entry point for the PolyStrata container. The engine is compiled during docker
# build and lives in the image, so the first start only downloads the model.
# The install config is kept on the /data volume so a recreated container skips
# the setup pass and goes straight to serving.
set -e
cd /opt/strata || exit 1

# MODEL_ENV picks a preset file (.env.qwen, .env.coder, .env.clover, .env.qwen36, .env.unsloth).
# docker-compose.yml hands the same name to env_file, but a platform that builds the Dockerfile and never reads
# that file would start the defaults (qwen / IQ2_XS) and a model switch would look like it worked when it did
# not. So the preset is read here too, inside the container, where both ways end. It wins over FAMILY / MODEL /
# ... given beside it (choosing a preset means choosing its settings), except REINSTALL, which stays as the
# platform set it, so one re-setup pass can be asked for without editing the preset. A preset that is named but
# missing stops the start instead of serving the default model.
if [ -n "${MODEL_ENV:-}" ]; then
  case "$MODEL_ENV" in
    */* | . | ..) echo "MODEL_ENV=$MODEL_ENV: name a preset file from the image, not a path." >&2
                  echo "  The presets are .env.qwen, .env.coder, .env.clover, .env.qwen36 and .env.unsloth." >&2
                  exit 1 ;;
  esac
  preset="./$MODEL_ENV"
  if [ ! -f "$preset" ]; then
    echo "MODEL_ENV=$MODEL_ENV: no such file ($preset). The presets in the image are .env.qwen, .env.coder," >&2
    echo "  .env.clover, .env.qwen36 and .env.unsloth: check it is committed and in the build." >&2
    exit 1
  fi
  reinstall_env="${REINSTALL:-}"
  set -a
  . "$preset"
  set +a
  if [ -n "$reinstall_env" ]; then REINSTALL="$reinstall_env"; fi
  echo "Preset: $MODEL_ENV (MODEL_ENV)"
fi

# #1584: the engine in the image is built with AVX2 CPU kernels. A host CPU without AVX2 (Sandy / Ivy Bridge Xeons,
# Bulldozer) runs it into an illegal instruction. A warning, not a stop: recommend, never force (the flags file
# can be wrong in a VM, and a build made on that host may be fine). STRATA_CPUINFO is for the test.
cpuinfo="${STRATA_CPUINFO:-/proc/cpuinfo}"
if [ -r "$cpuinfo" ] && grep -q '^flags' "$cpuinfo" && ! grep -m1 '^flags' "$cpuinfo" | grep -qw avx2; then
  echo "WARNING: this host CPU has no AVX2. The engine in this image is built for CPUs with AVX2, so it may stop" >&2
  echo "  with 'illegal instruction' (exit 132). Build the image on this machine (docker build), which compiles the" >&2
  echo "  engine for the CPU it runs on, and see docs/INSTALL.md (Older CPUs, experimental) for the older-CPU build." >&2
fi

STRATA_DATA="${STRATA_DATA:-/data}"
FAMILY="${FAMILY:-qwen}"
MODEL="${MODEL:-IQ2_XS}"
BACKEND="${BACKEND:-}"          # cuda | hip | sycl; set to appropriate backend for your GPU
CONTEXT="${CONTEXT:-32768}"
VISION="${VISION:-no}"          # no | yes | cpu (the image encoder on the CPU)
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
API_KEY="${API_KEY:-}"
KV="${KV:-}"                    # int8 | q4_0 | k8v4; empty: setup.py's own default (int8)
GPUS="${GPUS:-}"                # "0,2" or "all": one model across several cards (docs/MULTI_GPU.md)
GPU="${GPU:-}"                  # one card, numbered as nvidia-smi numbers them
LAYER_SPLIT="${LAYER_SPLIT:-}"  # with GPUS: where each later card's layers start (default: auto)
LOW_RAM="${LOW_RAM:-auto}"      # on: the experts come from the pack's experts.bin, not from RAM
GGUF_DIR="${GGUF_DIR:-}"        # a mounted folder with GGUF files you already have: no download
RESIDENT_BUDGET_GIB="${RESIDENT_BUDGET_GIB:-}"   # UD-Q4_K_XL: GiB of experts kept in RAM (default: setup's pick)
KV_STREAMING="${KV_STREAMING:-}" # auto | on | off; empty: setup.py's own default (auto)
CONFIG="${CONFIG:-}"            # a config file to start with (wins over MODEL's /data/config/strata-<model>.json)
PARALLEL="${PARALLEL:-}"        # up to N requests decode together in batch slots (setup's --parallel, docs/BATCHING.md);
                                # empty: one at a time, the rest wait in the queue
CONVERSATION_CACHE_MIB="${CONVERSATION_CACHE_MIB:-}"        # MiB of RAM the parked conversations may hold (setup's
                                # --conversation-cache-mib); empty: setup asks, and an existing config keeps its answer
CONVERSATION_CACHE_SLOTS="${CONVERSATION_CACHE_SLOTS:-}"    # how many conversations are parked at once (its
                                # --conversation-cache-slots, the engine's default 4); needs the MiB above

# setup.py starts the newest strata-*.json it finds, so link in exactly the one
# this family and model were set up with. The config is the recorded output of
# that setup (the pack, the profile, the quant, the KV decision), not settings
# the entry point could rebuild from env vars. qwen has an empty family tag.
case "$FAMILY" in qwen) prefix="" ;; *) prefix="${FAMILY}-" ;; esac
tag="${prefix}$(printf '%s' "$MODEL" | tr 'A-Z' 'a-z')"
cfg="$STRATA_DATA/config/strata-$tag.json"
mkdir -p "$STRATA_DATA/config"

# REINSTALL is only needed to change settings for a model that is already set up
# (context, vision, KV, host, api_key, parallel, the conversation cache). Switching
# between models already on the volume needs no setup pass: their config is already
# there, and the settings below are recorded in it.
#
# KV / GPUS / GPU / LAYER_SPLIT / PARALLEL / CONVERSATION_CACHE_MIB / CONVERSATION_CACHE_SLOTS
# are passed only when set, so an unset one keeps setup.py's own default (the conversation
# cache: what setup asks, or what the model's config already has). LOW_RAM is always passed:
# setup.py measures the PC's RAM from /proc/meminfo, which in a container is the host's total,
# not the container's limit, so a memory-capped container has to ask for the low-RAM mode itself.
#
# Which config the server starts with (#1244): CONFIG when set; else a link in /opt/strata that already points
# into $STRATA_DATA/config/ (a pod command that picked one by linking); else MODEL's strata-<model>.json.
link="/opt/strata/strata-$tag.json"
keep=""
if [ -n "$CONFIG" ]; then
  [ -f "$CONFIG" ] || { echo "CONFIG=$CONFIG does not exist." >&2; exit 1; }
elif [ "${REINSTALL:-0}" != "1" ] && [ -L "$link" ] && [ -f "$link" ]; then
  case "$(readlink "$link")" in "$STRATA_DATA"/config/*) keep=1 ;; esac
fi

if [ -n "$CONFIG" ]; then
  ln -sfn "$CONFIG" "$link"
  echo "Config: $CONFIG (from CONFIG)"
elif [ -n "$keep" ]; then
  echo "Config: $(readlink "$link") (existing link kept)"
elif [ "${REINSTALL:-0}" = "1" ] || [ ! -f "$cfg" ]; then
  if [ -n "$GGUF_DIR" ]; then
    echo "Setting up $tag from the GGUF files in $GGUF_DIR (the engine is already in the image)."
  else
    echo "Setting up $tag: downloading the model (size depends on MODEL; the engine is already in the image)."
  fi
  set -- --family "$FAMILY" --model "$MODEL" --context "$CONTEXT" --vision "$VISION" \
    --data-dir "$STRATA_DATA" --host "$HOST" --api-key "$API_KEY" \
    --port "$PORT" --no-start --low-ram "$LOW_RAM"
  if [ -n "$KV" ]; then set -- "$@" --kv "$KV"; fi
  if [ -n "$GPUS" ]; then set -- "$@" --gpus "$GPUS"; fi
  if [ -n "$GPU" ]; then set -- "$@" --gpu "$GPU"; fi
  if [ -n "$LAYER_SPLIT" ]; then set -- "$@" --layer-split "$LAYER_SPLIT"; fi
  if [ -n "$GGUF_DIR" ]; then set -- "$@" --gguf-dir "$GGUF_DIR"; fi
  if [ -n "$RESIDENT_BUDGET_GIB" ]; then set -- "$@" --resident-budget-gib "$RESIDENT_BUDGET_GIB"; fi
  if [ -n "$KV_STREAMING" ]; then set -- "$@" --kv-streaming "$KV_STREAMING"; fi
  if [ -n "$BACKEND" ]; then set -- "$@" --backend "$BACKEND"; fi
  if [ -n "$PARALLEL" ]; then set -- "$@" --parallel "$PARALLEL"; fi
  if [ -n "$CONVERSATION_CACHE_MIB" ]; then set -- "$@" --conversation-cache-mib "$CONVERSATION_CACHE_MIB"; fi
  if [ -n "$CONVERSATION_CACHE_SLOTS" ]; then set -- "$@" --conversation-cache-slots "$CONVERSATION_CACHE_SLOTS"; fi
  .venv/bin/python setup.py --setup --yes "$@"
  [ -e "/opt/strata/strata-$tag.json" ] && { cmp -s "/opt/strata/strata-$tag.json" "$cfg" || cp -f "/opt/strata/strata-$tag.json" "$cfg"; }
  echo "Config: $cfg (from MODEL $MODEL, just set up)"
else
  # #1244: the copy on the volume is the one that counts, so a regular file left in /opt/strata by an earlier setup
  # (or by an image built with one) must not stand in for it: edits to /data/config would be ignored
  ln -sfn "$cfg" "/opt/strata/strata-$tag.json"
  echo "Config: $cfg (from MODEL $MODEL)"
fi

# Later starts skip straight here: setup.py finds the installed config and
# launches serve/server.py (OpenAI- and Anthropic-compatible API on :8080).
# GPUS / GPU / LAYER_SPLIT are repeated on purpose. Given at the start they pin the
# cards for this model, and setup.py saves them in its config; without them a config
# that names one card is offered once to a pair, on its own, when the host has two
# cards that can share the model (setup.py's offer_together, docs/MULTI_GPU.md).
set -- --port "$PORT"
if [ -n "$GPUS" ]; then set -- "$@" --gpus "$GPUS"; fi
if [ -n "$GPU" ]; then set -- "$@" --gpu "$GPU"; fi
if [ -n "$LAYER_SPLIT" ]; then set -- "$@" --layer-split "$LAYER_SPLIT"; fi
if [ -n "$BACKEND" ]; then set -- "$@" --backend "$BACKEND"; fi
exec .venv/bin/python setup.py "$@"
