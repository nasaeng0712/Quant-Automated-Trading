"""Settings loader (Core System Design v0.1, sections 12, 18, 36; Batch #2.0).

Cost / risk / net-alpha numbers live in ``config/settings.yaml``, not in code.

* ``load_settings()`` with no path keeps the legacy behaviour: the default file
  missing (or PyYAML missing) yields ``{}``.
* ``load_settings(path)`` with an explicit path raises when the file or PyYAML is
  missing - an explicitly requested configuration never degrades to "no config".
* ``load_research_settings(path)`` is the strict entry point for research runs:
  costs for every enabled market must be present and finite, so a missing
  configuration can never look like a zero-cost, no-limit research result.
  Zero-cost fixtures stay available through ``build_paper_stack`` defaults and
  are kept separate from research settings.
"""

from __future__ import annotations

import hashlib
import math
import pathlib

try:  # PyYAML is a declared dependency, but keep the import defensive
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from qat.cost.engine import CostModel
from qat.core.fx import StaticFXRateProvider
from qat.core.models import Currency, Market

_DEFAULT_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "settings.yaml"

_COST_FIELDS = ("commission_rate", "tax_rate_sell", "half_spread_bps", "slippage_bps", "fx_cost_bps")
_RISK_FIELDS = (
    "max_order_notional", "max_symbol_exposure", "max_market_exposure",
    "max_total_exposure", "daily_loss_limit", "max_drawdown",
)


class SettingsError(ValueError):
    """Raised when research settings are missing or invalid."""


def default_settings_path() -> pathlib.Path:
    return _DEFAULT_PATH


def load_settings(path: str | pathlib.Path | None = None) -> dict:
    if path is None:
        if yaml is None or not _DEFAULT_PATH.exists():
            return {}
        target = _DEFAULT_PATH
    else:
        target = pathlib.Path(path)
        if not target.exists():
            raise FileNotFoundError(f"settings file not found: {target}")
        if yaml is None:  # pragma: no cover - PyYAML is a declared dependency
            raise RuntimeError("PyYAML is required to read an explicit settings file")
    with target.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def settings_sha256(path: str | pathlib.Path | None = None) -> str:
    target = pathlib.Path(path) if path is not None else _DEFAULT_PATH
    return hashlib.sha256(target.read_bytes()).hexdigest()


def enabled_markets_from_settings(settings: dict) -> set[str] | None:
    markets = settings.get("markets")
    if not markets:
        return None
    return {str(name) for name, on in dict(markets).items() if on}


def _finite_non_negative(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{label} must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise SettingsError(f"{label} must be finite and >= 0, got {value!r}")
    return number


def cost_models_from_settings(settings: dict) -> dict[Market, CostModel]:
    out: dict[Market, CostModel] = {}
    for name, cfg in (settings.get("costs") or {}).items():
        try:
            market = Market(name)
        except ValueError:
            continue
        out[market] = CostModel(
            **{key: _finite_non_negative(value, f"costs.{name}.{key}") for key, value in dict(cfg).items()}
        )
    return out


def fx_provider_from_settings(settings: dict) -> StaticFXRateProvider:
    """Build a deterministic FX provider from ``fx.rates`` (a list of
    ``{from, to, rate}`` entries). Rates are placeholders and must be replaced
    with a live feed before any non-paper use (FIX-01)."""

    rates: dict[tuple[Currency, Currency], float] = {}
    for entry in ((settings.get("fx") or {}).get("rates") or []):
        rates[(Currency(entry["from"]), Currency(entry["to"]))] = float(entry["rate"])
    return StaticFXRateProvider(rates)


def load_research_settings(path: str | pathlib.Path | None = None) -> dict:
    """Strict settings for research. Returns the parsed dict plus
    ``_meta = {path, sha256, placeholder: True}``. Raises ``SettingsError`` /
    ``FileNotFoundError`` instead of degrading to an empty configuration."""

    target = pathlib.Path(path) if path is not None else _DEFAULT_PATH
    settings = load_settings(target)
    if not settings:
        raise SettingsError(f"research settings are empty: {target}")
    enabled = enabled_markets_from_settings(settings)
    costs = settings.get("costs") or {}
    required = enabled if enabled is not None else {m.value for m in Market}
    for market in sorted(required):
        cfg = costs.get(market)
        if not cfg:
            raise SettingsError(f"costs.{market} missing for enabled market {market}")
        for key in _COST_FIELDS:
            if key not in cfg:
                raise SettingsError(f"costs.{market}.{key} missing")
    cost_models_from_settings(settings)  # validates every number
    if "base_currency" not in settings:
        raise SettingsError("base_currency missing")
    Currency(settings["base_currency"])
    risk = settings.get("risk")
    if risk is None:
        raise SettingsError("risk section missing (use null values for 'not configured')")
    for key in _RISK_FIELDS:
        if key not in risk:
            raise SettingsError(f"risk.{key} missing (use null for 'not configured')")
        if risk[key] is not None:
            _finite_non_negative(risk[key], f"risk.{key}")
    universe = settings.get("paper_universe")
    if universe is not None:
        if not isinstance(universe, list) or not all(isinstance(x, str) and x.strip() for x in universe):
            raise SettingsError("paper_universe must be a list of non-empty symbol strings")
    out = dict(settings)
    out["_meta"] = {
        "path": str(target.resolve()),
        "sha256": settings_sha256(target),
        # every number in settings.yaml is a placeholder (design doc D-009)
        "placeholder": True,
    }
    return out
