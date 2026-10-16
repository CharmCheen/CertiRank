"""OpenAI-compatible live provider config and client.

The only user-edited file is ``configs/api_config.json`` (see the example next to it).
Secrets never belong in the repository: put the key in that local file or in an
environment variable named by ``api_key_env``. Temperature is explicit — a number
sends the field, ``null`` omits it (historical P0 wire semantics).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from certirank.pair_pool import parse_comparison_answer

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "configs" / "api_config.json"

REQUIRED_KEYS = ("base_url", "model")
OPTIONAL_DEFAULTS = {
    "provider": "openai_compatible",
    "api_key": "",
    "api_key_env": "CERTIRANK_API_KEY",
    "temperature": None,
    "top_p": None,
    "presence_penalty": None,
    "frequency_penalty": None,
    "max_tokens": 128,
    "timeout_seconds": 120,
    "max_retries": 3,
    "thinking_mode": "low",
    "reasoning_effort": None,
    "experiment_id": "CertiRank|live",
    "condition": "live",
}


class ProviderConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ApiConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    api_key_env: str
    temperature: float | None
    top_p: float | None
    presence_penalty: float | None
    frequency_penalty: float | None
    max_tokens: int
    timeout_seconds: float
    max_retries: int
    thinking_mode: str | None
    reasoning_effort: str | None
    experiment_id: str
    condition: str

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ApiConfig":
        config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        if not config_path.is_file():
            raise ProviderConfigError(
                f"missing API config: {config_path} (copy configs/api_config.example.json and edit it)"
            )
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderConfigError(f"invalid JSON in {config_path}: {exc}") from exc
        return cls.from_dict(raw, source=str(config_path))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source: str = "api_config") -> "ApiConfig":
        if not isinstance(raw, dict):
            raise ProviderConfigError(f"{source}: config must be a JSON object")
        data = {**OPTIONAL_DEFAULTS, **raw}
        for key in REQUIRED_KEYS:
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ProviderConfigError(f"{source}: '{key}' must be a nonempty string")
        base_url = data["base_url"].rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ProviderConfigError(f"{source}: base_url must be http(s)")
        model = data["model"].strip()
        temperature = _optional_number(data["temperature"], "temperature", source, minimum=0.0, maximum=2.0)
        top_p = _optional_number(data["top_p"], "top_p", source, minimum=0.0, maximum=1.0)
        presence_penalty = _optional_number(data["presence_penalty"], "presence_penalty", source, minimum=-2.0, maximum=2.0)
        frequency_penalty = _optional_number(data["frequency_penalty"], "frequency_penalty", source, minimum=-2.0, maximum=2.0)
        if type(data["max_tokens"]) is not int or data["max_tokens"] < 1:
            raise ProviderConfigError(f"{source}: max_tokens must be a positive integer")
        if not isinstance(data["timeout_seconds"], (int, float)) or data["timeout_seconds"] <= 0:
            raise ProviderConfigError(f"{source}: timeout_seconds must be positive")
        if type(data["max_retries"]) is not int or data["max_retries"] < 0:
            raise ProviderConfigError(f"{source}: max_retries must be a nonnegative integer")
        thinking = data["thinking_mode"]
        if thinking is not None and thinking not in ("disabled", "enabled", "low", "high", "max"):
            raise ProviderConfigError(
                f"{source}: thinking_mode must be null, disabled, enabled, low, high, or max"
            )
        reasoning = data["reasoning_effort"]
        if reasoning is not None and reasoning not in ("low", "high", "max"):
            raise ProviderConfigError(f"{source}: reasoning_effort must be low, high, max, or null")
        if thinking in ("low", "high", "max") and reasoning is not None and reasoning != thinking:
            raise ProviderConfigError(f"{source}: reasoning_effort conflicts with thinking_mode={thinking}")
        api_key = data.get("api_key") or ""
        api_key_env = str(data.get("api_key_env") or "CERTIRANK_API_KEY")
        if not isinstance(api_key, str):
            raise ProviderConfigError(f"{source}: api_key must be a string")
        resolved = api_key.strip() or os.environ.get(api_key_env, "").strip()
        return cls(
            provider=str(data["provider"] or "openai_compatible"),
            base_url=base_url,
            model=model,
            api_key=resolved,
            api_key_env=api_key_env,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            max_tokens=data["max_tokens"],
            timeout_seconds=float(data["timeout_seconds"]),
            max_retries=data["max_retries"],
            thinking_mode=thinking,
            reasoning_effort=reasoning if reasoning is not None else (thinking if thinking in ("low", "high", "max") else None),
            experiment_id=str(data.get("experiment_id") or OPTIONAL_DEFAULTS["experiment_id"]),
            condition=str(data.get("condition") or OPTIONAL_DEFAULTS["condition"]),
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ProviderConfigError(
                f"missing API key: set 'api_key' in {DEFAULT_CONFIG_PATH.name} or export {self.api_key_env}"
            )
        return self.api_key

    def model_parameters(self) -> dict[str, Any]:
        """Recorded request identity for traces; null means the field is omitted on the wire."""
        return {
            "provider": self.provider,
            "base_url_host": urllib.parse.urlparse(self.base_url).netloc,
            "temperature_requested": self.temperature,
            "top_p_requested": self.top_p,
            "presence_penalty_requested": self.presence_penalty,
            "frequency_penalty_requested": self.frequency_penalty,
            "max_tokens_requested": self.max_tokens,
            "thinking_mode_requested": self.thinking_mode,
            "reasoning_effort_requested": self.reasoning_effort,
        }

    def request_body_extras(self) -> dict[str, Any]:
        """Sampling fields actually sent; null values are omitted."""
        extras: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens}
        for key, value in (
            ("temperature", self.temperature),
            ("top_p", self.top_p),
            ("presence_penalty", self.presence_penalty),
            ("frequency_penalty", self.frequency_penalty),
        ):
            if value is not None:
                extras[key] = value
        return extras


class OpenAICompatibleClient:
    """Minimal stdlib chat-completions client. Offline paths never construct this."""

    def __init__(self, config: ApiConfig):
        self.config = config

    def chat(self, messages: list[dict[str, str]], *, timeout: float | None = None) -> dict[str, Any]:
        api_key = self.config.require_api_key()
        payload = {
            "messages": messages,
            **self.config.request_body_extras(),
        }
        effort = self.config.reasoning_effort
        if effort in ("low", "high", "max"):
            # GLM / OpenRouter style. Do not also send thinking.type=disabled.
            payload["reasoning_effort"] = effort
        elif self.config.thinking_mode == "enabled":
            payload["thinking"] = {"type": "enabled"}
        elif self.config.thinking_mode == "disabled":
            payload["thinking"] = {"type": "disabled"}
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.config.base_url + "/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        last_error: Exception | None = None
        attempts = self.config.max_retries + 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.config.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"provider HTTP {exc.code}: {detail}")
                if exc.code in (400, 401, 403, 404):
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = RuntimeError(f"provider request failed: {exc}")
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 8))
        raise last_error if last_error is not None else RuntimeError("provider request failed")

    def compare_pair(self, query: str, doc_a: str, doc_b: str, *, parse_retries: int = 3) -> dict[str, Any]:
        """One binary preference call. Prompt is intentionally minimal and versioned in identity."""
        prompt = (
            "Which document is more relevant to the query?\n"
            f"Query: {query}\n"
            f"Document 1: {doc_a}\n"
            f"Document 2: {doc_b}\n"
            'Answer with exactly "Document 1" or "Document 2".'
        )
        last_text = ""
        last_error: Exception | None = None
        for attempt in range(parse_retries + 1):
            response = self.chat([{"role": "user", "content": prompt}])
            text = ""
            try:
                text = response["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                text = ""
            last_text = text
            verdict, status = parse_comparison_answer(text)
            if status == "valid":
                return {
                    "raw_response_content": text,
                    "strict_parsed_decision": verdict,
                    "strict_parser_status": status,
                    "model": self.config.model,
                    "model_parameters": self.config.model_parameters(),
                    "usage": response.get("usage"),
                    "provider_request_id": response.get("id"),
                }
            last_error = ValueError(f"unparseable comparison answer: {text!r}")
            if attempt < parse_retries:
                time.sleep(min(2 ** attempt, 8))
        raise last_error if last_error is not None else ValueError(f"unparseable comparison answer: {last_text!r}")


def _optional_number(value: Any, name: str, source: str, *, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderConfigError(f"{source}: {name} must be a number or null")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ProviderConfigError(f"{source}: {name} must be in [{minimum}, {maximum}]")
    return number
