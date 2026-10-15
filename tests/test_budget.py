"""Budget artifacts must disclose assumptions instead of claiming a live cap."""

import importlib.util
import json
from pathlib import Path


def test_scenarios_keep_live_cap_pending_and_expose_cache_sensitivity():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("pilot_budget", root / "scripts/pilot_budget.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    artifacts = module.build_artifacts()
    report = json.loads(artifacts["budget_estimate.json"])
    assert report["live_hard_cap"] is None
    assert report["price_validation"]["max_abs_err_usd"] < 1e-5
    high_cached = next(r for r in report["blocks"] if r["calls"] == 6048)["cost_usd"]["peak"]["high"]
    assert report["stress_scenarios"][0]["core_usd_historical_price"] > high_cached * 4
    assert "最坏约" not in artifacts["budget_estimate.md"]
