"""Batch #3C - transaction-cost evidence classification (data, not behaviour).

Every cost parameter of ``config/settings.yaml`` is classified as

  VERIFIED             configured value matches a PRIMARY source for the whole research window
  EVIDENCE_SUPPORTED   a source supports the value or the structure, but not completely / not primary
  PLACEHOLDER          configured number without adequate support (kept as-is; never tuned)
  UNKNOWN              the real value cannot be determined without a user decision or data we do not have

Nothing is changed in the settings; Batch #3B results stay computed with the PLACEHOLDER numbers.
A new cost model belongs to a future protocol version only. ``validate`` is the guard: a status must
be one of the four, every settings cost field of every market must be classified, VERIFIED needs a
primary source plus an explicit statement that the configured value matches it.
Sources were retrieved on RETRIEVED; "primary" = regulator / exchange / the venue itself.
"""

from __future__ import annotations

from dataclasses import asdict

from qat.config import cost_models_from_settings, load_research_settings

RETRIEVED = "2026-10-06"
STATUSES = ("VERIFIED", "EVIDENCE_SUPPORTED", "PLACEHOLDER", "UNKNOWN")
COST_FIELDS = ("commission_rate", "tax_rate_sell", "half_spread_bps", "slippage_bps", "fx_cost_bps")

SEC31 = {"url": "https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2", "kind": "primary", "retrieved": RETRIEVED,
         "fact": "SEC Section 31 fee: $0.00 per million for covered sales through 2026-04-03, $20.60 per million from 2026-04-04 (FY2026); "
                 "the rate changes by fiscal year; earlier fiscal-year rates were not retrieved"}
FINRA31 = {"url": "https://www.finra.org/rules-guidance/notices/information-notice-20260317", "kind": "primary", "retrieved": RETRIEVED,
           "fact": "FINRA notice confirms the Section 31 rate change ($0.00 -> $20.60 per million on 2026-04-04); it does not state the TAF"}
TAF = {"url": "https://help.revolut.com/en-IT/help/wealth/order-execution-fees-and-limits/trading-regulatory-fees", "kind": "secondary",
       "retrieved": RETRIEVED, "fact": "a broker help page quotes the FINRA TAF for stock sales from 2026-01-01 as $0.000195/share, min $0.01, max $9.79 per trade; "
       "the FINRA rule text was not retrieved"}
KR_TAX = {"url": "https://www.taxtimes.co.kr/mobile/article.html?no=272624", "kind": "secondary", "retrieved": RETRIEVED,
          "fact": "news report of the 2025 tax revision: from 2026-01-01 KOSPI securities transaction tax 0.05% (+0.15% rural-development special tax kept = 0.20% total), "
                  "KOSDAQ 0.15% -> 0.20%; the law text (증권거래세법 시행령) was not retrievable here"}
KR_TAX_HISTORY = {"url": "https://newstomato.com/ReadNews.aspx?no=980180", "kind": "secondary", "retrieved": RETRIEVED,
                  "fact": "search results report KOSPI tax 0.05% (2023), 0.03% (2024), 0% (2025) plus the 0.15% special tax, KOSDAQ 0.20% / 0.18% / 0.15%; "
                          "2020-2022 rates were not confirmed from a primary source"}
BINANCE = {"url": "https://www.binance.com/en/fee/schedule", "kind": "primary", "retrieved": RETRIEVED,
           "fact": "Binance spot, regular user (VIP 0): maker 0.100% / taker 0.100%; 0.075% / 0.075% with the BNB discount. This is the CURRENT schedule only; "
                   "the 2018-2025 history and the user's real tier/discount are not verified"}
BINANCE_ARCHIVE = {"url": "https://data.binance.vision/", "kind": "primary", "retrieved": RETRIEVED,
                   "fact": "public spot archive lists klines (1s..1mo), aggTrades and trades for BTCUSDT; no quote / order-book (bid-ask) data type for spot"}


def _e(status, why, sources=(), *, matches=False, dependency=None, schedule=None):
    return {"status": status, "why": why, "sources": list(sources), "matches_primary_source": matches,
            "dependency": dependency, "schedule_evidence": schedule}


EVIDENCE = {
    "KR": {
        "commission_rate": _e("PLACEHOLDER", "broker/account specific; no broker is chosen", dependency="user decision: broker, account, fee tier"),
        "tax_rate_sell": _e("PLACEHOLDER", "a single constant (0.18%) is applied to 2020-2025 although the official rate changes by year; the constant equals the "
                            "reported 2024 total only", [KR_TAX, KR_TAX_HISTORY], schedule="EVIDENCE_SUPPORTED (secondary sources only; law text not retrieved)"),
        "half_spread_bps": _e("PLACEHOLDER", "bid-ask spread is not in daily OHLCV and no quote data was acquired; FACT: unavailable"),
        "slippage_bps": _e("PLACEHOLDER", "market impact / slippage cannot be measured from daily bars; FACT: unavailable"),
        "fx_cost_bps": _e("EVIDENCE_SUPPORTED", "KRW-denominated asset in a KRW base currency: no currency conversion is involved (structural)"),
    },
    "US": {
        "commission_rate": _e("PLACEHOLDER", "broker specific; many brokers charge no commission; none chosen", dependency="user decision: broker, account"),
        "tax_rate_sell": _e("PLACEHOLDER", "configured 0.0, but an SEC Section 31 sell-side fee (rate varies by fiscal year) and a FINRA TAF exist; both are small "
                            "per trade and were not modelled; only the FY2026 rates were retrieved", [SEC31, FINRA31, TAF],
                            schedule="EVIDENCE_SUPPORTED (SEC primary for FY2026 only; TAF secondary)"),
        "half_spread_bps": _e("PLACEHOLDER", "no quote data acquired; FACT: unavailable"),
        "slippage_bps": _e("PLACEHOLDER", "not measurable from daily bars; FACT: unavailable"),
        "fx_cost_bps": _e("PLACEHOLDER", "USD->KRW conversion cost is broker specific (and the FX rate itself is a placeholder)", dependency="user decision: broker FX terms"),
    },
    "CRYPTO": {
        "commission_rate": _e("PLACEHOLDER", "configured 0.05% is LOWER than the venue's current standard fee (0.10%, 0.075% with BNB): it understates cost if "
                              "the user's tier is the standard one", [BINANCE], dependency="user decision: venue and fee tier (VIP level, BNB discount)",
                              schedule="EVIDENCE_SUPPORTED for the current Binance VIP0 schedule only"),
        "tax_rate_sell": _e("UNKNOWN", "no venue transaction tax on sales is known; tax treatment of crypto gains is jurisdiction/user dependent and is not a trading cost here",
                            dependency="user decision: tax jurisdiction"),
        "half_spread_bps": _e("PLACEHOLDER", "the public spot archive has no bid-ask quote data; FACT: unavailable", [BINANCE_ARCHIVE]),
        "slippage_bps": _e("PLACEHOLDER", "not measurable from klines; trade-level data exists (aggTrades/trades) but order sizes/impact are unknown; FACT: unavailable",
                           [BINANCE_ARCHIVE]),
        "fx_cost_bps": _e("PLACEHOLDER", "USDT->KRW base conversion uses a placeholder rate; configured 0 is not evidence", dependency="user decision: how USDT is converted"),
    },
}


class CostEvidenceError(ValueError):
    pass


def configured_costs(settings_path=None) -> dict:
    models = cost_models_from_settings(load_research_settings(settings_path))
    return {m.value: asdict(cm) for m, cm in models.items()}


def validate(evidence: dict, configured: dict) -> None:
    """Raise ``CostEvidenceError`` if the classification is not self-consistent."""

    for market, fields in configured.items():
        if market not in evidence:
            raise CostEvidenceError(f"market {market} has no cost evidence")
        for name in COST_FIELDS:
            if name not in configured[market]:
                continue
            entry = evidence[market].get(name)
            if entry is None:
                raise CostEvidenceError(f"{market}.{name} is not classified")
            if entry["status"] not in STATUSES:
                raise CostEvidenceError(f"{market}.{name}: invalid status {entry['status']!r}")
            primary = [s for s in entry["sources"] if s.get("kind") == "primary"]
            if entry["status"] == "VERIFIED" and not (primary and entry["matches_primary_source"]):
                raise CostEvidenceError(f"{market}.{name}: VERIFIED needs a primary source and an explicit match")
            if entry["status"] in ("PLACEHOLDER", "UNKNOWN") and entry["matches_primary_source"]:
                raise CostEvidenceError(f"{market}.{name}: a {entry['status']} value cannot claim to match a primary source")
            if entry["status"] == "EVIDENCE_SUPPORTED" and not entry["why"]:
                raise CostEvidenceError(f"{market}.{name}: EVIDENCE_SUPPORTED needs a stated basis")
    for market in evidence:
        if market not in configured:
            raise CostEvidenceError(f"evidence for unknown market {market}")


def report(settings_path=None) -> dict:
    configured = configured_costs(settings_path)
    validate(EVIDENCE, configured)
    rows = {m: {f: {"configured": configured[m][f], **EVIDENCE[m][f]} for f in COST_FIELDS if f in configured[m]} for m in configured}
    counts = {s: sum(1 for m in rows.values() for e in m.values() if e["status"] == s) for s in STATUSES}
    return {"retrieved": RETRIEVED, "status_counts": counts, "markets": rows,
            "note": "Batch #3B used these numbers unchanged; none is VERIFIED. Costs are NOT tuned. A new cost model is for a future protocol version only."}


# ====================================================================== Batch #3D: Protocol v2 cost contract (BTCUSDT, Binance Spot)
# Separate vocabulary and structure; the Batch #3C classification above is frozen and untouched.
V2_STATUSES = ("VERIFIED_CURRENT", "HISTORICAL_UNKNOWN", "PLACEHOLDER", "SCENARIO")
V2_VENUE = "Binance Spot BTCUSDT"
V2_TIER = "Regular User, no BNB discount"
BINANCE_FEE_2 = {"url": "https://www.binance.com/en/fee/schedule", "kind": "primary", "retrieved": RETRIEVED,
                 "fact": "Regular User spot maker 0.100% / taker 0.100%; 0.075% / 0.075% when paying with BNB (not applied here); the page shows no fee history or effective dates"}
SCENARIO_LABEL = "SCENARIO ASSUMPTION - not an observed cost; round numbers fixed before any Protocol v2 result, not derived from data or performance"


def _v2(status, scope, value, why, sources=(), **extra):
    return {"status": status, "scope": scope, "value": value, "why": why, "sources": list(sources), **extra}


V2_CONTRACT = {
    "venue": V2_VENUE, "fee_tier": V2_TIER, "interval": "4h",
    "commission_maker_current": _v2("VERIFIED_CURRENT", "current", 0.001, "Binance fee schedule, Regular User", [BINANCE_FEE_2]),
    "commission_taker_current": _v2("VERIFIED_CURRENT", "current", 0.001, "Binance fee schedule, Regular User", [BINANCE_FEE_2]),
    "commission_historical_2018_2025": _v2("HISTORICAL_UNKNOWN", "historical", None, "no official fee history was retrieved; the current 0.100% does not prove "
                                           "what was charged in 2018-2025", research_assumption="ASSUMED_FOR_RESEARCH: the current 0.100% is applied to every historical bar and "
                                           "labelled as an assumption, never as a verified historical fee"),
    "bid_ask_spread_historical": _v2("HISTORICAL_UNKNOWN", "historical", None, "no historical quote data was acquired; klines/aggTrades/trades do not contain bid or ask (FACT: unavailable)"),
    "slippage_market_impact_historical": _v2("HISTORICAL_UNKNOWN", "historical", None, "cannot be observed from klines or trade prints (FACT: unavailable)"),
    "fx_cost": _v2("PLACEHOLDER", "structural", 0.0, "v2 is evaluated in USDT (single currency): no conversion is modelled; this is a structural zero, not evidence"),
    "tax_on_sale": _v2("PLACEHOLDER", "structural", 0.0, "not a venue trading cost; jurisdiction dependent; not modelled"),
}
V2_SCENARIOS = {
    "S0_commission_only": {"half_spread_bps": 0.0, "slippage_bps": 0.0},
    "S1_low": {"half_spread_bps": 1.0, "slippage_bps": 1.0},
    "S2_mid": {"half_spread_bps": 2.5, "slippage_bps": 2.5},
    "S3_high": {"half_spread_bps": 5.0, "slippage_bps": 5.0},
}


def scenario_cost_models() -> dict:
    """Engine-style cost parameters per scenario: VERIFIED_CURRENT commission + explicitly labelled SCENARIO spread/slippage."""

    out = {}
    for name, s in V2_SCENARIOS.items():
        out[name] = {"commission_rate": V2_CONTRACT["commission_taker_current"]["value"], "tax_rate_sell": 0.0, "fx_cost_bps": 0.0, **s,
                     "status": {"commission_rate": "VERIFIED_CURRENT", "half_spread_bps": "SCENARIO", "slippage_bps": "SCENARIO"},
                     "label": SCENARIO_LABEL, "result_label": "scenario result, NOT actual-cost performance"}
    return out


def validate_v2(contract: dict = V2_CONTRACT, scenarios: dict = V2_SCENARIOS) -> None:
    for key, entry in contract.items():
        if not isinstance(entry, dict) or "status" not in entry:
            continue
        if entry["status"] not in V2_STATUSES:
            raise CostEvidenceError(f"{key}: invalid status {entry['status']!r}")
        if entry["status"] == "VERIFIED_CURRENT":
            if entry["scope"] != "current" or not any(s.get("kind") == "primary" for s in entry["sources"]):
                raise CostEvidenceError(f"{key}: VERIFIED_CURRENT needs scope 'current' and a primary source")
        if entry["scope"] == "historical" and entry["status"] not in ("HISTORICAL_UNKNOWN", "SCENARIO"):
            raise CostEvidenceError(f"{key}: a historical value cannot be {entry['status']} without a historical source")
        if entry["status"] == "HISTORICAL_UNKNOWN" and entry["value"] is not None:
            raise CostEvidenceError(f"{key}: HISTORICAL_UNKNOWN must not carry a value")
    if any("SCENARIO ASSUMPTION" not in SCENARIO_LABEL for _ in scenarios):
        raise CostEvidenceError("scenarios must be labelled SCENARIO ASSUMPTION")


def v2_report() -> dict:
    validate_v2()
    return {"contract": V2_CONTRACT, "scenarios": scenario_cost_models(), "manifest_block": {
        "current_verified_fee": {"venue": V2_VENUE, "tier": V2_TIER, "maker": 0.001, "taker": 0.001, "status": "VERIFIED_CURRENT", "retrieved": RETRIEVED},
        "historical_fee_assumption": V2_CONTRACT["commission_historical_2018_2025"],
        "spread": V2_CONTRACT["bid_ask_spread_historical"], "slippage": V2_CONTRACT["slippage_market_impact_historical"],
        "scenarios": "see scenarios (SCENARIO ASSUMPTION)"},
        "frozen_3c_untouched": True, "not_applied": "no scenario or fee was used to run any strategy in Batch #3D"}
