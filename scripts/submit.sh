#!/usr/bin/env bash
set -euo pipefail
annotation_project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
annotation_master="${DET_MASTER:-http://10.130.130.5:4096}"
annotation_cli="${ANNOTATION_DET_CLI:-det}"
annotation_client_python="${ANNOTATION_CLIENT_PYTHON:-python3}"
annotation_instance="${ANNOTATION_INSTANCE:-}"
annotation_local_port="${ANNOTATION_LOCAL_PORT:-8088}"
if [[ -z "${ANNOTATION_DET_CLI:-}" && -x "$annotation_project/.venv-det/bin/det" ]]; then
  annotation_cli="$annotation_project/.venv-det/bin/det"
  annotation_client_python="$annotation_project/.venv-det/bin/python"
fi
if [[ -n "$annotation_instance" && ! "$annotation_instance" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "ANNOTATION_INSTANCE may contain only letters, numbers, underscores, and hyphens." >&2
  exit 2
fi
if [[ ! "$annotation_local_port" =~ ^[0-9]+$ ]] || (( annotation_local_port < 1 || annotation_local_port > 65535 )); then
  echo "ANNOTATION_LOCAL_PORT must be between 1 and 65535." >&2
  exit 2
fi
# Do not forward the development container's localhost HTTP proxy to the cluster.
export NO_PROXY="${NO_PROXY:-},10.130.130.5,node1.zjuscl.lab"
export no_proxy="$NO_PROXY"
# det 0.22.2 uses lomond for --publish, which ignores NO_PROXY.
# Unset proxies in this subprocess only; do not affect the caller's shell.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
if [[ "${1:-}" == --check ]]; then
  "$annotation_cli" -m "$annotation_master" user whoami
  "$annotation_cli" -m "$annotation_master" master info
  exit
fi
case "${1:-}" in
  ""|--command|--publish|--validate-only) ;;
  *) echo "Usage: bash submit.sh [--check|--validate-only|--publish|--command]" >&2; exit 2 ;;
esac
if [[ "${1:-}" == --command && -n "$annotation_instance" ]]; then
  echo "ANNOTATION_INSTANCE is supported for experiments; use --publish or omit --command." >&2
  exit 2
fi
"$annotation_cli" -m "$annotation_master" user whoami
# Stage only source files: no annotations, virtual environments, model weights,
# or local verification output should become the uploaded model definition.
annotation_context="$(mktemp -d /tmp/contact-annotation-submit.XXXXXX)"
cp -- "$annotation_project"/{app.py,requirements.txt,start.sh} "$annotation_context/"
mkdir -p -- "$annotation_context/backend" "$annotation_context/scripts"
cp -- "$annotation_project/backend/"*.py "$annotation_context/backend/"
cp -- "$annotation_project/scripts/start.sh" "$annotation_context/scripts/start.sh"
cp -R -- "$annotation_project/static" "$annotation_context/static"
annotation_config="$annotation_project/configs/experiment.yaml"
if [[ -n "$annotation_instance" ]]; then
  annotation_config="$annotation_context/experiment-$annotation_instance.yaml"
  "$annotation_client_python" "$annotation_project/scripts/instance_config.py" \
    "$annotation_project/configs/experiment.yaml" "$annotation_config" "$annotation_instance"
fi
if [[ "${1:-}" == --validate-only ]]; then
  "$annotation_client_python" "$annotation_project/scripts/validate_experiment.py" "$annotation_config" "$annotation_context" --master "$annotation_master"
elif [[ "${1:-}" == --command ]]; then
  "$annotation_cli" -m "$annotation_master" command run --config-file "$annotation_project/configs/command.yaml" --context "$annotation_context" --detach bash start.sh
elif [[ "${1:-}" == --publish ]]; then
  echo "Opening the web service at http://localhost:$annotation_local_port after scheduling. Keep this terminal open."
  "$annotation_cli" -m "$annotation_master" e create --follow-first-trial --publish "$annotation_local_port:8088" "$annotation_config" "$annotation_context"
else
  "$annotation_cli" -m "$annotation_master" e create "$annotation_config" "$annotation_context"
fi
