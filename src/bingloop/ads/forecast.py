"""Bing Ads Ad Insight v13 — keyword traffic estimation (estimate_budget).

Wraps GetKeywordTrafficEstimates: given a set of keywords with optional max
CPCs, project impressions/clicks/spend without creating anything. Criteria
(location/language/network) are left to the account defaults so the tool needs
no geo-target lookups; the estimate is a broad-targeting projection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bingloop.ads.client import ADINSIGHT_BASE, call_service

if TYPE_CHECKING:
    from bingloop.config import BingloopConfig

_MATCH_TYPE = {"EXACT": "Exact", "PHRASE": "Phrase", "BROAD": "Broad"}
_DEFAULT_MAX_CPC = 1.0


def estimate_budget(
    config: BingloopConfig,
    *,
    keywords: list[dict],
    account_id: str = "",
    daily_budget: float = 0,
) -> dict:
    """Forecast clicks, impressions, and cost for a set of keywords."""
    if not keywords:
        return {"error": "At least one keyword is required"}

    keyword_estimators = []
    for kw in keywords:
        text = kw.get("text")
        if not text:
            return {"error": f"Keyword missing text: {kw}"}
        match_type = _MATCH_TYPE.get(str(kw.get("match_type", "EXACT")).upper())
        if match_type is None:
            return {"error": f"Invalid match_type for '{text}' — use EXACT, PHRASE, or BROAD"}
        keyword_estimators.append({
            "Keyword": {"Text": text, "MatchType": match_type},
            "MaxCpc": float(kw.get("max_cpc") or _DEFAULT_MAX_CPC),
        })

    estimator: dict = {
        "CampaignId": None,
        "AdGroupEstimators": [{"AdGroupId": None, "KeywordEstimators": keyword_estimators}],
    }
    if daily_budget:
        estimator["DailyBudget"] = float(daily_budget)

    resp = call_service(
        config, ADINSIGHT_BASE, "KeywordTrafficEstimates/Query",
        {"CampaignEstimators": [estimator]},
        account_id=account_id,
    )

    rows = _flatten_estimates(resp, keyword_estimators)
    totals = _sum_totals(rows)

    result: dict = {
        "keywords": rows,
        "totals": totals,
        "note": (
            "Broad-targeting estimate using account default location/language/"
            "network. Min/Max bracket the projected range per keyword."
        ),
    }
    if daily_budget and totals.get("max_total_cost"):
        captured = min(1.0, daily_budget / totals["max_total_cost"]) if totals["max_total_cost"] else None
        result["budget_coverage"] = {
            "daily_budget": daily_budget,
            "estimated_daily_cost_range": [totals["min_total_cost"], totals["max_total_cost"]],
            "share_of_max_captured": round(captured, 2) if captured is not None else None,
        }
    return result


def _flatten_estimates(resp: dict, requested: list[dict]) -> list[dict]:
    """Walk CampaignEstimates -> AdGroupEstimates -> KeywordEstimates into flat rows."""
    rows = []
    idx = 0
    for camp in resp.get("CampaignEstimates") or []:
        for ag in camp.get("AdGroupEstimates") or []:
            for est in ag.get("KeywordEstimates") or []:
                kw = est.get("Keyword") or {}
                text = kw.get("Text")
                if not text and idx < len(requested):
                    text = requested[idx]["Keyword"]["Text"]
                mn = est.get("Minimum") or {}
                mx = est.get("Maximum") or {}
                rows.append({
                    "keyword": text,
                    "match_type": kw.get("MatchType"),
                    "min": _bracket(mn),
                    "max": _bracket(mx),
                })
                idx += 1
    return rows


def _bracket(b: dict) -> dict:
    return {
        "impressions": b.get("Impressions"),
        "clicks": b.get("Clicks"),
        "ctr": b.get("Ctr"),
        "average_cpc": b.get("AverageCpc"),
        "average_position": b.get("AveragePosition"),
        "total_cost": b.get("TotalCost"),
    }


def _sum_totals(rows: list[dict]) -> dict:
    def _s(bracket: str, field: str) -> float:
        return round(sum((r[bracket].get(field) or 0) for r in rows), 2)

    return {
        "min_clicks": _s("min", "clicks"),
        "max_clicks": _s("max", "clicks"),
        "min_impressions": _s("min", "impressions"),
        "max_impressions": _s("max", "impressions"),
        "min_total_cost": _s("min", "total_cost"),
        "max_total_cost": _s("max", "total_cost"),
    }
