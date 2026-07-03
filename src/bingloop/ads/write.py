"""Bing Ads write tools — all behind the safety layer.

Every write tool returns a preview/plan. Nothing executes until
``confirm_and_apply`` is called with the plan ID.

Composite entity IDs (Bing needs the parent ID on child operations):
  campaign        -> "campaignId"
  ad_group        -> "campaignId~adGroupId"
  ad              -> "adGroupId~adId"
  keyword         -> "adGroupId~keywordId"
  negative_keyword-> "campaignId~negativeKeywordId"
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bingloop.config import BingloopConfig


# ---------------------------------------------------------------------------
# Draft tools — validate inputs, create a ChangePlan, return preview
# ---------------------------------------------------------------------------


def draft_responsive_search_ad(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    ad_group_id: str = "",
    headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
    final_url: str = "",
    path1: str = "",
    path2: str = "",
) -> dict:
    """Draft a Responsive Search Ad — returns preview, does NOT execute."""
    from bingloop.safety.guards import SafetyViolation, check_blocked_operation
    from bingloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_responsive_search_ad", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    headlines = headlines or []
    descriptions = descriptions or []

    errors = _validate_rsa(ad_group_id, headlines, descriptions, final_url)
    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings = []
    if len(headlines) < 8:
        warnings.append(
            f"Only {len(headlines)} headlines provided. Microsoft recommends 8-15 "
            "diverse headlines for optimal RSA performance."
        )
    if len(descriptions) < 3:
        warnings.append(
            f"Only {len(descriptions)} descriptions provided. Microsoft recommends "
            "3-4 descriptions for optimal RSA performance."
        )

    plan = ChangePlan(
        operation="create_responsive_search_ad",
        entity_type="ad",
        account_id=account_id or config.ads.account_id,
        customer_id=customer_id or config.ads.customer_id,
        changes={
            "ad_group_id": ad_group_id,
            "headlines": headlines,
            "descriptions": descriptions,
            "final_url": final_url,
            "path1": path1,
            "path2": path2,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def draft_keywords(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    ad_group_id: str = "",
    keywords: list[dict] | None = None,
) -> dict:
    """Draft keyword additions with match types — returns preview."""
    from bingloop.safety.guards import SafetyViolation, check_blocked_operation
    from bingloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_keywords", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []

    errors = _validate_keywords(ad_group_id, keywords)
    if errors:
        return {"error": "Validation failed", "details": errors}

    warnings = []
    if any(kw.get("match_type", "").upper() == "BROAD" for kw in keywords):
        warnings.append(
            "BROAD match keywords match very loosely on Bing. Verify the target "
            "campaign uses smart bidding (Target CPA / Target ROAS / Maximize "
            "Conversions) before applying — Broad Match with manual bidding "
            "leads to irrelevant matches and wasted budget."
        )

    plan = ChangePlan(
        operation="add_keywords",
        entity_type="keyword",
        account_id=account_id or config.ads.account_id,
        customer_id=customer_id or config.ads.customer_id,
        changes={
            "ad_group_id": ad_group_id,
            "keywords": keywords,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


def add_negative_keywords(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    campaign_id: str = "",
    keywords: list[str] | None = None,
    match_type: str = "EXACT",
) -> dict:
    """Draft campaign-level negative keyword additions — returns preview."""
    from bingloop.safety.guards import SafetyViolation, check_blocked_operation
    from bingloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("add_negative_keywords", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    keywords = keywords or []
    match_type = match_type.upper()

    errors = []
    if not campaign_id:
        errors.append("campaign_id is required")
    if not keywords:
        errors.append("At least one keyword is required")
    if match_type not in _NEGATIVE_MATCH_TYPES:
        errors.append(
            f"Invalid match_type '{match_type}' — Bing negative keywords support "
            "EXACT or PHRASE only (no BROAD negatives)."
        )
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="add_negative_keywords",
        entity_type="negative_keyword",
        entity_id=campaign_id,
        account_id=account_id or config.ads.account_id,
        customer_id=customer_id or config.ads.customer_id,
        changes={
            "campaign_id": campaign_id,
            "keywords": keywords,
            "match_type": match_type,
        },
    )
    store_plan(plan)
    return plan.to_preview()


def pause_entity(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Draft pausing a campaign/ad group/ad/keyword — returns preview."""
    return _draft_status_change(
        config, "pause_entity", customer_id, account_id, entity_type, entity_id, "Paused"
    )


def enable_entity(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Draft enabling a paused entity — returns preview."""
    return _draft_status_change(
        config, "enable_entity", customer_id, account_id, entity_type, entity_id, "Active"
    )


def remove_entity(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Draft removing an entity — DESTRUCTIVE, cannot be undone.

    Returns a preview; call confirm_and_apply to execute.
    """
    from bingloop.safety.guards import SafetyViolation, check_blocked_operation
    from bingloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("remove_entity", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = _validate_entity_ref(entity_type, entity_id, _REMOVABLE_ENTITY_TYPES)
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation="remove_entity",
        entity_type=entity_type,
        entity_id=entity_id,
        account_id=account_id or config.ads.account_id,
        customer_id=customer_id or config.ads.customer_id,
        changes={"action": "DELETE"},
        requires_double_confirm=True,
    )
    store_plan(plan)
    return plan.to_preview()


def draft_campaign(
    config: BingloopConfig,
    *,
    customer_id: str = "",
    account_id: str = "",
    campaign_name: str = "",
    daily_budget: float = 0,
    bidding_strategy: str = "",
    target_cpa: float = 0,
    target_roas: float = 0,
    campaign_type: str = "SEARCH",
    ad_group_name: str = "",
    keywords: list[dict] | None = None,
) -> dict:
    """Draft a full campaign structure — returns preview, does NOT execute.

    Creates: Campaign (PAUSED, with daily budget) + AdGroup + optional Keywords.
    Ads are NOT included — use draft_responsive_search_ad separately after the
    campaign exists.
    """
    from bingloop.safety.guards import (
        SafetyViolation,
        check_blocked_operation,
        check_budget_cap,
    )
    from bingloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("create_campaign", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors, warnings = _validate_campaign(
        campaign_name=campaign_name,
        daily_budget=daily_budget,
        bidding_strategy=bidding_strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        campaign_type=campaign_type,
        keywords=keywords,
    )
    if errors:
        return {"error": "Validation failed", "details": errors}

    try:
        check_budget_cap(daily_budget, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    plan = ChangePlan(
        operation="create_campaign",
        entity_type="campaign",
        account_id=account_id or config.ads.account_id,
        customer_id=customer_id or config.ads.customer_id,
        changes={
            "campaign_name": campaign_name,
            "daily_budget": daily_budget,
            "bidding_strategy": bidding_strategy.upper(),
            "target_cpa": target_cpa if target_cpa else None,
            "target_roas": target_roas if target_roas else None,
            "campaign_type": campaign_type.upper(),
            "ad_group_name": ad_group_name or campaign_name,
            "keywords": keywords,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    if warnings:
        preview["warnings"] = warnings
    return preview


# ---------------------------------------------------------------------------
# confirm_and_apply — the only function that actually mutates Bing Ads
# ---------------------------------------------------------------------------


def confirm_and_apply(
    config: BingloopConfig,
    *,
    plan_id: str = "",
    dry_run: bool = True,
) -> dict:
    """Execute a previously previewed change.

    Defaults to dry_run=True. The caller must explicitly pass dry_run=False
    to make real changes.
    """
    from bingloop.safety.audit import log_mutation
    from bingloop.safety.preview import get_plan, remove_plan

    plan = get_plan(plan_id)
    if plan is None:
        return {
            "error": f"No pending plan found with id '{plan_id}'. "
            "Plans expire when the MCP server restarts.",
        }

    if config.safety.require_dry_run:
        dry_run = True

    if dry_run:
        log_mutation(
            config.safety.log_file,
            operation=plan.operation,
            account_id=plan.account_id,
            customer_id=plan.customer_id,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            changes=plan.changes,
            dry_run=True,
            result="dry_run_success",
        )
        return {
            "status": "DRY_RUN_SUCCESS",
            "plan_id": plan.plan_id,
            "operation": plan.operation,
            "changes": plan.changes,
            "message": (
                "Dry run completed — no changes were made to your Bing Ads account. "
                "To apply for real, call confirm_and_apply again with dry_run=false."
            ),
        }

    try:
        result = _execute_plan(config, plan)
    except Exception as e:
        log_mutation(
            config.safety.log_file,
            operation=plan.operation,
            account_id=plan.account_id,
            customer_id=plan.customer_id,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            changes=plan.changes,
            dry_run=False,
            result="error",
            error=str(e),
        )
        return {"error": str(e), "plan_id": plan.plan_id}

    log_mutation(
        config.safety.log_file,
        operation=plan.operation,
        account_id=plan.account_id,
        customer_id=plan.customer_id,
        entity_type=plan.entity_type,
        entity_id=plan.entity_id,
        changes=plan.changes,
        dry_run=False,
        result="success",
    )
    remove_plan(plan.plan_id)

    return {
        "status": "APPLIED",
        "plan_id": plan.plan_id,
        "operation": plan.operation,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------

_VALID_MATCH_TYPES = {"EXACT", "PHRASE", "BROAD"}
_NEGATIVE_MATCH_TYPES = {"EXACT", "PHRASE"}  # Bing has no broad-match negatives
_VALID_ENTITY_TYPES = {"campaign", "ad_group", "ad", "keyword"}
_REMOVABLE_ENTITY_TYPES = _VALID_ENTITY_TYPES | {"negative_keyword"}

# entity types whose entity_id must be "parentId~childId"
_COMPOSITE_ID_TYPES = {"ad_group", "ad", "keyword", "negative_keyword"}

_VALID_BIDDING_STRATEGIES = {
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
    "TARGET_CPA",
    "TARGET_ROAS",
    "MAXIMIZE_CLICKS",
    "MANUAL_CPC",
    "ENHANCED_CPC",
}

_SMART_BIDDING_STRATEGIES = {
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
    "TARGET_CPA",
    "TARGET_ROAS",
}

_VALID_CAMPAIGN_TYPES = {"SEARCH", "SHOPPING", "AUDIENCE", "PERFORMANCE_MAX"}


def _validate_entity_ref(
    entity_type: str, entity_id: str, valid_types: set[str]
) -> list[str]:
    errors = []
    if entity_type not in valid_types:
        errors.append(
            f"entity_type must be one of {sorted(valid_types)}, got '{entity_type}'"
        )
    if not entity_id:
        errors.append("entity_id is required")
    elif entity_type in _COMPOSITE_ID_TYPES and "~" not in entity_id:
        parent = {
            "ad_group": "campaignId",
            "ad": "adGroupId",
            "keyword": "adGroupId",
            "negative_keyword": "campaignId",
        }[entity_type]
        errors.append(
            f"entity_id for {entity_type} must be '{parent}~{entity_type.replace('_', '')}Id' "
            f"(Bing operations need the parent ID), got '{entity_id}'"
        )
    return errors


def _validate_rsa(
    ad_group_id: str,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
) -> list[str]:
    errors = []
    if not ad_group_id:
        errors.append("ad_group_id is required")
    if not final_url:
        errors.append("final_url is required")
    if len(headlines) < 3:
        errors.append(f"Need at least 3 headlines, got {len(headlines)}")
    if len(headlines) > 15:
        errors.append(f"Maximum 15 headlines, got {len(headlines)}")
    if len(descriptions) < 2:
        errors.append(f"Need at least 2 descriptions, got {len(descriptions)}")
    if len(descriptions) > 4:
        errors.append(f"Maximum 4 descriptions, got {len(descriptions)}")
    for i, h in enumerate(headlines):
        if len(h) > 30:
            errors.append(f"Headline {i + 1} exceeds 30 chars ({len(h)}): '{h}'")
    for i, d in enumerate(descriptions):
        if len(d) > 90:
            errors.append(f"Description {i + 1} exceeds 90 chars ({len(d)}): '{d}'")
    return errors


def _validate_campaign(
    *,
    campaign_name: str,
    daily_budget: float,
    bidding_strategy: str,
    target_cpa: float,
    target_roas: float,
    campaign_type: str,
    keywords: list[dict] | None,
) -> tuple[list[str], list[str]]:
    """Validate campaign draft inputs. Returns (errors, warnings)."""
    errors = []
    warnings = []

    if not campaign_name or not campaign_name.strip():
        errors.append("campaign_name is required")
    if daily_budget <= 0:
        errors.append("daily_budget must be greater than 0")

    bs = bidding_strategy.upper()
    if bs not in _VALID_BIDDING_STRATEGIES:
        errors.append(
            f"bidding_strategy must be one of {sorted(_VALID_BIDDING_STRATEGIES)}, "
            f"got '{bidding_strategy}'"
        )
    if bs == "TARGET_CPA" and not target_cpa:
        errors.append("target_cpa is required when bidding_strategy is TARGET_CPA")
    if bs == "TARGET_ROAS" and not target_roas:
        errors.append("target_roas is required when bidding_strategy is TARGET_ROAS")

    ct = campaign_type.upper()
    if ct not in _VALID_CAMPAIGN_TYPES:
        errors.append(
            f"campaign_type must be one of {sorted(_VALID_CAMPAIGN_TYPES)}, "
            f"got '{campaign_type}'"
        )
    if ct != "SEARCH" and keywords:
        errors.append(f"keywords are only supported for SEARCH campaigns, not {ct}")

    if keywords:
        has_broad = any(
            kw.get("match_type", "").upper() == "BROAD" for kw in keywords
        )
        if has_broad and bs not in _SMART_BIDDING_STRATEGIES:
            errors.append(
                f"BROAD match keywords require smart bidding "
                f"(tCPA/tROAS/Maximize Conversions). "
                f"'{bidding_strategy}' is not a smart bidding strategy. "
                f"Use PHRASE or EXACT match instead."
            )
        for i, kw in enumerate(keywords):
            if not kw.get("text"):
                errors.append(f"Keyword {i + 1} has no text")
            mt = kw.get("match_type", "").upper()
            if mt not in _VALID_MATCH_TYPES:
                errors.append(
                    f"Keyword {i + 1} has invalid match_type '{mt}' "
                    "(must be EXACT, PHRASE, or BROAD)"
                )

    if target_cpa > 0 and daily_budget < 5 * target_cpa:
        warnings.append(
            f"Daily budget {daily_budget:.2f} is less than 5x target CPA "
            f"{target_cpa:.2f}. At least 5x target CPA per day is recommended "
            f"({5 * target_cpa:.2f}/day) for sufficient learning data."
        )

    if bs in ("MANUAL_CPC", "ENHANCED_CPC"):
        warnings.append(
            f"{bs} bidding requires constant monitoring. Consider "
            "MAXIMIZE_CONVERSIONS or TARGET_CPA for automated optimization."
        )

    return errors, warnings


def _validate_keywords(ad_group_id: str, keywords: list[dict]) -> list[str]:
    errors = []
    if not ad_group_id:
        errors.append("ad_group_id is required")
    if not keywords:
        errors.append("At least one keyword is required")
    for i, kw in enumerate(keywords):
        if not kw.get("text"):
            errors.append(f"Keyword {i + 1} has no text")
        mt = kw.get("match_type", "").upper()
        if mt not in _VALID_MATCH_TYPES:
            errors.append(
                f"Keyword {i + 1} has invalid match_type '{mt}' "
                "(must be EXACT, PHRASE, or BROAD)"
            )
    return errors


def _draft_status_change(
    config: BingloopConfig,
    operation: str,
    customer_id: str,
    account_id: str,
    entity_type: str,
    entity_id: str,
    target_status: str,
) -> dict:
    from bingloop.safety.guards import SafetyViolation, check_blocked_operation
    from bingloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation(operation, config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    errors = _validate_entity_ref(entity_type, entity_id, _VALID_ENTITY_TYPES)
    if errors:
        return {"error": "Validation failed", "details": errors}

    plan = ChangePlan(
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        account_id=account_id or config.ads.account_id,
        customer_id=customer_id or config.ads.customer_id,
        changes={"target_status": target_status},
    )
    store_plan(plan)
    return plan.to_preview()


# ---------------------------------------------------------------------------
# Execution — actual Bing Ads Campaign Management REST calls
# ---------------------------------------------------------------------------


def _split_composite(entity_id: str) -> tuple[int, int]:
    parent, child = entity_id.split("~", 1)
    return int(parent), int(child)


def _execute_plan(config: BingloopConfig, plan: object) -> dict:
    """Dispatch to the right Campaign Management call based on plan.operation."""
    from bingloop.ads import campaign_mgmt as cm

    ctx = dict(customer_id=plan.customer_id, account_id=plan.account_id)
    changes = plan.changes

    if plan.operation == "create_campaign":
        return cm.create_campaign_structure(config, changes, **ctx)

    if plan.operation == "create_responsive_search_ad":
        return cm.add_responsive_search_ad(
            config,
            ad_group_id=int(changes["ad_group_id"]),
            headlines=changes["headlines"],
            descriptions=changes["descriptions"],
            final_url=changes["final_url"],
            path1=changes.get("path1", ""),
            path2=changes.get("path2", ""),
            **ctx,
        )

    if plan.operation == "add_keywords":
        return cm.add_keywords(
            config,
            ad_group_id=int(changes["ad_group_id"]),
            keywords=changes["keywords"],
            **ctx,
        )

    if plan.operation == "add_negative_keywords":
        return cm.add_negative_keywords_to_campaign(
            config,
            campaign_id=int(changes["campaign_id"]),
            keywords=changes["keywords"],
            match_type=changes["match_type"],
            **ctx,
        )

    if plan.operation in ("pause_entity", "enable_entity"):
        return cm.update_entity_status(
            config,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            status=changes["target_status"],
            **ctx,
        )

    if plan.operation == "remove_entity":
        return cm.delete_entity(
            config,
            entity_type=plan.entity_type,
            entity_id=plan.entity_id,
            **ctx,
        )

    raise ValueError(f"Unknown operation: {plan.operation}")
