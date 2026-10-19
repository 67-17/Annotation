#!/usr/bin/env bash
set -euo pipefail
annotation_project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
annotation_token_file="${ANNOTATION_STATE_DIR:-$annotation_project/state}/reload-token"
annotation_url="${ANNOTATION_URL:-http://127.0.0.1:8088}"
if [[ ! -s "$annotation_token_file" ]]; then
  echo "Reload token not found: $annotation_token_file. Deploy the manual-reload version once first." >&2
  exit 1
fi
curl --noproxy '*' --fail --show-error --silent -X POST \
  -H "X-Annotation-Reload-Token: $(<"$annotation_token_file")" \
  "$annotation_url/api/admin/reload"
echo
echo "Reload requested. Wait for MODEL_SMOKE_OK, then refresh the browser."
