"""Independent cross-source comparison (Batch #3A, Phase 9).

Purpose: explain or expose differences between sources. It NEVER promotes an
unofficial source to trusted and never edits either dataset.

Per-row verdicts for each field:
  MATCH                 equal within a tight numeric tolerance
  EXPLAINED_DIFFERENCE  differs, with a stated cause that is checked numerically
                        (price/volume on a different adjustment basis across a reference split,
                         different venue within a venue tolerance)
  CONFLICT              differs and nothing explains it
  UNKNOWN               one side missing / unusable (null, NaN) so no judgment is possible

Dataset-level verdict: CONFLICT if any CONFLICT row, else EXPLAINED_DIFFERENCE if any
explained row, else MATCH; UNKNOWN when no overlap exists.
"""

from __future__ import annotations

import math

FIELDS = ("open", "high", "low", "close", "volume")


def _usable(v) -> bool:
    return v is not None and isinstance(v, float) and math.isfinite(v)


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)


def compare(a, b, *, label_a: str, label_b: str, price_tol: float = 1e-4, volume_tol: float = 1e-6,
            venue_tol: float | None = None, split_actions: list | None = None, volume_comparable: bool = True,
            window: tuple | None = None, unknown_tol: dict | None = None) -> dict:
    """Compare two NormalizedResult objects date by date.

    ``unknown_tol``: {field: tol} - a difference that is small (within tol) but has NO established
    cause is reported UNKNOWN, never "explained". ``venue_tol``: when set (different venues), price differences up to it are EXPLAINED
    rather than CONFLICT and volume is judged only for presence. ``split_actions``: reference
    splits used to explain different adjustment bases (a's value / ratio == b's value for dates
    before the ex-date; volume * ratio)."""

    index_a = {r["date"]: r for r in a.rows}
    index_b = {r["date"]: r for r in b.rows}
    dates_a, dates_b = set(index_a), set(index_b)
    if window:
        dates_a = {d for d in dates_a if window[0] <= d <= window[1]}
        dates_b = {d for d in dates_b if window[0] <= d <= window[1]}
    overlap = sorted(dates_a & dates_b)
    only_a, only_b = sorted(dates_a - dates_b), sorted(dates_b - dates_a)
    splits = [(s["ex_date"], s["ratio"]) for s in (split_actions or [])]

    tally = {f: {"MATCH": 0, "EXPLAINED_DIFFERENCE": 0, "CONFLICT": 0, "UNKNOWN": 0} for f in FIELDS}
    explained_by: dict[str, int] = {}
    conflicts, unknowns, explained_examples, max_unknown = [], [], [], {}
    for d in overlap:
        ra, rb = index_a[d], index_b[d]
        ratio_before = next((ratio for ex, ratio in splits if d < ex), None)
        for f in FIELDS:
            va, vb = ra[f], rb[f]
            if not (_usable(va) and _usable(vb)):
                tally[f]["UNKNOWN"] += 1
                if len(unknowns) < 12:
                    unknowns.append({"date": d, "field": f, label_a: va, label_b: vb})
                continue
            tol = volume_tol if f == "volume" else price_tol
            if f == "volume" and not volume_comparable:
                tally[f]["EXPLAINED_DIFFERENCE"] += 1
                explained_by["venue-specific volume (not comparable across venues)"] = explained_by.get("venue-specific volume (not comparable across venues)", 0) + 1
                continue
            if _close(va, vb, tol):
                tally[f]["MATCH"] += 1
                continue
            cause = None
            if ratio_before:
                # b is on the adjusted basis: price / ratio, volume * ratio (reference ratio, rounded by the provider)
                expected = va * ratio_before if f == "volume" else va / ratio_before
                swapped = vb * ratio_before if f == "volume" else vb / ratio_before
                loose = 2e-3 if f != "volume" else 5e-2
                if _close(expected, vb, loose):
                    cause = f"{label_b} on split-adjusted basis (x1/{ratio_before:g} price, x{ratio_before:g} volume) vs {label_a} as-traded"
                elif _close(swapped, va, loose):
                    cause = f"{label_a} on split-adjusted basis vs {label_b} as-traded"
            if cause is None and venue_tol is not None and f != "volume" and _close(va, vb, venue_tol):
                cause = f"different venues, within {venue_tol:.2%}"
            if cause is None and unknown_tol and f in unknown_tol and _close(va, vb, unknown_tol[f]):
                tally[f]["UNKNOWN"] += 1
                if len(unknowns) < 12:
                    unknowns.append({"date": d, "field": f, label_a: va, label_b: vb,
                                     "note": f"differs by {abs(va - vb) / max(abs(va), abs(vb)):.3%}; cause not established"})
                max_unknown[f] = max(max_unknown.get(f, 0.0), abs(va - vb) / max(abs(va), abs(vb), 1e-12))
                continue
            if cause:
                tally[f]["EXPLAINED_DIFFERENCE"] += 1
                explained_by[cause] = explained_by.get(cause, 0) + 1
                if len(explained_examples) < 6:
                    explained_examples.append({"date": d, "field": f, label_a: va, label_b: vb, "cause": cause})
            else:
                tally[f]["CONFLICT"] += 1
                if len(conflicts) < 15:
                    conflicts.append({"date": d, "field": f, label_a: va, label_b: vb,
                                      "rel_diff": abs(va - vb) / max(abs(va), abs(vb), 1e-12)})
    totals = {k: sum(t[k] for t in tally.values()) for k in ("MATCH", "EXPLAINED_DIFFERENCE", "CONFLICT", "UNKNOWN")}
    if not overlap:
        verdict = "UNKNOWN"
    elif totals["CONFLICT"]:
        verdict = "CONFLICT"
    elif totals["EXPLAINED_DIFFERENCE"]:
        verdict = "EXPLAINED_DIFFERENCE"
    elif totals["UNKNOWN"]:
        verdict = "UNKNOWN" if not totals["EXPLAINED_DIFFERENCE"] else "EXPLAINED_DIFFERENCE+UNKNOWN"
    else:
        verdict = "MATCH"
    return {"a": label_a, "b": label_b, "rows_a": len(dates_a), "rows_b": len(dates_b), "overlap_rows": len(overlap),
            "only_in_a": only_a, "only_in_b": only_b, "price_tolerance": price_tol, "venue_tolerance": venue_tol,
            "per_field": tally, "totals": totals, "verdict": verdict, "explained_by": explained_by,
            "conflict_examples": conflicts, "unknown_examples": unknowns, "explained_examples": explained_examples,
            "max_unexplained_rel_diff": max_unknown}


def date_set_difference(a, b) -> dict:
    da, db = {r["date"] for r in a.rows}, {r["date"] for r in b.rows}
    return {"only_in_a": sorted(da - db), "only_in_b": sorted(db - da)}


def kr_hypotheses(yahoo_eval: dict, naver_eval: dict, official=None) -> list[dict]:
    """Historical hypotheses are tested against CURRENT data. Each returns REPRODUCED /
    NOT_REPRODUCED / UNKNOWN with the evidence used."""

    def check(ev, cid):
        return next((c for c in ev["report"]["checks"] if c["id"] == cid), None)

    out = []
    v13 = check(yahoo_eval, "V13")
    missing = set(v13.get("missing_sessions", [])) if v13 else set()
    for day in ("2022-01-03", "2022-05-09"):
        out.append({"id": f"yahoo-missing-{day}", "source": "yahoo-chart", "hypothesis": f"Yahoo is missing {day}",
                    "verdict": "REPRODUCED" if day in missing else "NOT_REPRODUCED",
                    "evidence": f"{day} {'absent' if day in missing else 'present'} in the current Yahoo raw; "
                                f"XKRX snapshot calls it a session (community calendar - see official comparison)"})
    v02 = check(yahoo_eval, "V02")
    out.append({"id": "yahoo-null-row", "source": "yahoo-chart", "hypothesis": "Yahoo contains null rows",
                "verdict": "REPRODUCED" if v02 and v02["status"] == "FAIL" else "NOT_REPRODUCED",
                "evidence": v02["summary"] if v02 else "n/a", "examples": v02["examples"][:3] if v02 else []})
    v05 = check(yahoo_eval, "V05")
    out.append({"id": "yahoo-ohlc-anomaly", "source": "yahoo-chart", "hypothesis": "Yahoo has OHLC anomalies",
                "verdict": "REPRODUCED" if v05 and v05["status"] == "FAIL" else "NOT_REPRODUCED",
                "evidence": v05["summary"] if v05 else "n/a", "examples": v05["examples"][:3] if v05 else []})
    v06 = check(yahoo_eval, "V06b")
    out.append({"id": "yahoo-stale-volume", "source": "yahoo-chart", "hypothesis": "Yahoo has stale / zero-volume anomalies",
                "verdict": "REPRODUCED" if v06 else "NOT_REPRODUCED",
                "evidence": v06["summary"] if v06 else "no zero-volume sessions", "examples": v06["examples"][:5] if v06 else []})
    v04n = check(naver_eval, "V04")
    out.append({"id": "naver-zero-price", "source": "naver-fchart", "hypothesis": "Naver has zero-price rows on suspension days",
                "verdict": "REPRODUCED" if v04n and v04n["status"] == "FAIL" else "NOT_REPRODUCED",
                "evidence": v04n["summary"] if v04n else "n/a", "examples": v04n["examples"][:4] if v04n else []})
    return out
