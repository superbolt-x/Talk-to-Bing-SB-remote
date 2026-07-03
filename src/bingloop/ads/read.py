"""Bing Ads read tools — report-based performance pulls.

Every tool runs a Reporting API v13 report with Summary aggregation (one row
per entity, aggregated over the whole inclusive date range) and returns
normalized rows: numeric strings parsed, spend-derived CPA/ROAS added.

Conversion columns — important Bing quirk (verified against Superbolt's ETL):
the plain ``Conversions`` column is deprecated (returns 0) in the classic
reports, so those use ``ConversionsQualified`` / ``AllConversionsQualified``.
The newer AssetGroup/Asset reports only offer plain ``Conversions``, which IS
live in those value sets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bingloop.ads.accounts import require_account
from bingloop.ads.client import build_report_request, get_report, to_float, to_int

if TYPE_CHECKING:
    from bingloop.config import BingloopConfig

# Metric columns parsed as numbers; everything else stays a verbatim string.
_INT_METRICS = {"Impressions", "Clicks", "Assists", "QualityScore"}
_FLOAT_METRICS = {
    "Spend", "Ctr", "AverageCpc", "AverageCpm", "AveragePosition",
    "Conversions", "ConversionsQualified", "ConversionRate", "CostPerConversion",
    "AllConversionsQualified", "AllRevenue", "Revenue", "ReturnOnAdSpend",
    "AllReturnOnAdSpend", "RevenuePerConversion", "CurrentMaxCpc",
    "AbsoluteTopImpressionRatePercent", "TopImpressionRatePercent",
    "ImpressionSharePercent",
}


def _account_scope(account_id: str) -> dict:
    return {"AccountIds": [int(account_id)]}


def _normalize_rows(rows: list[dict], conversions_col: str) -> list[dict]:
    """Parse numeric cells and add computed Cpa / Roas per row."""
    out = []
    for raw in rows:
        row: dict = {}
        for key, value in raw.items():
            if key in _INT_METRICS:
                row[key] = to_int(value)
            elif key in _FLOAT_METRICS:
                row[key] = to_float(value)
            else:
                row[key] = value
        spend = row.get("Spend") or 0
        conversions = row.get(conversions_col) or 0
        revenue = row.get("Revenue") or row.get("AllRevenue") or 0
        if conversions:
            row["Cpa"] = round(spend / conversions, 2)
        if spend and revenue:
            row["Roas"] = round(revenue / spend, 2)
        out.append(row)
    out.sort(key=lambda r: r.get("Spend") or 0, reverse=True)
    return out


def _run(
    config: BingloopConfig,
    report_type: str,
    columns: list[str],
    *,
    customer_id: str,
    account_id: str,
    date_range_start: str,
    date_range_end: str,
    conversions_col: str = "ConversionsQualified",
) -> list[dict]:
    from bingloop.dates import validate_iso_date_range

    if date_range_start and date_range_end:
        validate_iso_date_range(date_range_start, date_range_end)

    account_id = require_account(config, account_id)
    request = build_report_request(
        report_type,
        columns,
        _account_scope(account_id),
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )
    rows = get_report(
        config, request,
        customer_id=customer_id,  # empty -> resolved from account_id by call_service
        account_id=account_id,
    )
    return _normalize_rows(rows, conversions_col)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def list_accounts(config: BingloopConfig, *, refresh: bool = False) -> dict:
    """List every ad account the authenticated user can access, across all customers.

    Discovers accounts via GetUser + SearchAccounts and reports each account
    with the customer_id (CID) that other tools will use for it automatically.
    """
    from bingloop.ads.accounts import get_accounts

    data = get_accounts(config, refresh=refresh)
    accounts = data["accounts"]
    customers = sorted({r["customer_id"] for r in accounts if r["customer_id"] is not None})
    return {
        "accounts": accounts,
        "total_accounts": len(accounts),
        "total_customers": len(customers),
        "note": (
            "Pass account_id to any tool to target a specific account — the "
            "matching customer_id is resolved automatically. No default account "
            "is assumed."
        ),
    }


# ---------------------------------------------------------------------------
# Performance reads
# ---------------------------------------------------------------------------


def get_campaign_performance(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    rows = _run(
        config, "CampaignPerformanceReportRequest",
        ["AccountId", "AccountName", "CampaignId", "CampaignName", "CampaignStatus",
         "CampaignType", "CurrencyCode", "Impressions", "Clicks", "Ctr", "AverageCpc",
         "Spend", "ConversionsQualified", "Revenue", "AllConversionsQualified",
         "AllRevenue"],
        customer_id=customer_id, account_id=account_id,
        date_range_start=date_range_start, date_range_end=date_range_end,
    )
    return {"campaigns": rows, "total_campaigns": len(rows)}


def get_ad_group_performance(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    rows = _run(
        config, "AdGroupPerformanceReportRequest",
        ["AccountId", "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
         "Status", "Impressions", "Clicks", "Ctr", "AverageCpc", "Spend",
         "ConversionsQualified", "Revenue", "AllConversionsQualified", "AllRevenue"],
        customer_id=customer_id, account_id=account_id,
        date_range_start=date_range_start, date_range_end=date_range_end,
    )
    return {"ad_groups": rows, "total_ad_groups": len(rows)}


def get_ad_performance(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    rows = _run(
        config, "AdPerformanceReportRequest",
        ["AccountId", "CampaignName", "AdGroupName", "AdId", "AdType", "AdStatus",
         "AdTitle", "AdDescription", "AdDescription2", "FinalUrl",
         "Impressions", "Clicks", "Ctr", "AverageCpc", "Spend",
         "ConversionsQualified", "Revenue"],
        customer_id=customer_id, account_id=account_id,
        date_range_start=date_range_start, date_range_end=date_range_end,
    )
    return {"ads": rows, "total_ads": len(rows)}


def get_keyword_performance(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    rows = _run(
        config, "KeywordPerformanceReportRequest",
        ["AccountId", "CampaignId", "CampaignName", "AdGroupName", "KeywordId",
         "Keyword", "BidMatchType", "DeliveredMatchType", "KeywordStatus",
         "CurrentMaxCpc", "QualityScore", "Impressions", "Clicks", "Ctr",
         "AverageCpc", "Spend", "ConversionsQualified", "Revenue"],
        customer_id=customer_id, account_id=account_id,
        date_range_start=date_range_start, date_range_end=date_range_end,
    )
    return {"keywords": rows, "total_keywords": len(rows)}


def get_search_terms(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    rows = _run(
        config, "SearchQueryPerformanceReportRequest",
        ["AccountId", "CampaignName", "AdGroupName", "SearchQuery", "Keyword",
         "BidMatchType", "DeliveredMatchType", "Impressions", "Clicks", "Ctr",
         "AverageCpc", "Spend", "ConversionsQualified", "Revenue"],
        customer_id=customer_id, account_id=account_id,
        date_range_start=date_range_start, date_range_end=date_range_end,
    )
    rows = rows[:200]
    return {"search_terms": rows, "total_search_terms": len(rows)}


def get_product_performance(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    rows = _run(
        config, "ProductDimensionPerformanceReportRequest",
        ["AccountId", "CampaignName", "AdGroupName", "MerchantProductId", "Title",
         "Brand", "ProductType1", "ProductCategory1", "Condition",
         "Impressions", "Clicks", "Ctr", "AverageCpc", "Spend",
         "ConversionsQualified", "Revenue"],
        customer_id=customer_id, account_id=account_id,
        date_range_start=date_range_start, date_range_end=date_range_end,
    )
    rows = rows[:500]
    return {"products": rows, "total_products": len(rows)}


def get_asset_group_performance(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    rows = _run(
        config, "AssetGroupPerformanceReportRequest",
        ["AccountId", "CampaignId", "CampaignName", "AssetGroupId", "AssetGroupName",
         "AssetGroupStatus", "Impressions", "Clicks", "Ctr", "AverageCpc", "Spend",
         "Conversions", "Revenue"],
        customer_id=customer_id, account_id=account_id,
        date_range_start=date_range_start, date_range_end=date_range_end,
        conversions_col="Conversions",
    )
    return {
        "asset_groups": rows,
        "total_asset_groups": len(rows),
        "note": (
            "Microsoft's API has no per-asset metrics INSIDE PMax asset groups — "
            "asset-group level is the finest PMax grain available."
        ),
    }


def get_asset_performance(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    rows = _run(
        config, "AssetPerformanceReportRequest",
        ["AccountId", "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
         "AssetId", "AssetContent", "AssetType", "AssetSource",
         "Impressions", "Clicks", "Spend", "Conversions"],
        customer_id=customer_id, account_id=account_id,
        date_range_start=date_range_start, date_range_end=date_range_end,
        conversions_col="Conversions",
    )
    return {
        "assets": rows,
        "total_assets": len(rows),
        "note": (
            "Grain is ad group + asset (Microsoft's asset report has no AdId "
            "column). Covers RSA headlines/descriptions/images per ad group."
        ),
    }


def get_extension_performance(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Per-extension-item performance (sitelinks, callouts, calls, ...).

    AdExtensionDetailReportRequest requires TimePeriod, so this pulls Daily
    rows and sums them client-side to one row per extension item.
    """
    from bingloop.dates import validate_iso_date_range

    if date_range_start and date_range_end:
        validate_iso_date_range(date_range_start, date_range_end)

    account_id_resolved = require_account(config, account_id)
    request = build_report_request(
        "AdExtensionDetailReportRequest",
        ["TimePeriod", "AccountId", "CampaignId", "CampaignName", "AdExtensionId",
         "AdExtensionType", "AdExtensionTypeId", "AdExtensionPropertyValue",
         "Impressions", "Clicks", "Spend", "ConversionsQualified"],
        _account_scope(account_id_resolved),
        aggregation="Daily",
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )
    raw = get_report(
        config, request,
        customer_id=customer_id,  # empty -> resolved from account_id by call_service
        account_id=account_id_resolved,
    )

    grouped: dict[tuple, dict] = {}
    for row in raw:
        key = (row.get("CampaignId"), row.get("AdExtensionId"),
               row.get("AdExtensionPropertyValue"))
        if key not in grouped:
            grouped[key] = {
                "CampaignId": row.get("CampaignId"),
                "CampaignName": row.get("CampaignName"),
                "AdExtensionId": row.get("AdExtensionId"),
                "AdExtensionType": row.get("AdExtensionType"),
                "AdExtensionPropertyValue": row.get("AdExtensionPropertyValue"),
                "Impressions": 0, "Clicks": 0, "Spend": 0.0,
                "ConversionsQualified": 0.0,
            }
        g = grouped[key]
        g["Impressions"] += to_int(row.get("Impressions")) or 0
        g["Clicks"] += to_int(row.get("Clicks")) or 0
        g["Spend"] += to_float(row.get("Spend")) or 0.0
        g["ConversionsQualified"] += to_float(row.get("ConversionsQualified")) or 0.0

    rows = sorted(grouped.values(), key=lambda r: r["Spend"], reverse=True)
    for r in rows:
        r["Spend"] = round(r["Spend"], 2)
        if r["ConversionsQualified"]:
            r["Cpa"] = round(r["Spend"] / r["ConversionsQualified"], 2)
    return {"extensions": rows, "total_extensions": len(rows)}


# ---------------------------------------------------------------------------
# Generic escape hatch — Bing has no query language, this is the run_gaql analog
# ---------------------------------------------------------------------------

# Report types Claude may run generically, with notes on quirks.
SUPPORTED_REPORT_TYPES = {
    "CampaignPerformanceReportRequest": "Campaign level. Use ConversionsQualified (plain Conversions is deprecated here).",
    "AdGroupPerformanceReportRequest": "Ad group level. Use ConversionsQualified.",
    "AdPerformanceReportRequest": "Ad level (titles, descriptions, final URL). Use ConversionsQualified.",
    "KeywordPerformanceReportRequest": "Keyword level incl. QualityScore, bids. Use ConversionsQualified.",
    "SearchQueryPerformanceReportRequest": "Actual user search queries. Use ConversionsQualified.",
    "ProductDimensionPerformanceReportRequest": "Shopping product level. Use ConversionsQualified.",
    "ProductPartitionPerformanceReportRequest": "Shopping product-partition level. Use ConversionsQualified.",
    "AssetGroupPerformanceReportRequest": "PMax asset-group level. Only plain Conversions exists (and is live).",
    "AssetPerformanceReportRequest": "Per-asset (ad group + asset grain, no AdId). Only plain Conversions.",
    "AdExtensionDetailReportRequest": "Per extension item. TimePeriod column is REQUIRED (use Daily aggregation).",
    "AgeGenderAudienceReportRequest": "Demographics. Use ConversionsQualified.",
    "GeographicPerformanceReportRequest": "Geo performance. Use ConversionsQualified.",
    "AudiencePerformanceReportRequest": "Audience/remarketing lists.",
    "DeviceOSPerformanceReportRequest": "Device/OS split. Use ConversionsQualified.",
    "BudgetSummaryReportRequest": "Daily budget utilization (MonthlyBudget/DailySpend). Daily aggregation only.",
    "AccountPerformanceReportRequest": "Account level rollup. Use ConversionsQualified.",
}


def list_report_types(config: BingloopConfig) -> dict:
    """Reference for run_report — supported report types and usage notes."""
    return {
        "report_types": SUPPORTED_REPORT_TYPES,
        "usage": (
            "Call run_report with report_type, columns, and optional aggregation/"
            "dates. Column names are PascalCase per Microsoft's docs "
            "(learn.microsoft.com 'Report Attributes and Performance Statistics'). "
            "With aggregation='Summary' do NOT include TimePeriod; with any other "
            "aggregation TimePeriod is required. An invalid column name fails the "
            "whole report with an error naming the column."
        ),
    }


def run_report(
    config: BingloopConfig,
    *,
    report_type: str,
    columns: list[str],
    aggregation: str = "Summary",
    customer_id: str = "",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_ids: list[int] | None = None,
    limit: int = 1000,
) -> dict:
    """Run an arbitrary Reporting API v13 report — the GAQL escape-hatch analog."""
    from bingloop.dates import validate_iso_date_range

    if date_range_start and date_range_end:
        validate_iso_date_range(date_range_start, date_range_end)

    if aggregation == "Summary" and "TimePeriod" in columns:
        return {"error": "TimePeriod is not allowed with Summary aggregation — "
                         "drop the column or use aggregation='Daily'."}
    if aggregation != "Summary" and "TimePeriod" not in columns:
        columns = ["TimePeriod"] + columns

    account_id_resolved = require_account(config, account_id)
    if campaign_ids:
        scope: dict = {"Campaigns": [
            {"AccountId": int(account_id_resolved), "CampaignId": int(cid)}
            for cid in campaign_ids
        ]}
    else:
        scope = _account_scope(account_id_resolved)

    request = build_report_request(
        report_type, columns, scope,
        aggregation=aggregation,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )
    raw = get_report(
        config, request,
        customer_id=customer_id,  # empty -> resolved from account_id by call_service
        account_id=account_id_resolved,
    )
    truncated = len(raw) > limit
    rows = _normalize_rows(raw[:limit], "ConversionsQualified")
    result = {"report_type": report_type, "rows": rows, "total_rows": len(rows)}
    if truncated:
        result["truncated"] = f"Report returned {len(raw)} rows; showing first {limit}."
    return result
