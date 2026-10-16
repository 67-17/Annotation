#!/usr/bin/env bash
set -euo pipefail
annotation_project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
annotation_master="${DET_MASTER:-http://10.130.130.5:4096}"
annotation_cli="${ANNOTATION_DET_CLI:-det}"
annotation_client_python="${ANNOTATION_CLIENT_PYTHON:-python3}"
if [[ -z "${ANNOTATION_DET_CLI:-}" && -x "$annotation_project/.venv-det/bin/det" ]]; then
  annotation_cli="$annotation_project/.venv-det/bin/det"
  annotation_client_python="$annotation_project/.venv-det/bin/python"
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
"$annotation_cli" -m "$annotation_master" user whoami
# Stage only source files: no annotations, virtual environments, model weights,
# or local verification output should become the uploaded model definition.
annotation_context="$(mktemp -d /tmp/contact-annotation-submit.XXXXXX)"
cp -- "$annotation_project"/{app.py,batch.py,dataset.py,model_service.py,review.py,requirements.txt,start.sh} "$annotation_context/"
cp -R -- "$annotation_project/static" "$annotation_context/static"
if [[ "${1:-}" == --validate-only ]]; then
  "$annotation_client_python" "$annotation_project/validate_experiment.py" "$annotation_project/experiment.yaml" "$annotation_context" --master "$annotation_master"
elif [[ "${1:-}" == --command ]]; then
  "$annotation_cli" -m "$annotation_master" command run --config-file "$annotation_project/command.yaml" --context "$annotation_context" --detach bash start.sh
elif [[ "${1:-}" == --publish ]]; then
  echo "Opening the web service at http://localhost:8088 after scheduling. Keep this terminal open."
  "$annotation_cli" -m "$annotation_master" e create --follow-first-trial --publish 8088:8088 "$annotation_project/experiment.yaml" "$annotation_context"
else
  "$annotation_cli" -m "$annotation_master" e create "$annotation_project/experiment.yaml" "$annotation_context"
fi
