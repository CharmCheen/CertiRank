"""API config validation and temperature wire semantics."""

import json
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from certirank.provider import ApiConfig, ProviderConfigError


def base():
    return {
        "base_url": "https://api.example.com/v1/",
        "model": "demo-model",
        "api_key": "sk-test",
        "temperature": 0.2,
        "max_tokens": 64,
    }


def test_load_example_template_and_roundtrip(tmp_path):
    example = (ROOT / "configs" / "api_config.example.json").read_text(encoding="utf-8")
    path = tmp_path / "api_config.json"
    path.write_text(example, encoding="utf-8")
    config = ApiConfig.load(path)
    assert config.base_url.endswith("/v1") or config.base_url.startswith("https://")
    assert config.temperature == 0.0
    assert config.model


def test_single_file_exposes_url_model_key_and_temperature(tmp_path):
    path = tmp_path / "api_config.json"
    path.write_text(json.dumps({
        "base_url": "https://llm.example.cn/v1",
        "model": "my-model",
        "api_key": "sk-abc",
        "temperature": 0.7,
        "top_p": 0.9,
    }), encoding="utf-8")
    config = ApiConfig.load(path)
    assert config.base_url == "https://llm.example.cn/v1"
    assert config.model == "my-model"
    assert config.api_key == "sk-abc"
    assert config.temperature == 0.7
    assert config.request_body_extras()["temperature"] == 0.7
    assert config.model_parameters()["temperature_requested"] == 0.7


def test_temperature_null_omits_wire_field():
    config = ApiConfig.from_dict({**base(), "temperature": None})
    assert config.temperature is None
    assert "temperature" not in config.request_body_extras()
    assert config.model_parameters()["temperature_requested"] is None


def test_temperature_bounds_rejected():
    for bad in (-0.1, 2.1, "0.3", True):
        with pytest.raises(ProviderConfigError):
            ApiConfig.from_dict({**base(), "temperature": bad})


def test_thinking_modes_map_to_wire_fields():
    low = ApiConfig.from_dict({**base(), "thinking_mode": "low"})
    assert low.reasoning_effort == "low"
    disabled = ApiConfig.from_dict({**base(), "thinking_mode": "disabled"})
    assert disabled.reasoning_effort is None
    assert disabled.thinking_mode == "disabled"
    with pytest.raises(ProviderConfigError):
        ApiConfig.from_dict({**base(), "thinking_mode": "nope"})
    with pytest.raises(ProviderConfigError):
        ApiConfig.from_dict({**base(), "thinking_mode": "low", "reasoning_effort": "high"})
    assert ApiConfig.from_dict({**base(), "thinking_mode": "low"}).model_parameters()["reasoning_effort_requested"] == "low"


def test_api_key_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("CERTIRANK_API_KEY", "sk-from-env")
    config = ApiConfig.from_dict({**base(), "api_key": "", "api_key_env": "CERTIRANK_API_KEY"})
    assert config.api_key == "sk-from-env"
    monkeypatch.delenv("CERTIRANK_API_KEY", raising=False)
    config2 = ApiConfig.from_dict({**base(), "api_key": "", "api_key_env": "MISSING_KEY_VAR"})
    assert config2.api_key == ""
    with pytest.raises(ProviderConfigError):
        config2.require_api_key()


def test_missing_or_invalid_fields_rejected():
    with pytest.raises(ProviderConfigError):
        ApiConfig.from_dict({})
    with pytest.raises(ProviderConfigError):
        ApiConfig.from_dict({**base(), "base_url": "ftp://x"})
    with pytest.raises(ProviderConfigError):
        ApiConfig.from_dict({**base(), "max_tokens": 0})
