from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from typing import cast

from openai import OpenAI

from nocturne.config import Config, provider_of


class ProviderNotRegistered(Exception):
    pass


_OPENCODE_MODEL_PATTERN = re.compile(r"^[a-z][a-z0-9-]*/.+$")


def list_opencode_models() -> set[str]:
    try:
        completed = subprocess.run(
            ["opencode", "models"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return set()

    if completed.returncode != 0:
        return set()

    models: set[str] = set()
    for line in completed.stdout.splitlines():
        candidate = line.strip()
        if _OPENCODE_MODEL_PATTERN.fullmatch(candidate):
            models.add(candidate)
    return models


def check_all_models_available(cfg: Config) -> None:
    required: set[str] = {cfg.models.reasoning, cfg.models.report}
    if cfg.models.coding is not None:
        required.add(cfg.models.coding)
    else:
        return
    available = list_opencode_models()
    if not available:
        return
    missing = required - available
    if missing:
        providers_missing = {provider_of(model) for model in missing}
        raise ProviderNotRegistered(
            f"Models not registered with OpenCode: {sorted(missing)}\n"
            + f"Providers needing registration: {sorted(providers_missing)}\n"
            + "Each provider in cfg.providers must be registered in ~/.config/opencode/opencode.jsonc; "
            + "run scripts/check_opencode_provider.sh for snippets."
        )


def assert_provider_registered(provider_name: str) -> None:
    available = list_opencode_models()
    if any(model.startswith(f"{provider_name}/") for model in available):
        return
    raise ProviderNotRegistered(
        f"Provider not registered with OpenCode: {provider_name}\n"
        + "Each provider in cfg.providers must be registered in ~/.config/opencode/opencode.jsonc; "
        + "run scripts/check_opencode_provider.sh for snippets."
    )


def _configured_models_for_provider(cfg: Config, provider_name: str) -> list[str]:
    candidates = [cfg.models.reasoning, cfg.models.report]
    if cfg.models.coding is not None:
        candidates.append(cfg.models.coding)
    return [model for model in candidates if provider_of(model) == provider_name]


def _provider_model_ids(response: object) -> set[str]:
    data = cast(Iterable[object], getattr(response, "data", ()))
    model_ids: set[str] = set()
    for item in data:
        model_id = getattr(item, "id", None)
        if isinstance(model_id, str) and model_id:
            model_ids.add(model_id)
    return model_ids


def check_models_via_provider_api(cfg: Config) -> list[str]:
    warnings: list[str] = []
    for provider_name, provider_cfg in cfg.providers.items():
        api_key = os.environ.get(provider_cfg.api_key_env)
        if not api_key:
            continue
        try:
            client = OpenAI(base_url=provider_cfg.base_url, api_key=api_key)
            available_models = _provider_model_ids(client.models.list())
        except Exception:
            continue

        configured_models = _configured_models_for_provider(cfg, provider_name)
        for model in configured_models:
            if model not in available_models:
                warnings.append(
                    f"WARNING: {provider_name} responded at {provider_cfg.base_url} but missing model {model}"
                )
    return warnings
