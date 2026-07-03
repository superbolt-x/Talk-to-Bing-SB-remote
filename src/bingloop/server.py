"""Bingloop MCP server — FastMCP instance with all tool registrations."""

from __future__ import annotations

import functools
import logging
import os
import secrets
from typing import Callable
from urllib.parse import parse_qs, urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from bingloop.config import load_config

logger = logging.getLogger("bingloop")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# ── Tool classification (drives Claude's permission management) ───────────────
# readOnlyHint -> safe reads; WRITE -> mutates the ad account; DESTRUCTIVE ->
# removes/applies irreversible changes. openWorldHint -> talks to Microsoft APIs.
_READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

# ── Auth: authless (Parker-style) ─────────────────────────────────────────────
# No OAuth is advertised — Claude connects with no login step. If MCP_AUTH_TOKEN
# is set, TokenGateMiddleware requires it on every MCP request (via the connector
# URL ?access_token=... or an Authorization: Bearer header). Empty = fully open.
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


def _derive_allowed_hosts() -> list:
    """Hosts allowed through DNS-rebinding protection: SERVER_URL (scheme optional)
    + Railway's auto-injected RAILWAY_PUBLIC_DOMAIN + optional MCP_ALLOWED_HOSTS.
    Prevents 421 'Invalid Host header'."""
    candidates: list = []
    if SERVER_URL:
        _u = SERVER_URL if "//" in SERVER_URL else "https://" + SERVER_URL
        netloc = urlparse(_u).netloc
        if netloc:
            candidates.append(netloc)
    rpd = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if rpd:
        candidates.append(rpd)
    for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(","):
        if h.strip():
            candidates.append(h.strip())
    seen, out = set(), []
    for h in candidates:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


_allowed_hosts = _derive_allowed_hosts()
if _allowed_hosts:
    _transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[x for h in _allowed_hosts for x in (h, f"{h}:*")],
        allowed_origins=[x for h in _allowed_hosts for x in (f"https://{h}", f"http://{h}")],
    )
    logger.info("DNS-rebinding protection ON — allowed hosts: %s", _allowed_hosts)
else:
    _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    logger.warning("No SERVER_URL / RAILWAY_PUBLIC_DOMAIN — DNS-rebinding protection OFF")

mcp = FastMCP(
    "Talk-to-Bing",
    instructions=(
        "Talk-to-Bing connects Microsoft Advertising (Bing Ads) data to Claude. "
        "Use the read tools to analyze performance, and the write tools (with "
        "safety confirmation) to manage campaigns. Call list_accounts first when "
        "the target ad account is not known."
    ),
    transport_security=_transport_security,
)
logger.info("Talk-to-Bing configured — server_url=%s auth_gate=%s", SERVER_URL or "(unset)", bool(AUTH_TOKEN))


class TokenGateMiddleware:
    """Authless-with-a-shared-secret ASGI gate. When a token is configured, require
    it on every HTTP request (from ?access_token=/token or Bearer). Correct token →
    pass through (no auth challenge); otherwise 401."""

    def __init__(self, app, token: str):
        self._app = app
        self._token = token

    def _provided(self, scope) -> str:
        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        val = (qs.get("access_token") or qs.get("token") or [""])[0]
        if val:
            return val
        for k, v in scope.get("headers") or []:
            if k == b"authorization":
                auth = v.decode("latin-1")
                if auth.lower().startswith("bearer "):
                    return auth[7:].strip()
        return ""

    async def __call__(self, scope, receive, send):
        if self._token and scope.get("type") == "http":
            if not secrets.compare_digest(self._provided(scope), self._token):
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self._app(scope, receive, send)


_config = load_config()


def _safe(fn: Callable) -> Callable:
    """Wrap a tool function so exceptions return structured error dicts."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RuntimeError as e:
            from bingloop.auth import BingAdsAuthError

            if isinstance(e, BingAdsAuthError):
                return {
                    "error": "Authentication failed — Bing Ads refresh token is dead.",
                    "hint": (
                        "The refresh token was revoked, expired, or rotated by "
                        "another process. Re-run the one-time OAuth consent flow, "
                        "set BING_ADS_REFRESH_TOKEN_SEED to the new token, and "
                        "delete the stale token file on the volume (see README)."
                    ),
                }
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e), "tool": fn.__name__}

    return wrapper


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def health_check() -> dict:
    """Test Talk-to-Bing connectivity — checks config, OAuth token, and the Bing Ads API.

    Run this first if other tools are failing. Returns status for each layer
    and actionable guidance if something is broken.
    """
    from bingloop.auth import BingAdsAuthError, check_config

    status: dict = {"config": "ok", "auth": "unknown", "ads": "unknown"}

    try:
        check_config(_config)
    except Exception as e:
        status["config"] = "error"
        status["config_error"] = str(e)
        return status

    try:
        from bingloop.auth import get_access_token

        get_access_token(_config)
        status["auth"] = "ok"
    except BingAdsAuthError as e:
        status["auth"] = "error"
        status["auth_error"] = str(e)
        status["hint"] = (
            "Refresh token is dead — re-run the OAuth consent flow and reset "
            "BING_ADS_REFRESH_TOKEN_SEED (see README)."
        )
        return status
    except Exception as e:
        status["auth"] = "error"
        status["auth_error"] = str(e)
        return status

    try:
        from bingloop.ads.read import list_accounts as _ads_test

        result = _ads_test(_config)
        status["ads"] = "ok"
        status["ads_accounts"] = result.get("total_accounts", 0)
        status["default_account_id"] = _config.ads.account_id or "(unset)"
    except Exception as e:
        status["ads"] = "error"
        status["ads_error"] = str(e)

    return status


# ---------------------------------------------------------------------------
# Read Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def list_accounts() -> dict:
    """List every Bing Ads account the authenticated user can access, across all customers.

    Returns each account's id, name, number, status, and the customer_id (CID)
    that other tools use for it automatically. Call this first to discover which
    accounts are available, then pass an account_id to any other tool — the
    matching customer is resolved for you. No account is assumed by default.
    """
    from bingloop.ads.read import list_accounts as _impl

    return _impl(_config)


@mcp.tool(annotations=_READONLY)
@_safe
def split_date_range(
    date_range_start: str,
    date_range_end: str,
    days: int = 7,
) -> dict:
    """Split an inclusive date range into consecutive, NON-overlapping windows.

    Call this before pulling week-over-week (or any multi-window) performance.
    Every Talk-to-Bing date range is inclusive on both ends, so if you reuse an
    end date as the next window's start, that day's spend, impressions, and
    clicks are counted twice — and a sum of weekly pulls will overstate the true
    total. The windows returned here tile the range cleanly so summing a metric
    across them equals the whole-range value.

    days=7 with a Monday start gives Mon–Sun weeks. Date format: "YYYY-MM-DD".
    Returns: {"windows": [{"start", "end"}, ...], "total_windows", "days_per_window"}.
    """
    from bingloop.dates import split_date_range as _impl

    windows = _impl(date_range_start, date_range_end, days=days)
    return {
        "windows": [{"start": s, "end": e} for s, e in windows],
        "total_windows": len(windows),
        "days_per_window": days,
        "note": (
            "Windows are inclusive and non-overlapping; summing a metric across "
            "them equals the metric over the full range."
        ),
    }


@mcp.tool(annotations=_READONLY)
@_safe
def get_campaign_performance(
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get campaign-level performance metrics for a date range.

    Returns: campaign name, status, type, impressions, clicks, spend,
    conversions (ConversionsQualified), revenue, CPA, ROAS, CTR per campaign.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. For
    weekly/multi-window pulls, build windows with split_date_range — adjacent
    windows that share a boundary day double-count spend, impressions, clicks.
    """
    from bingloop.ads.read import get_campaign_performance as _impl

    return _impl(
        _config,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_ad_group_performance(
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get ad group-level performance metrics for a date range.

    Returns: campaign name, ad group name, status, impressions, clicks, spend,
    conversions (ConversionsQualified), CPA, CTR for each ad group.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts spend, impressions, and clicks).
    """
    from bingloop.ads.read import get_ad_group_performance as _impl

    return _impl(
        _config,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_ad_performance(
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get ad-level performance data including titles, descriptions, and metrics.

    Returns: ad type, title, descriptions, final URL, impressions, clicks,
    CTR, conversions, spend for each ad. For per-headline/description detail
    on RSAs use get_asset_performance instead.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts spend, impressions, and clicks).
    """
    from bingloop.ads.read import get_ad_performance as _impl

    return _impl(
        _config,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_keyword_performance(
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get keyword metrics including quality scores and bids.

    Returns: keyword text, match types (bid + delivered), quality score,
    current max CPC, impressions, clicks, CTR, CPC, conversions, spend.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts spend, impressions, and clicks).
    """
    from bingloop.ads.read import get_keyword_performance as _impl

    return _impl(
        _config,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_search_terms(
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get search query report — what users actually typed before clicking your ads.

    Critical for finding negative keyword opportunities and understanding user
    intent. Returns: search query, matched keyword, campaign, ad group,
    impressions, clicks, spend, conversions. Top 200 rows by spend.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts spend, impressions, and clicks).
    """
    from bingloop.ads.read import get_search_terms as _impl

    return _impl(
        _config,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_product_performance(
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get product-level performance from Shopping campaigns.

    Returns: merchant product ID, title, brand, type, category, condition,
    impressions, clicks, spend, conversions, and ROAS per product. Use this to
    identify top/underperforming products in Shopping or PMax. Max 500 products.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days. Use
    split_date_range for week-over-week so adjacent windows don't share a
    boundary day (which double-counts spend, impressions, and clicks).
    """
    from bingloop.ads.read import get_product_performance as _impl

    return _impl(
        _config,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_asset_group_performance(
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get asset group-level performance for Performance Max (PMax) campaigns.

    Returns: campaign name, asset group name, status, impressions, clicks,
    spend, conversions per asset group. NOTE: this report's live conversion
    column is plain Conversions (unlike the classic reports). Microsoft's API
    has NO per-asset metrics inside PMax asset groups — this is the finest
    PMax grain available.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days.
    """
    from bingloop.ads.read import get_asset_group_performance as _impl

    return _impl(
        _config,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_asset_performance(
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get per-asset performance (headlines, descriptions, images) by ad group.

    Returns each asset's content, type, and source with impressions, clicks,
    spend, and conversions. Use this to identify which RSA headlines and
    descriptions drive performance and which should be replaced.

    NOTE: the grain is ad group + asset — Microsoft's asset report has no AdId
    column, so per-ad asset breakdown is not possible (unlike Google's
    ad_group_ad_asset_view).
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days.
    """
    from bingloop.ads.read import get_asset_performance as _impl

    return _impl(
        _config,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_extension_performance(
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
) -> dict:
    """Get per-extension-item performance (sitelinks, callouts, calls, ...).

    Returns each ad extension item with its type, displayed value, impressions,
    clicks, spend, and conversions — summed over the date range.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days.
    """
    from bingloop.ads.read import get_extension_performance as _impl

    return _impl(
        _config,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


@mcp.tool(annotations=_READONLY)
@_safe
def get_negative_keywords(
    account_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List existing campaign-level negative keywords.

    Use this before adding negative keywords to check for duplicates.
    If campaign_id is empty, returns negatives across all campaigns in the
    account. Negative keywords that live in shared lists are reported with
    their list name.
    """
    from bingloop.ads.campaign_mgmt import get_negative_keywords as _impl

    return _impl(_config, account_id=account_id, campaign_id=campaign_id)


@mcp.tool(annotations=_READONLY)
@_safe
def list_report_types() -> dict:
    """Reference for run_report — supported Bing report types and usage notes.

    Bing Ads has no query language (no GAQL equivalent); arbitrary pulls go
    through run_report with an explicit report type + column list. Call this
    first to see what is available and which conversion column each report
    supports.
    """
    from bingloop.ads.read import list_report_types as _impl

    return _impl(_config)


@mcp.tool(annotations=_READONLY)
@_safe
def run_report(
    report_type: str,
    columns: list[str],
    aggregation: str = "Summary",
    account_id: str = "",
    date_range_start: str = "",
    date_range_end: str = "",
    campaign_ids: list[int] | None = None,
    limit: int = 1000,
) -> dict:
    """Run an arbitrary Bing Ads Reporting API v13 report — the advanced escape hatch.

    Use this for pulls not covered by the dedicated tools (geo, device,
    demographics, audiences, budget pacing, per-day breakdowns...). Call
    list_report_types first for valid report types and conversion-column notes.

    columns: PascalCase report column names per Microsoft's documentation.
    aggregation: Summary (one row per entity over the whole range) | Daily |
    Weekly | Monthly | Hourly | DayOfWeek | HourOfDay. Non-Summary aggregations
    automatically include TimePeriod.
    campaign_ids: optional — restrict the report scope to specific campaigns.
    Dates are INCLUSIVE on both ends (YYYY-MM-DD); empty = last 30 days.
    """
    from bingloop.ads.read import run_report as _impl

    return _impl(
        _config,
        report_type=report_type,
        columns=columns,
        aggregation=aggregation,
        account_id=account_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        campaign_ids=campaign_ids,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Write Tools (Safety Layer)
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_WRITE)
@_safe
def draft_campaign(
    campaign_name: str,
    daily_budget: float,
    bidding_strategy: str,
    account_id: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    campaign_type: str = "SEARCH",
    ad_group_name: str = "",
    keywords: list[dict] | None = None,
) -> dict:
    """Draft a full campaign structure — returns a PREVIEW, does NOT create anything.

    Creates: Campaign (PAUSED, with daily budget) + AdGroup + optional Keywords.
    Ads are NOT included — use draft_responsive_search_ad after the campaign exists.

    bidding_strategy: MAXIMIZE_CONVERSIONS | TARGET_CPA | TARGET_ROAS |
                      MAXIMIZE_CONVERSION_VALUE | MAXIMIZE_CLICKS | MANUAL_CPC |
                      ENHANCED_CPC
    target_cpa: required if bidding_strategy is TARGET_CPA (in account currency)
    target_roas: required if bidding_strategy is TARGET_ROAS
    campaign_type: SEARCH | SHOPPING | AUDIENCE | PERFORMANCE_MAX
    keywords: list of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD"}

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from bingloop.ads.write import draft_campaign as _impl

    return _impl(
        _config,
        account_id=account_id,
        campaign_name=campaign_name,
        daily_budget=daily_budget,
        bidding_strategy=bidding_strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        campaign_type=campaign_type,
        ad_group_name=ad_group_name,
        keywords=keywords,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_responsive_search_ad(
    ad_group_id: str,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
    account_id: str = "",
    path1: str = "",
    path2: str = "",
) -> dict:
    """Draft a Responsive Search Ad — returns a PREVIEW, does NOT create the ad.

    Provide 3-15 headlines (max 30 chars each) and 2-4 descriptions (max 90
    chars each). The ad is created PAUSED. The preview shows exactly what will
    be created. Call confirm_and_apply to execute.
    """
    from bingloop.ads.write import draft_responsive_search_ad as _impl

    return _impl(
        _config,
        account_id=account_id,
        ad_group_id=ad_group_id,
        headlines=headlines,
        descriptions=descriptions,
        final_url=final_url,
        path1=path1,
        path2=path2,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def draft_keywords(
    ad_group_id: str,
    keywords: list[dict],
    account_id: str = "",
) -> dict:
    """Draft keyword additions — returns a PREVIEW, does NOT add keywords.

    keywords: list of {"text": "keyword phrase", "match_type": "EXACT|PHRASE|BROAD"}
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from bingloop.ads.write import draft_keywords as _impl

    return _impl(
        _config,
        account_id=account_id,
        ad_group_id=ad_group_id,
        keywords=keywords,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def add_negative_keywords(
    campaign_id: str,
    keywords: list[str],
    account_id: str = "",
    match_type: str = "EXACT",
) -> dict:
    """Draft campaign-level negative keyword additions — returns a PREVIEW.

    Negative keywords prevent your ads from showing for irrelevant searches.
    match_type: "EXACT" or "PHRASE" — Bing does NOT support broad-match negatives.
    Call confirm_and_apply with the returned plan_id to execute.
    """
    from bingloop.ads.write import add_negative_keywords as _impl

    return _impl(
        _config,
        account_id=account_id,
        campaign_id=campaign_id,
        keywords=keywords,
        match_type=match_type,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def pause_entity(
    entity_type: str,
    entity_id: str,
    account_id: str = "",
) -> dict:
    """Draft pausing a campaign, ad group, ad, or keyword — returns a PREVIEW.

    entity_type: "campaign", "ad_group", "ad", or "keyword"
    entity_id format by type (Bing needs the parent ID on child operations):
      - campaign: campaign ID (e.g. "12345678")
      - ad_group: "campaignId~adGroupId" (e.g. "12345678~987654")
      - ad: "adGroupId~adId" (e.g. "12345678~987654")
      - keyword: "adGroupId~keywordId" (e.g. "12345678~987654")

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from bingloop.ads.write import pause_entity as _impl

    return _impl(
        _config,
        account_id=account_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_WRITE)
@_safe
def enable_entity(
    entity_type: str,
    entity_id: str,
    account_id: str = "",
) -> dict:
    """Draft enabling a paused campaign, ad group, ad, or keyword — returns a PREVIEW.

    entity_type: "campaign", "ad_group", "ad", or "keyword"
    entity_id format by type (Bing needs the parent ID on child operations):
      - campaign: campaign ID (e.g. "12345678")
      - ad_group: "campaignId~adGroupId" (e.g. "12345678~987654")
      - ad: "adGroupId~adId" (e.g. "12345678~987654")
      - keyword: "adGroupId~keywordId" (e.g. "12345678~987654")

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from bingloop.ads.write import enable_entity as _impl

    return _impl(
        _config,
        account_id=account_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def remove_entity(
    entity_type: str,
    entity_id: str,
    account_id: str = "",
) -> dict:
    """Draft REMOVING an entity — returns a PREVIEW. This is IRREVERSIBLE.

    entity_type: "campaign", "ad_group", "ad", "keyword", or "negative_keyword"
    entity_id format by type (Bing needs the parent ID on child operations):
      - campaign: campaign ID
      - ad_group: "campaignId~adGroupId"
      - ad: "adGroupId~adId"
      - keyword: "adGroupId~keywordId"
      - negative_keyword: "campaignId~negativeKeywordId"

    WARNING: Deleted entities cannot be restored. Use pause_entity instead
    if you just want to temporarily disable something.

    Call confirm_and_apply with the returned plan_id to execute.
    """
    from bingloop.ads.write import remove_entity as _impl

    return _impl(
        _config,
        account_id=account_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@mcp.tool(annotations=_DESTRUCTIVE)
@_safe
def confirm_and_apply(
    plan_id: str,
    dry_run: bool = True,
) -> dict:
    """Execute a previously previewed change.

    IMPORTANT: Defaults to dry_run=True. You MUST explicitly pass dry_run=false
    to make real changes to the Bing Ads account.

    The plan_id comes from a prior draft_* or pause/enable/remove tool call.
    """
    from bingloop.ads.write import confirm_and_apply as _impl

    return _impl(_config, plan_id=plan_id, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Planning Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READONLY)
@_safe
def estimate_budget(
    keywords: list[dict],
    account_id: str = "",
    daily_budget: float = 0,
) -> dict:
    """Forecast clicks, impressions, and cost for a set of keywords.

    Uses the Bing Ads Ad Insight keyword traffic estimation service to project
    campaign performance without creating anything. Essential for budget
    planning before launching campaigns. Targeting defaults to the account's
    existing settings.

    keywords: list of {"text": "keyword", "match_type": "EXACT|PHRASE|BROAD", "max_cpc": 1.50}
        max_cpc is optional (defaults to 1.00 in account currency)
    daily_budget: if provided, insights will show what % of estimated traffic
        the budget captures
    """
    from bingloop.ads.forecast import estimate_budget as _impl

    return _impl(
        _config,
        keywords=keywords,
        account_id=account_id,
        daily_budget=daily_budget,
    )


# ---------------------------------------------------------------------------
# HTTP server entrypoint
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server.

    Transport is controlled by the MCP_TRANSPORT env var:
      - streamable-http (default) — modern HTTP, recommended for remote deployments
      - sse                       — legacy Server-Sent Events HTTP transport
      - stdio                     — local stdio transport for Claude Desktop / CLI

    HTTP env vars (ignored for stdio):
      MCP_HOST — bind address (default: 0.0.0.0)
      MCP_PORT — bind port    (default: 8000)
    """
    import uvicorn

    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    logger.info("Starting Talk-to-Bing MCP server transport=%s", transport)

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("MCP_PORT", "8000"))

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from contextlib import asynccontextmanager

    raw_app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
    gated_app = TokenGateMiddleware(raw_app, token=AUTH_TOKEN)

    @asynccontextmanager
    async def lifespan(_app):
        async with raw_app.router.lifespan_context(_app):
            yield

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "transport": transport, "auth_gate": bool(AUTH_TOKEN)})

    routes = [
        Route("/health", health),
        Mount("/", app=gated_app),
    ]

    app = Starlette(lifespan=lifespan, routes=routes)
    logger.info("Listening on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
