"""Adjustment semantics (Batch #3A, Phase 7).

States: UNADJUSTED | SPLIT_ADJUSTED | DIVIDEND_ADJUSTED | TOTAL_RETURN_ADJUSTED | UNKNOWN.

The record keeps three things apart:
  * ``declared``   - what the provider's OFFICIAL documentation says, with the document
                     identity (empty unless a document was actually retrieved and recorded);
  * ``observed``   - what the data itself shows around reference corporate actions
                     (price continuity across an announced split ex-date) - EVIDENCE, not
                     a verdict;
  * ``final``      - the judgment. It is only ever a concrete state when it rests on an
                     official provider statement OR on an official corporate-action record
                     that the data is *tested against* and that yields an unambiguous
                     result. Otherwise it stays UNKNOWN and the dataset cannot be admitted.

Price patterns alone never decide: observation is always paired with a reference
corporate action whose terms (ex-date, ratio) come from the issuer, not from the series.
"""

from __future__ import annotations

import math
from datetime import date

STATES = ("UNADJUSTED", "SPLIT_ADJUSTED", "DIVIDEND_ADJUSTED", "TOTAL_RETURN_ADJUSTED", "UNKNOWN")
USABLE_STATES = ("UNADJUSTED", "SPLIT_ADJUSTED", "DIVIDEND_ADJUSTED", "TOTAL_RETURN_ADJUSTED")

# Reference corporate actions. SOURCE KIND is stated honestly: these are issuer-announced
# events recorded here as reference facts for testing the data; they were not fetched
# programmatically in this repository.
CORPORATE_ACTIONS = [
    {"market": "KR", "symbol": "005930", "type": "SPLIT", "ex_date": "2018-05-04", "ratio": 50.0,
     "description": "Samsung Electronics 50-for-1 par-value (stock) split; trading resumed 2018-05-04",
     "source": "issuer disclosure (Samsung Electronics, 2018 stock split) - recorded reference, not machine-fetched"},
    {"market": "US", "symbol": "AAPL", "type": "SPLIT", "ex_date": "2020-08-31", "ratio": 4.0,
     "description": "Apple 4-for-1 stock split, first split-adjusted trading day 2020-08-31",
     "source": "issuer press release (Apple Inc., 2020-07-30) - recorded reference, not machine-fetched"},
]


def actions_for(market: str, symbol: str) -> list[dict]:
    return [a for a in CORPORATE_ACTIONS if a["market"] == market and a["symbol"] == symbol]


def split_observation(rows: list[dict], action: dict) -> dict:
    """Close-to-close continuity across a split ex-date.

    ``ratio`` = first close on/after the ex-date divided by the last close before it.
    UNADJUSTED data jumps by ~1/split ratio; split-adjusted data stays continuous."""

    ex = date.fromisoformat(action["ex_date"]).isoformat()
    before = [r for r in rows if r["date"] < ex and r.get("close") is not None and math.isfinite(r["close"])]
    after = [r for r in rows if r["date"] >= ex and r.get("close") is not None and math.isfinite(r["close"])]
    if not before or not after:
        return {"ex_date": ex, "ratio_declared": action["ratio"], "verdict": "NOT_IN_RANGE",
                "detail": "series does not contain rows on both sides of the ex-date"}
    last_before, first_after = before[-1], after[0]
    if last_before["close"] <= 0:
        return {"ex_date": ex, "ratio_declared": action["ratio"], "verdict": "INCONCLUSIVE", "detail": "non-positive close"}
    jump = first_after["close"] / last_before["close"]
    expected = 1.0 / action["ratio"]
    if abs(math.log(jump) - math.log(expected)) < 0.35:
        verdict = "UNADJUSTED_AT_EVENT"
    elif abs(math.log(jump)) < 0.35:
        verdict = "ADJUSTED_AT_EVENT"
    else:
        verdict = "INCONCLUSIVE"
    return {"ex_date": ex, "ratio_declared": action["ratio"], "verdict": verdict,
            "close_before": last_before["close"], "date_before": last_before["date"],
            "close_after": first_after["close"], "date_after": first_after["date"],
            "close_jump_ratio": jump, "unadjusted_expected_ratio": expected}


def detect_discontinuities(rows: list[dict], threshold: float = 0.35) -> list[dict]:
    """Data-driven: overnight close-to-close moves beyond ``threshold`` (|ln ratio|)."""

    out, prev = [], None
    for row in rows:
        close = row.get("close")
        if close is None or not math.isfinite(close) or close <= 0:
            continue
        if prev is not None and abs(math.log(close / prev["close"])) > threshold:
            out.append({"date": row["date"], "prev_date": prev["date"], "prev_close": prev["close"], "close": close,
                        "ratio": close / prev["close"]})
        prev = row
    return out


def assess(rows: list[dict], *, market: str, symbol: str, declared: dict | None, dividend_evidence: dict | None = None,
           base_price: dict | None = None) -> dict:
    """Build the adjustment record. ``declared`` = {"state": ..., "document": ..., "basis": ...}
    from OFFICIAL documentation, or None when no official statement was retrieved."""

    declared = declared or {"state": "UNKNOWN", "document": None, "basis": "no official provider statement retrieved"}
    events = [split_observation(rows, a) for a in actions_for(market, symbol)]
    in_range = [e for e in events if e["verdict"] != "NOT_IN_RANGE"]
    verdicts = {e["verdict"] for e in in_range}
    observed_state = "UNKNOWN"
    if in_range and verdicts == {"UNADJUSTED_AT_EVENT"}:
        observed_state = "UNADJUSTED"
    elif in_range and verdicts == {"ADJUSTED_AT_EVENT"}:
        observed_state = "SPLIT_ADJUSTED"
    discontinuities = detect_discontinuities(rows)

    final, reason = "UNKNOWN", ""
    declared_state = declared["state"]
    kind = declared.get("kind", "official_statement")
    if declared_state != "UNKNOWN":
        if (kind == "official_field_definitions" and base_price is not None
                and (base_price["mismatch_count"] or discontinuities)):
            final, reason = "UNKNOWN", ("official field definitions suggest as-traded values, but the series shows base-price changes / "
                                        "discontinuities that must be classified first")
        elif observed_state not in ("UNKNOWN", declared_state) and not (
                declared_state in ("DIVIDEND_ADJUSTED", "TOTAL_RETURN_ADJUSTED") and observed_state == "SPLIT_ADJUSTED"):
            final, reason = "UNKNOWN", f"official statement ({declared_state}) contradicts observation ({observed_state})"
        else:
            what = ("official provider statement" if kind != "official_field_definitions" else
                    "official field definitions describe as-traded values (the guide contains NO explicit adjustment statement)")
            final, reason = declared_state, what + (
                f", consistent with observation ({observed_state})" if observed_state != "UNKNOWN" else "") + (
                f"; official base-price (vs) consistency: {base_price['rows_checked']} rows checked, "
                f"{base_price['mismatch_count']} mismatches" if base_price is not None else "")
    elif observed_state != "UNKNOWN":
        # no provider statement; the data was tested against an issuer-announced corporate action
        final, reason = observed_state, ("tested against the issuer's reference corporate action(s): "
                                         + ", ".join(e["ex_date"] for e in in_range) + " (no provider documentation retrieved)")
    elif not actions_for(market, symbol) and market == "CRYPTO":
        final, reason = "UNADJUSTED", "spot trading pair: no corporate-action mechanism exists (structural)"
    else:
        reason = "no official statement and no reference corporate action inside the range to test against"

    dividend = dividend_evidence or {"status": "UNKNOWN", "detail": "no dividend-adjusted counterpart observed"}
    stated = declared_state != "UNKNOWN" and kind == "official_statement"
    if final == "UNKNOWN":
        assurance = "UNKNOWN"
    elif stated:
        assurance = f"{final}_PROVIDER_STATED"
    elif kind == "official_field_definitions" and declared_state != "UNKNOWN":
        assurance = f"{final}_EVIDENCE_SUPPORTED"  # official field definitions + consistency; NOT a provider guarantee
    else:
        assurance = f"{final}_OBSERVED"
    return {
        "provider_explicit_statement": "AVAILABLE" if stated else "UNAVAILABLE", "assurance": assurance,
        "declared": declared, "observed": {"state": observed_state, "events": events, "base_price_consistency": base_price},
        "dividend": dividend, "final": final, "final_reason": reason,
        "unadjusted_discontinuities": [d for d in discontinuities] if final in ("UNADJUSTED", "UNKNOWN") else [],
        "all_overnight_discontinuities": discontinuities,
        "usable_for_research": final in USABLE_STATES,
    }
