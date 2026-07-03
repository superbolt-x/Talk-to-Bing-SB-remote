"""Bing Ads Campaign Management v13 REST — the mutation + negative-keyword calls.

REST conventions (verified against learn.microsoft.com, bingads-13 REST pivot):
  * POST = Add, PUT = Update, DELETE = Delete (same collection URL). DELETE
    requests carry a JSON body.
  * Get* operations are POST to a /Query* sub-path.
  * Polymorphic objects use a "Type" string discriminator: BiddingScheme uses
    the full class name ("MaxConversionsBiddingScheme"); Ad uses the AdType enum
    value ("ResponsiveSearch", NOT "ResponsiveSearchAd").
  * Returned ID arrays (CampaignIds, AdGroupIds, KeywordIds, AdIds) are
    positional with the request; a failed item is null at its index.
  * Batch failures come back as HTTP 200 with a PartialErrors array (negative
    keyword ops use NestedPartialErrors instead).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bingloop.ads.accounts import require_account
from bingloop.ads.client import CAMPAIGN_BASE, call_service, raise_on_partial_errors

if TYPE_CHECKING:
    from bingloop.config import BingloopConfig

# ── Enum value maps: our uppercase API contract -> Bing's PascalCase values ──
_MATCH_TYPE = {"EXACT": "Exact", "PHRASE": "Phrase", "BROAD": "Broad"}
_NEG_MATCH_TYPE = {"EXACT": "Exact", "PHRASE": "Phrase"}
_CAMPAIGN_TYPE = {
    "SEARCH": "Search",
    "SHOPPING": "Shopping",
    "AUDIENCE": "Audience",
    "PERFORMANCE_MAX": "PerformanceMax",
}


def _bidding_scheme(strategy: str, target_cpa: float | None, target_roas: float | None) -> dict:
    """Map our bidding-strategy contract to a Bing BiddingScheme object.

    TargetCpaBiddingScheme / TargetRoasBiddingScheme were removed for new
    campaigns (Aug 2025), so TARGET_CPA/TARGET_ROAS map onto the MaxConversions /
    MaxConversionValue schemes carrying the target — the behavior Microsoft's
    own silent conversion produces.
    """
    if strategy in ("MAXIMIZE_CONVERSIONS", "TARGET_CPA"):
        scheme: dict = {"Type": "MaxConversionsBiddingScheme"}
        if target_cpa:
            scheme["TargetCpa"] = target_cpa
        return scheme
    if strategy in ("MAXIMIZE_CONVERSION_VALUE", "TARGET_ROAS"):
        scheme = {"Type": "MaxConversionValueBiddingScheme"}
        if target_roas:
            scheme["TargetRoas"] = target_roas
        return scheme
    if strategy == "MAXIMIZE_CLICKS":
        return {"Type": "MaxClicksBiddingScheme"}
    if strategy == "MANUAL_CPC":
        return {"Type": "ManualCpcBiddingScheme"}
    if strategy == "ENHANCED_CPC":
        return {"Type": "EnhancedCpcBiddingScheme"}
    raise ValueError(f"Unsupported bidding strategy: {strategy}")


def _first_id(response: dict, key: str) -> int:
    """Pull the first (index 0) id from a positional Add response, erroring on null."""
    ids = response.get(key) or []
    if not ids or ids[0] is None:
        raise RuntimeError(
            f"{key} came back empty/null — the entity was not created. "
            f"PartialErrors: {response.get('PartialErrors')}"
        )
    return int(ids[0])


# ---------------------------------------------------------------------------
# Create a full campaign structure (campaign -> ad group -> keywords)
# ---------------------------------------------------------------------------


def create_campaign_structure(
    config: BingloopConfig,
    changes: dict,
    *,
    customer_id: str = "",
    account_id: str = "",
) -> dict:
    """AddCampaigns -> AddAdGroups -> AddKeywords, sequentially (each needs the
    parent id from the previous step). The campaign is created Paused."""
    ctx = dict(customer_id=customer_id, account_id=account_id)
    account_id_int = int(account_id or config.ads.account_id)

    campaign = {
        "Name": changes["campaign_name"],
        "CampaignType": _CAMPAIGN_TYPE.get(changes.get("campaign_type", "SEARCH"), "Search"),
        "Status": "Paused",
        "BudgetType": "DailyBudgetStandard",
        "DailyBudget": float(changes["daily_budget"]),
        "TimeZone": config.ads.default_timezone,
        "Languages": ["All"],
        "BiddingScheme": _bidding_scheme(
            changes["bidding_strategy"], changes.get("target_cpa"), changes.get("target_roas")
        ),
    }
    camp_resp = call_service(
        config, CAMPAIGN_BASE, "Campaigns",
        {"AccountId": account_id_int, "Campaigns": [campaign]}, **ctx,
    )
    raise_on_partial_errors(camp_resp, "AddCampaigns")
    campaign_id = _first_id(camp_resp, "CampaignIds")

    ad_group = {
        "Name": changes.get("ad_group_name") or changes["campaign_name"],
        "Status": "Paused",
        "AdGroupType": "SearchStandard",
    }
    ag_resp = call_service(
        config, CAMPAIGN_BASE, "AdGroups",
        {"CampaignId": campaign_id, "AdGroups": [ad_group]}, **ctx,
    )
    raise_on_partial_errors(ag_resp, "AddAdGroups")
    ad_group_id = _first_id(ag_resp, "AdGroupIds")

    result = {"campaign_id": campaign_id, "ad_group_id": ad_group_id, "keyword_ids": []}

    kw_list = changes.get("keywords") or []
    if kw_list:
        kw_resp = _add_keywords_raw(config, ad_group_id, kw_list, **ctx)
        raise_on_partial_errors(kw_resp, "AddKeywords")
        result["keyword_ids"] = [k for k in (kw_resp.get("KeywordIds") or []) if k is not None]

    return result


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------


def add_responsive_search_ad(
    config: BingloopConfig,
    *,
    ad_group_id: int,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
    path1: str = "",
    path2: str = "",
    customer_id: str = "",
    account_id: str = "",
) -> dict:
    """AddAds with a single ResponsiveSearch ad (created Paused)."""
    ad: dict = {
        "Type": "ResponsiveSearch",
        "Status": "Paused",
        "FinalUrls": [final_url],
        "Headlines": [{"Asset": {"Type": "TextAsset", "Text": h}} for h in headlines],
        "Descriptions": [{"Asset": {"Type": "TextAsset", "Text": d}} for d in descriptions],
    }
    if path1:
        ad["Path1"] = path1
    if path2:
        ad["Path2"] = path2

    resp = call_service(
        config, CAMPAIGN_BASE, "Ads",
        {"AdGroupId": ad_group_id, "Ads": [ad]},
        customer_id=customer_id, account_id=account_id,
    )
    raise_on_partial_errors(resp, "AddAds")
    return {"ad_id": _first_id(resp, "AdIds"), "ad_group_id": ad_group_id, "status": "Paused"}


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------


def _add_keywords_raw(
    config: BingloopConfig, ad_group_id: int, keywords: list[dict],
    *, customer_id: str = "", account_id: str = "",
) -> dict:
    payload = [
        {
            "Text": kw["text"],
            "MatchType": _MATCH_TYPE[kw["match_type"].upper()],
            "Status": "Active",
        }
        for kw in keywords
    ]
    return call_service(
        config, CAMPAIGN_BASE, "Keywords",
        {"AdGroupId": ad_group_id, "Keywords": payload},
        customer_id=customer_id, account_id=account_id,
    )


def add_keywords(
    config: BingloopConfig,
    *,
    ad_group_id: int,
    keywords: list[dict],
    customer_id: str = "",
    account_id: str = "",
) -> dict:
    resp = _add_keywords_raw(config, ad_group_id, keywords,
                             customer_id=customer_id, account_id=account_id)
    raise_on_partial_errors(resp, "AddKeywords")
    return {
        "ad_group_id": ad_group_id,
        "keyword_ids": [k for k in (resp.get("KeywordIds") or []) if k is not None],
    }


# ---------------------------------------------------------------------------
# Campaign-level negative keywords
# ---------------------------------------------------------------------------


def add_negative_keywords_to_campaign(
    config: BingloopConfig,
    *,
    campaign_id: int,
    keywords: list[str],
    match_type: str,
    customer_id: str = "",
    account_id: str = "",
) -> dict:
    negatives = [
        {"Text": text, "MatchType": _NEG_MATCH_TYPE[match_type.upper()]}
        for text in keywords
    ]
    body = {
        "EntityNegativeKeywords": [
            {"EntityId": campaign_id, "EntityType": "Campaign", "NegativeKeywords": negatives}
        ]
    }
    resp = call_service(
        config, CAMPAIGN_BASE, "EntityNegativeKeywords", body,
        customer_id=customer_id, account_id=account_id,
    )
    # Negative-keyword ops report failures via NestedPartialErrors, not PartialErrors.
    nested = [e for e in (resp.get("NestedPartialErrors") or []) if e]
    if nested:
        raise RuntimeError(f"AddNegativeKeywordsToEntities failed: {nested}")
    added = []
    for group in resp.get("NegativeKeywordIds") or []:
        added.extend(group.get("Ids") or [])
    return {"campaign_id": campaign_id, "negative_keyword_ids": added}


def get_negative_keywords(
    config: BingloopConfig,
    *,
    account_id: str = "",
    campaign_id: str = "",
) -> dict:
    """List campaign-level negative keywords for one campaign, or all campaigns.

    GetNegativeKeywordsByEntityIds accepts at most one EntityId per call, so
    "all campaigns" fans out: list campaigns first, then query each.
    """
    account_id = require_account(config, account_id)
    ctx = dict(account_id=account_id)

    if campaign_id:
        campaign_ids = [int(campaign_id)]
        names: dict[int, str] = {}
    else:
        campaigns = _list_campaigns(config, account_id=account_id)
        campaign_ids = [c["id"] for c in campaigns]
        names = {c["id"]: c["name"] for c in campaigns}

    results = []
    total = 0
    for cid in campaign_ids:
        resp = call_service(
            config, CAMPAIGN_BASE, "NegativeKeywords/QueryByEntityIds",
            {"EntityIds": [cid], "EntityType": "Campaign", "ParentEntityId": None},
            **ctx,
        )
        for entity in resp.get("EntityNegativeKeywords") or []:
            negs = entity.get("NegativeKeywords") or []
            for n in negs:
                results.append({
                    "campaign_id": cid,
                    "campaign_name": names.get(cid, ""),
                    "id": n.get("Id"),
                    "text": n.get("Text"),
                    "match_type": n.get("MatchType"),
                })
            total += len(negs)

    return {"negative_keywords": results, "total_negative_keywords": total}


def _list_campaigns(config: BingloopConfig, *, account_id: str = "") -> list[dict]:
    """GetCampaignsByAccountId across all campaign types → [{id, name, status, type}]."""
    account_id_int = int(account_id or config.ads.account_id)
    resp = call_service(
        config, CAMPAIGN_BASE, "Campaigns/QueryByAccountId",
        {
            "AccountId": account_id_int,
            # default is Search only — request every type explicitly
            "CampaignType": "Search Shopping DynamicSearchAds Audience PerformanceMax",
            "ReturnAdditionalFields": None,
        },
        account_id=account_id,
    )
    return [
        {
            "id": int(c["Id"]),
            "name": c.get("Name"),
            "status": c.get("Status"),
            "type": c.get("CampaignType"),
        }
        for c in (resp.get("Campaigns") or [])
        if c.get("Id") is not None
    ]


# ---------------------------------------------------------------------------
# Status change + delete (dispatched from write._execute_plan)
# ---------------------------------------------------------------------------


def update_entity_status(
    config: BingloopConfig,
    *,
    entity_type: str,
    entity_id: str,
    status: str,
    customer_id: str = "",
    account_id: str = "",
) -> dict:
    """Update Status on a campaign/ad group/ad/keyword via PUT.

    Child entities carry their parent id in entity_id as "parentId~childId"
    (see write.py). status is already Bing-cased ("Active" / "Paused").
    """
    ctx = dict(customer_id=customer_id, account_id=account_id)

    if entity_type == "campaign":
        account_id_int = int(account_id or config.ads.account_id)
        body = {"AccountId": account_id_int, "Campaigns": [{"Id": int(entity_id), "Status": status}]}
        resp = call_service(config, CAMPAIGN_BASE, "Campaigns", body, method="PUT", **ctx)
        raise_on_partial_errors(resp, "UpdateCampaigns")
        return {"campaign_id": int(entity_id), "status": status}

    parent_id, child_id = _split(entity_id)

    if entity_type == "ad_group":
        body = {"CampaignId": parent_id, "AdGroups": [{"Id": child_id, "Status": status}]}
        resp = call_service(config, CAMPAIGN_BASE, "AdGroups", body, method="PUT", **ctx)
        raise_on_partial_errors(resp, "UpdateAdGroups")

    elif entity_type == "keyword":
        body = {"AdGroupId": parent_id, "Keywords": [{"Id": child_id, "Status": status}]}
        resp = call_service(config, CAMPAIGN_BASE, "Keywords", body, method="PUT", **ctx)
        raise_on_partial_errors(resp, "UpdateKeywords")

    elif entity_type == "ad":
        # UpdateAds needs the polymorphic Type discriminator. We don't track the
        # ad's real type through the plan, so assume ResponsiveSearch — the only
        # ad type Bingloop creates and the dominant modern search ad type.
        body = {
            "AdGroupId": parent_id,
            "Ads": [{"Id": child_id, "Type": "ResponsiveSearch", "Status": status}],
        }
        resp = call_service(config, CAMPAIGN_BASE, "Ads", body, method="PUT", **ctx)
        raise_on_partial_errors(resp, "UpdateAds")

    else:
        raise ValueError(f"Cannot change status of entity_type: {entity_type}")

    return {"entity_type": entity_type, "entity_id": entity_id, "status": status}


def delete_entity(
    config: BingloopConfig,
    *,
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
    account_id: str = "",
) -> dict:
    """DELETE a campaign/ad group/ad/keyword/negative_keyword (irreversible)."""
    ctx = dict(customer_id=customer_id, account_id=account_id)

    if entity_type == "campaign":
        account_id_int = int(account_id or config.ads.account_id)
        body = {"AccountId": account_id_int, "CampaignIds": [int(entity_id)]}
        resp = call_service(config, CAMPAIGN_BASE, "Campaigns", body, method="DELETE", **ctx)
        raise_on_partial_errors(resp, "DeleteCampaigns")
        return {"deleted": "campaign", "id": int(entity_id)}

    parent_id, child_id = _split(entity_id)

    if entity_type == "ad_group":
        body = {"CampaignId": parent_id, "AdGroupIds": [child_id]}
        resp = call_service(config, CAMPAIGN_BASE, "AdGroups", body, method="DELETE", **ctx)
        raise_on_partial_errors(resp, "DeleteAdGroups")

    elif entity_type == "ad":
        body = {"AdGroupId": parent_id, "AdIds": [child_id]}
        resp = call_service(config, CAMPAIGN_BASE, "Ads", body, method="DELETE", **ctx)
        raise_on_partial_errors(resp, "DeleteAds")

    elif entity_type == "keyword":
        body = {"AdGroupId": parent_id, "KeywordIds": [child_id]}
        resp = call_service(config, CAMPAIGN_BASE, "Keywords", body, method="DELETE", **ctx)
        raise_on_partial_errors(resp, "DeleteKeywords")

    elif entity_type == "negative_keyword":
        body = {
            "EntityNegativeKeywords": [
                {"EntityId": parent_id, "EntityType": "Campaign",
                 "NegativeKeywords": [{"Id": child_id}]}
            ]
        }
        resp = call_service(
            config, CAMPAIGN_BASE, "EntityNegativeKeywords", body, method="DELETE", **ctx
        )
        nested = [e for e in (resp.get("NestedPartialErrors") or []) if e]
        if nested:
            raise RuntimeError(f"DeleteNegativeKeywordsFromEntities failed: {nested}")

    else:
        raise ValueError(f"Cannot delete entity_type: {entity_type}")

    return {"deleted": entity_type, "entity_id": entity_id}


def _split(entity_id: str) -> tuple[int, int]:
    parent, child = entity_id.split("~", 1)
    return int(parent), int(child)
