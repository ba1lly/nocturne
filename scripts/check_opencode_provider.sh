#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

mapfile -t MODEL_LINES < <(
  "${PYTHON_BIN}" -c '
import os
import yaml
from types import SimpleNamespace
from nocturne.config import ConfigError, load_config, load_test_config

path = "config.yaml" if os.path.exists("config.yaml") else "config.example.yaml"
try:
    if path == "config.example.yaml":
        c = load_test_config(path)
    else:
        c = load_config(path)
except ConfigError:
    raw = yaml.safe_load(open(path)) or {}
    models = raw.get("models", {})
    providers = raw.get("providers", {})
    class _Cfg:
        pass
    c = _Cfg()
    c.models = _Cfg()
    c.providers = {name: SimpleNamespace(**value) for name, value in providers.items()}
    c.models.reasoning = models["reasoning"]
    c.models.coding = models["coding"]
    c.models.report = models["report"]
for model in (c.models.reasoning, c.models.coding, c.models.report):
    provider = model.split("/", 1)[0]
    provider_cfg = c.providers[provider]
    print("\t".join([model, provider, provider_cfg.base_url, provider_cfg.api_key_env]))
'
)

listing_file="/tmp/opencode-models-listing.txt"
set +e
opencode models 2>&1 | tee "${listing_file}"
opencode_status=${PIPESTATUS[0]}
set -e

missing_count=0
for line in "${MODEL_LINES[@]}"; do
  IFS=$'\t' read -r model provider base_url api_key_env <<<"${line}"
  if grep -Fqx "${model}" "${listing_file}"; then
    continue
  fi

  missing_count=$((missing_count + 1))
  {
    printf 'MISSING: %s\n' "${model}"
    printf 'Add to ~/.config/opencode/opencode.jsonc:\n'
    printf '"provider": {\n'
    printf '  "%s": {\n' "${provider}"
    printf '    "options": {\n'
    printf '      "baseURL": "%s",\n' "${base_url}"
    printf '      "apiKey": "{env:%s}"\n' "${api_key_env}"
    printf '    }\n'
    printf '  }\n'
    printf '}\n'
  } >&2
done

if [[ ${missing_count} -eq 0 ]]; then
  echo "OK"
  exit 0
fi

if [[ ${opencode_status} -ne 0 ]]; then
  printf 'NOTE: opencode models exited %s; catalog treated as unavailable\n' "${opencode_status}" >&2
fi

exit "${missing_count}"
