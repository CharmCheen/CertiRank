#!/usr/bin/env python3
"""Validate configs/api_config.json; optionally probe the provider with one tiny call.

Never required for offline reproduction. Does not write experiment artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from certirank.provider import ApiConfig, OpenAICompatibleClient, ProviderConfigError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "api_config.json")
    parser.add_argument("--probe", action="store_true", help="send one cheap chat completion")
    args = parser.parse_args()
    try:
        config = ApiConfig.load(args.config)
    except ProviderConfigError as exc:
        print(f"FAIL {exc}")
        return 1
    print("OK config loaded")
    print(json.dumps({
        "base_url": config.base_url,
        "model": config.model,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_tokens,
        "thinking_mode": config.thinking_mode,
        "api_key_env": config.api_key_env,
        "api_key_present": bool(config.api_key),
        "experiment_id": config.experiment_id,
        "condition": config.condition,
    }, ensure_ascii=False, indent=2))
    if not args.probe:
        print("offline config check only; rerun with --probe to contact the provider")
        return 0
    try:
        client = OpenAICompatibleClient(config)
        result = client.chat([{"role": "user", "content": "Reply with the single word: pong"}])
    except Exception as exc:
        print(f"FAIL probe: {exc}")
        return 1
    try:
        text = result["choices"][0]["message"]["content"]
    except Exception:
        text = json.dumps(result)[:200]
    print(f"PROBE_PASS {text!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
