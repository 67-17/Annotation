#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export MODEL_PATH="${MODEL_PATH:-/run/determined/NAS1/public/HuggingFace/Qwen/Qwen3-VL-8B-Instruct}"
export ANNOTATION_STATE_DIR="${ANNOTATION_STATE_DIR:-/home/liushiqi/Annotation/state}"
export PRELOAD_MODEL="${PRELOAD_MODEL:-0}"
export TOKENIZERS_PARALLELISM=false
if [[ ! -r "$MODEL_PATH/config.json" ]]; then
  echo "Model directory unavailable in this task: $MODEL_PATH. Check NAS mounts." >&2
  id >&2
  ls -ld /run/determined/NAS1/public "$MODEL_PATH" >&2 || true
  exit 1
fi
if [[ -n "${SMOKE_DATASET:-}" && ! -r "$SMOKE_DATASET/meta/info.json" ]]; then
  echo "Smoke dataset unavailable in this task: $SMOKE_DATASET. Check the read-only NAS mount." >&2
  exit 1
fi
mkdir -p -- "$ANNOTATION_STATE_DIR"
annotation_state_probe="$(mktemp "$ANNOTATION_STATE_DIR/.write-check.XXXXXX")"
unlink -- "$annotation_state_probe"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$ANNOTATION_STATE_DIR/cache/pip}"
export HF_HOME="${HF_HOME:-$ANNOTATION_STATE_DIR/cache/huggingface}"
echo "PATHS_OK model=$MODEL_PATH state=$ANNOTATION_STATE_DIR smoke=${SMOKE_DATASET:-disabled}"
if [[ "${ANNOTATION_SKIP_INSTALL:-0}" != 1 ]]; then
  annotation_runtime="$(mktemp -d /tmp/contact-annotation-runtime.XXXXXX)"
  python3 -m venv --system-site-packages "$annotation_runtime"
  annotation_python="$annotation_runtime/bin/python"
  "$annotation_python" -m pip install --disable-pip-version-check -r requirements.txt
else
  annotation_python="${ANNOTATION_PYTHON:-python3}"
fi
"$annotation_python" -c 'from backend.dataset import ffmpeg_binary; binary=ffmpeg_binary(); print({"ffmpeg":binary}); assert binary, "No ffmpeg available for video decoding"'
"$annotation_python" -c 'import torch,transformers; from transformers import Qwen3VLForConditionalGeneration; print({"torch":torch.__version__,"transformers":transformers.__version__,"cuda":torch.cuda.is_available()}); assert torch.cuda.is_available(), "CUDA is unavailable in the allocated task"'
echo "Contact Lab: host=${WEB_HOST:-127.0.0.1} port=${WEB_PORT:-8088} state=$ANNOTATION_STATE_DIR"
echo "After model startup, look for MODEL_SMOKE_OK in task logs. This is a long-running service; stop the task when finished."
annotation_source="${ANNOTATION_SOURCE_DIR:-/home/liushiqi/Annotation}"
if [[ ! -r "$annotation_source/app.py" || ! -r "$annotation_source/backend/app.py" ]]; then
  annotation_source="$PWD"
fi
annotation_reload_file="$ANNOTATION_STATE_DIR/reload-token"
if [[ ! -s "$annotation_reload_file" ]]; then
  "$annotation_python" -c 'import secrets,sys; open(sys.argv[1],"w").write(secrets.token_urlsafe(32))' "$annotation_reload_file"
  chmod 600 "$annotation_reload_file"
fi
export ANNOTATION_RELOAD_TOKEN="$(<"$annotation_reload_file")"
echo "SOURCE_READY source=$annotation_source manual_reload=$annotation_reload_file"
while true; do
  set +e
  (cd -- "$annotation_source" && "$annotation_python" -m uvicorn app:app \
    --host "${WEB_HOST:-127.0.0.1}" --port "${WEB_PORT:-8088}" --workers 1)
  annotation_exit=$?
  set -e
  if [[ "$annotation_exit" != 75 ]]; then
    exit "$annotation_exit"
  fi
  echo "MANUAL_RELOAD requested; restarting from $annotation_source"
done
