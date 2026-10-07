"""Batch #3C - research-validity evidence builder (analysis of SAVED results only).

Runs no strategy, loads no Lockbox bar (benchmark audit slices ``bars[:development_end]``), never
imports ``evaluate_lockbox``. Writes ``artifacts/verification/validity_3c/*.json``.

    python -m qat.research.validity
"""

from __future__ import annotations

import hashlib
import json
import pathlib

from qat.data.loader import load_dataset
from qat.research import cost_evidence, protocol, trials
from qat.research.manifest import PROJECT_ROOT
from qat.research.metrics import MIN_TRADES_FOR_STATS
from qat.research.store import registry_read, results_root

OUT_DIR = PROJECT_ROOT / "artifacts" / "verification" / "validity_3c"
FROZEN_DIR = protocol.OUT_DIR
BARS_PER_YEAR = {"KR": 252, "US": 252, "CRYPTO": 365}

THRESHOLD_PROVENANCE = {
    "min_total_oos_trades": {
        "value": protocol.THRESHOLDS["min_total_oos_trades"],
        "repository_basis": f"qat.research.metrics.MIN_TRADES_FOR_STATS = {MIN_TRADES_FOR_STATS}: the Batch #2 'short sample' warning (design doc: 청산 거래 < 30이면 표본 경고)",
        "literature_basis_in_repository": "none found (no citation, derivation or power calculation anywhere in src/, docs/ or README)",
        "classification": "internal pre-registered heuristic",
        "note": "Not asserted to be right or wrong; its evidence level is only 'internal convention fixed before the results were seen'.",
    },
    "min_active_fold_fraction": {"value": protocol.THRESHOLDS["min_active_fold_fraction"], "classification": "internal pre-registered heuristic",
                                 "literature_basis_in_repository": "none"},
    "positive_fold_fraction_must_exceed": {"value": protocol.THRESHOLDS["positive_fold_fraction_must_exceed"],
                                           "classification": "internal pre-registered heuristic", "literature_basis_in_repository": "none"},
}

METHODS = {
    "chosen": {"method": "White Reality Check (per market, explicit universe)", "status": "CANDIDATE_METHOD - APPLICABILITY UNVERIFIED (not implemented; corrected in Batch #3D from 'provisional choice')",
               "fact": "White (2000) proposes the Reality Check, which evaluates the predictive superiority of the best model found in a specification search / under data snooping against a benchmark; "
                       "the assumptions needed to apply it to QAT were not sufficiently verified against the primary text",
               "why": "nonparametric (stationary bootstrap) and needs no estimate of an 'effective number of independent trials': the universe is the explicit, "
                      "registered set of candidate procedures evaluated on one market's common OOS time axis. Prerequisites are unmet today (see below).",
               "primary_text_verified_by_this_audit": False},
    "white_reality_check": {
        "source": "H. White, 'A Reality Check for Data Snooping', Econometrica 68(5), 1097-1126 (2000)",
        "source_check": "PDF retrieved but not machine-readable in this environment; only the publisher abstract-level description was confirmed: tests the null that the best model found "
                        "in a specification search has no predictive superiority over a benchmark, using the stationary bootstrap. Assumptions below are NOT verified against the text.",
        "assumptions_to_verify": ["stationary, weakly dependent (mixing) performance differentials", "benchmark fixed ex ante", "all M models searched are included and evaluated on the same sample"],
        "fits_current_structure": "partly: the 3 strategy procedures of one market have OOS equity series on a common axis (M=3 per market); the inner parameter grid is selected per fold "
                                  "and has no OOS series of its own",
        "sample_activity_check": "FAILS today: all 9 candidates are INSUFFICIENT_ACTIVITY (0-22 OOS trades; stitched series are mostly flat or fold-reset), stationarity across regimes is untested",
        "decision": "not applied to Batch #3B"},
    "deflated_sharpe_ratio": {
        "source": "D. Bailey, M. Lopez de Prado, 'The Deflated Sharpe Ratio', J. Portfolio Management (2014)",
        "source_check": "PDF retrieved and its text extracted locally. Confirmed in the text: DSR is a PSR whose rejection threshold reflects the expected maximum Sharpe ratio of N INDEPENDENT trials "
                        "(Euler-Mascheroni constant, standard-normal quantiles); inputs are N, the variance of the trial Sharpe estimates, sample length T, skewness and kurtosis of the selected "
                        "strategy; Sharpe is non-annualized; Appendix 3 treats non-independent trials. The equations themselves were not machine-readable, so the closed form was not re-checked.",
        "assumptions": ["trials of one strategy class share a mean/variance of Sharpe", "N counts independent trials (dependent trials need an effective-N argument)",
                        "returns are serially independent (non-normality via skew/kurtosis only)"],
        "fits_current_structure": "no: the 9 candidates sit in 3 different return processes (markets); an effective N is not defined; one selected return series per fold-stitched candidate "
                                  "has fold-reset capital and long flat stretches",
        "sample_activity_check": "FAILS today (activity); T of daily observations overstates information when trades are 0-22",
        "decision": "not applied; a possible secondary check for v2 if per-trial return series are stored"},
    "probability_of_backtest_overfitting": {
        "source": "Bailey, Borwein, Lopez de Prado, Zhu, 'The Probability of Backtest Overfitting'",
        "source_check": "PDF retrieved and text extracted. Confirmed: input is a T x N performance matrix of ALL N configurations on the same time axis, split into S sub-matrices; "
                        "all C(S,S/2) train/test combinations (CSCV); logit of the out-of-sample relative rank of the in-sample best; PBO is the frequency of non-positive logits.",
        "fits_current_structure": "no: Walk-Forward stores only the chosen configuration's OOS series; PBO needs every configuration's full-period series (14 per market)",
        "sample_activity_check": "cannot be evaluated from saved results; computing it needs new backtests of all configurations (not done in this batch)",
        "decision": "not computable from saved results; v2 candidate only if all-configuration series are stored"},
    "v2_storage_requirement": "store, for every registered trial/candidate, the OOS (and for PBO the full-period) per-bar return series on a common time axis so a method can be applied later",
}

BENCHMARK_NOTES = {
    "adjustment_semantics": "same: benchmark and engine read the same persisted dataset bars (KR UNADJUSTED_EVIDENCE_SUPPORTED, US split-adjusted closes not dividend-adjusted, BTC unadjusted)",
    "stitching_contract": "same: both are compounded fold by fold with capital reset at each fold",
    "exit": "same: close of the last OOS bar (the engine marks equity at that close)",
    "entry_finding": "MISALIGNED by one bar: the engine can fill the earliest order at open(test_start+1) (signal at close(test_start), fill next open), the v1 benchmark buys at open(test_start)",
    "exposure_finding": "benchmark is 100% invested and cost-free; the engine uses position_fraction 0.95, reservation buffer, spread/slippage/fees",
    "action": "Protocol v1 is frozen: code and definition are NOT changed (changing the definition changes the protocol hash). v2 must use the aligned definition.",
}


def _sha_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_status() -> dict:
    proto = json.loads((FROZEN_DIR / "protocol.json").read_text(encoding="utf-8"))
    return {"protocol_version": proto["version"], "protocol_sha256": proto["protocol_sha256"],
            "protocol_json_file_sha256": _sha_file(FROZEN_DIR / "protocol.json"), "summary_json_file_sha256": _sha_file(FROZEN_DIR / "summary.json"),
            "thresholds": proto["thresholds"], "search_space": proto["search_space"],
            "code_matches_frozen_protocol": protocol.protocol_definition()["protocol_sha256"] == proto["protocol_sha256"],
            "status": "historical research evidence; observed development evidence (NOT untouched); only the Lockbox tail is untouched"}


def aligned_passive_return(bars, test: list[int]) -> float:
    """Audit-only: earliest engine-feasible entry open(test_start+1) -> close(last OOS bar), cost-free."""

    return bars[test[1] - 1].close / bars[test[0] + 1].open - 1


def benchmark_audit(summary: dict, repo_root: pathlib.Path = PROJECT_ROOT) -> dict:
    out = {}
    for market in summary["markets"]:
        ds = load_dataset(str(repo_root / protocol.MARKETS[market]))
        dev_end = len(ds.bars) - protocol.WINDOWS[market][2]
        bars = ds.bars[:dev_end]  # the Lockbox tail is never indexed
        rows = summary["results"][f"{market}/{protocol.STRATEGY_NAMES[0]}"]["market_specific"]["rows"]
        v1 = aligned = 1.0
        for r in rows:
            if r["test"][1] > dev_end:
                raise AssertionError("an OOS window reaches into the Lockbox")
            v1 *= 1 + protocol.passive_return(bars, r["test"])
            aligned *= 1 + aligned_passive_return(bars, r["test"])
        flips = []
        for name in protocol.STRATEGY_NAMES:
            ms = summary["results"][f"{market}/{name}"]["market_specific"]
            if (ms["stitched_net_return"] > v1 - 1) != (ms["stitched_net_return"] > aligned - 1):
                flips.append(name)
        out[market] = {"v1_passive_stitched": v1 - 1, "aligned_passive_stitched": aligned - 1, "difference": (aligned - 1) - (v1 - 1),
                       "candidates_whose_beats_passive_flag_would_change": flips, "development_bars": [0, dev_end], "folds": len(rows)}
    flipped = sorted(f"{m}/{n}" for m, v in out.items() for n in v["candidates_whose_beats_passive_flag_would_change"])
    return {"markets": out, "notes": BENCHMARK_NOTES, "flag_would_change_for": flipped,
            "conclusion": "one-bar entry misalignment and exposure/cost asymmetry found. The v1 'beats passive' flag would change for the candidates listed in flag_would_change_for, "
                          "but no Batch #3B status or Lockbox decision depends on it (all 9 are INSUFFICIENT_ACTIVITY); v1 is left unchanged and no 'beats benchmark' claim is made"}


def observed_trade_rates(summary: dict) -> dict:
    """Planning facts from the frozen results: OOS closed trades per OOS year (no threshold change)."""

    out = {}
    for key, entry in summary["results"].items():
        market = entry["market"]
        ms = entry["market_specific"]
        years = sum(r["test"][1] - r["test"][0] for r in ms["rows"]) / BARS_PER_YEAR[market]
        rate = ms["total_oos_trades"] / years
        out[key] = {"oos_years": round(years, 2), "oos_trades": ms["total_oos_trades"], "trades_per_oos_year": round(rate, 2),
                    "oos_years_for_30_trades_at_this_rate": round(30 / rate, 1) if rate else None}
    return out


def protocol_v2_decision(rates: dict) -> dict:
    best = max(v["trades_per_oos_year"] for v in rates.values())
    return {
        "decision": "PROTOCOL_V2_RECOMMENDED",
        "scope": "BTC/USDT only, conditional on the prerequisites below; KR and US stay daily-only with RESEARCH_STOP (no verified higher-frequency source identified in this repository)",
        "if_prerequisites_not_met": "RESEARCH_STOP_RECOMMENDED",
        "criterion": "statistical verifiability and data quality, not expected performance",
        "observed_planning_facts": {"best_trades_per_oos_year_among_9": best, "years_of_OOS_needed_at_best_rate_for_30_trades": round(30 / best, 1),
                                    "source": "frozen Batch #3B summary; used only to size data needs, no threshold is changed"},
        "options": {
            "A_longer_verified_history": {
                "independent_observations": "adds calendar time only at the observed ~3-5 trades per OOS year: ~6 or more extra OOS years at the BEST rate, never at the worst",
                "cost_modelling": "unchanged daily problem (spread/slippage still unmeasurable)",
                "microstructure": "low impact", "provenance": "KR: impossible (official service has no rows before 2020-01-02); US: older Yahoo/Nasdaq rows need the existing adjustment audit; "
                                                              "BTC: Binance monthly archive starts 2017-08 (about 4 months before the current dataset)",
                "leakage_risk": "low", "complexity": "low", "verdict": "insufficient alone"},
            "B_higher_frequency_bars": {
                "independent_observations": "more bars and more trades per year, but not independent evidence across regimes: same market, same calendar period as the observed development data",
                "cost_modelling": "HARD: fees are published (Binance VIP0 0.10%/0.10%), spread/slippage are not measurable from klines and the spot archive has no quote data (aggTrades/trades only)",
                "microstructure": "high impact; hourly horizon makes spread/slippage first-order", "provenance": "BTCUSDT 1m..1d klines with checksums exist in the same public archive used for the daily data; "
                                                                                                      "KR/US: no verified intraday source identified here",
                "leakage_risk": "higher (intrabar timestamps, session semantics, look-ahead in resampling)", "complexity": "high: canonical format, validation V12/V13 and calendars are daily-based",
                "verdict": "only viable route to many more observations, crypto only, and ONLY after a microstructure/cost evidence step"},
            "C_keep_daily_and_stop": {"independent_observations": "none added", "cost_modelling": "n/a", "microstructure": "n/a", "provenance": "n/a", "leakage_risk": "none",
                                      "complexity": "none", "verdict": "valid fallback; leaves the question unanswered"},
        },
        "untouched_evidence_note": "Batch #3B development windows are observed evidence. The only untouched data are the Lockbox tails (not to be opened here) and data AFTER the verified coverage: the "
                                   "Binance monthly archive lists BTCUSDT months through 2026-09, i.e. 2026 data not in the verified dataset; its use needs the standard acquisition/provenance pipeline and a user decision.",
        "required_new_data_and_evidence": [
            "USER DECISIONS: venue and fee tier (VIP level, BNB discount), whether 2026 data may serve as the new untouched holdout, whether to proceed at all",
            "verified hourly (or 4h) BTCUSDT klines from the Binance archive with checksum, raw preservation, deterministic identity (extend canonical format/validation beyond daily)",
            "microstructure/cost study from aggTrades/trades (trade-based spread and impact proxies) with each cost parameter classified VERIFIED / EVIDENCE_SUPPORTED / PLACEHOLDER / UNKNOWN",
            "per-trial return-series storage + the trial registry so a data-snooping method (provisionally White Reality Check) can be applied; method assumptions verified against the primary text first",
            "a statistical power analysis (trade count needed) fixed BEFORE any v2 result, new PROTOCOL_VERSION wf-protocol-2, aligned passive benchmark (entry open(test_start+1), same exposure and costs)",
        ],
        "not_allowed": "no strategy execution, no Walk-Forward, no Lockbox access in Batch #3C"}


def lockbox_status() -> dict:
    return {"lockbox_registry_exists": (results_root() / "lockbox_registry.json").exists(),
            "evaluate_lockbox_imported_by_this_module": "evaluate_lockbox" in globals(),
            "benchmark_audit_reads_only": "bars[:development_end] (asserted)",
            "opened": False}


def _write(out_dir: pathlib.Path, name: str, obj) -> None:
    (out_dir / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True, default=str) + "\n", encoding="utf-8")


def build_all(out_dir: pathlib.Path | None = None) -> dict:
    out_dir = pathlib.Path(out_dir or OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = json.loads((FROZEN_DIR / "summary.json").read_text(encoding="utf-8"))
    rec = trials.reconstruct()
    reg = registry_read(trials.REGISTRY_NAME)
    tsum = trials.summarize(reg)
    trial_report = {
        "summary": tsum, "reconstruction": rec, "registry_fingerprint_sha256": trials.registry_fingerprint(reg),
        "counting_semantics": trials.__doc__, "identity": "sha256 of dataset identity, strategy+version, params, window, stage, selection metric, full base config, settings hash; "
                                                          "protocol version is recorded but not part of the identity",
        "caveats": ["9 plumbing smoke runs (save=False) and the pytest runs (tmp results dir) re-evaluated trials identical to registered ones: identical ids, hence 0 additional unique trials",
                    "Batch #2 synthetic runs are excluded from the real-data counts",
                    "cost-stress and common-period runs are counted as distinct configurations/windows but not as new candidate procedures",
                    "only Walk-Forward runs are registered; plain backtests and cost-stress runs outside Walk-Forward are not trials of a selection"],
        "naive_vs_actual": {"naive_candidates": 9, "actual_unique_trials": tsum["unique_trials"], "actual_train_selection_trials": tsum["train_selection_trials"]}}
    rates = observed_trade_rates(summary)
    files = {
        "frozen_3b_status.json": frozen_status(),
        "threshold_provenance.json": THRESHOLD_PROVENANCE,
        "trial_registry_3b.json": trial_report,
        "multiple_testing_assessment.json": METHODS,
        "cost_evidence.json": cost_evidence.report(str(protocol.SETTINGS_PATH)),
        "benchmark_audit.json": benchmark_audit(summary),
        "observed_trade_rates.json": rates,
        "protocol_v2_decision.json": protocol_v2_decision(rates),
        "lockbox_status.json": lockbox_status(),
    }
    for name, obj in files.items():
        _write(out_dir, name, obj)
    return {"trials": tsum, "decision": files["protocol_v2_decision.json"]["decision"], "cost_status_counts": files["cost_evidence.json"]["status_counts"],
            "lockbox": files["lockbox_status.json"]}


if __name__ == "__main__":
    print(json.dumps(build_all(), indent=1))
