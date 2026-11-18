"""In-place copy and ad-extension swaps — edit what is live without new ads.

- ``get_ads``: an ad group's ads with RSA headlines/descriptions, pins, status
  (GetAdsByAdGroupId). The reports only carry AdTitle/AdDescription, so this
  is the only way to see an RSA's current text before swapping it.
- ``draft_update_rsa``: swap specific headlines/descriptions on an existing RSA
  (UpdateAds). The ad keeps its ID and goes back through editorial review.
- ``draft_swap_asset``: replace / add / unlink a sitelink, callout or image ad
  extension at account, campaign or ad group level (AddAdExtensions +
  SetAdExtensionsAssociations + DeleteAdExtensionsAssociations). Bing has no
  atomic multi-op call, so the order is add -> associate -> remove old
  association: a failure part-way leaves both linked, never neither. The old
  extension itself is never deleted.

Bing has no validate-only mode: a dry run is checked by this server, not by Microsoft.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bingloop.ads.accounts import require_account
from bingloop.ads.client import CAMPAIGN_BASE, call_service, raise_on_partial_errors

if TYPE_CHECKING:
    from bingloop.config import BingloopConfig


HEADLINE_MAX = 30
DESCRIPTION_MAX = 90
SITELINK_TEXT_MAX = 25
SITELINK_DESC_MAX = 35
CALLOUT_MAX = 25

ASSET_LEVELS = {"account", "campaign", "ad_group"}
_ASSOCIATION_TYPE = {"account": "Account", "campaign": "Campaign", "ad_group": "AdGroup"}
_EXTENSION_TYPE = {
    "SITELINK": "SitelinkAdExtension",
    "CALLOUT": "CalloutAdExtension",
    "IMAGE": "ImageAdExtension",
}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without the API)
# ---------------------------------------------------------------------------


def apply_text_swaps(
    current: list[dict],
    swaps: list[dict],
    *,
    kind: str,
    max_len: int,
    min_count: int,
    max_count: int,
) -> tuple[list[dict], list[str]]:
    """Apply ``swaps`` ([{"old", "new"}]) to ``current`` ([{"text", "pinned_field"}]).

    The replaced entry keeps its position and pin; ``old`` empty adds ``new``
    unpinned at the end. Returns (new_list, errors).
    """
    errors: list[str] = []
    result = [dict(item) for item in current]

    for i, swap in enumerate(swaps or []):
        old = (swap.get("old") or "").strip()
        new = (swap.get("new") or "").strip()
        label = f"{kind} swap #{i + 1}"
        if not new:
            errors.append(f"{label}: 'new' is required")
            continue
        if len(new) > max_len:
            errors.append(f"{label}: '{new}' is {len(new)} chars (max {max_len})")
        if not old:
            result.append({"text": new, "pinned_field": None})
            continue
        matches = [j for j, item in enumerate(result) if item["text"] == old]
        if not matches:
            errors.append(
                f"{label}: '{old}' is not in this ad (it may already have "
                "been swapped, or the text differs)"
            )
            continue
        if len(matches) > 1:
            errors.append(f"{label}: '{old}' appears {len(matches)} times")
            continue
        result[matches[0]]["text"] = new

    texts = [item["text"] for item in result]
    dupes = sorted({t for t in texts if texts.count(t) > 1})
    if dupes:
        errors.append(f"Duplicate {kind}s after swaps: {dupes}")
    if not min_count <= len(result) <= max_count:
        errors.append(f"{len(result)} {kind}s after swaps (must be {min_count}-{max_count})")
    return result, errors


def validate_new_extension(field_type: str, new: dict) -> list[str]:
    errors: list[str] = []
    if new.get("extension_id"):
        return errors
    if field_type == "SITELINK":
        text = new.get("link_text", "")
        if not text:
            errors.append("SITELINK needs link_text")
        elif len(text) > SITELINK_TEXT_MAX:
            errors.append(f"link_text '{text}' is {len(text)} chars (max {SITELINK_TEXT_MAX})")
        d1, d2 = new.get("description1", ""), new.get("description2", "")
        if bool(d1) != bool(d2):
            errors.append("SITELINK descriptions: give both description1 and description2, or neither")
        for name, val in (("description1", d1), ("description2", d2)):
            if len(val) > SITELINK_DESC_MAX:
                errors.append(f"{name} '{val}' is {len(val)} chars (max {SITELINK_DESC_MAX})")
        if not new.get("final_url"):
            errors.append("SITELINK needs final_url")
    elif field_type == "CALLOUT":
        text = new.get("text", "")
        if not text:
            errors.append("CALLOUT needs text")
        elif len(text) > CALLOUT_MAX:
            errors.append(f"callout '{text}' is {len(text)} chars (max {CALLOUT_MAX})")
    elif field_type == "IMAGE":
        errors.append(
            "IMAGE: give extension_id (an image extension already in the account). "
            "Uploading new images is not supported yet."
        )
    return errors


def build_extension(field_type: str, new: dict) -> dict:
    """The AdExtension JSON for AddAdExtensions."""
    if field_type == "SITELINK":
        ext = {
            "Type": "SitelinkAdExtension",
            "DisplayText": new["link_text"],
            "FinalUrls": [new["final_url"]],
        }
        if new.get("description1"):
            ext["Description1"] = new["description1"]
            ext["Description2"] = new["description2"]
        return ext
    if field_type == "CALLOUT":
        return {"Type": "CalloutAdExtension", "Text": new["text"]}
    raise ValueError(f"Cannot build a new {field_type} extension")


def _text_assets(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        entry: dict = {"Asset": {"Type": "TextAsset", "Text": item["text"]}}
        if item.get("pinned_field"):
            entry["PinnedField"] = item["pinned_field"]
        out.append(entry)
    return out


def _split(entity_id: str) -> tuple[int, int]:
    parent, child = str(entity_id).split("~", 1)
    return int(parent), int(child)


# ---------------------------------------------------------------------------
# API reads
# ---------------------------------------------------------------------------


def _call(config, path, body, account_id, method="POST"):
    return call_service(config, CAMPAIGN_BASE, path, body, account_id=account_id, method=method)


def _ad_summary(ad: dict) -> dict:
    out = {
        "ad_id": str(ad["Id"]),
        "type": ad.get("Type"),
        "status": ad.get("Status"),
        "editorial_status": ad.get("EditorialStatus"),
        "final_urls": (ad.get("FinalUrls") or []),
        "path1": ad.get("Path1"),
        "path2": ad.get("Path2"),
    }
    if ad.get("Type") == "ResponsiveSearch":
        out["headlines"] = [
            {"text": h["Asset"]["Text"], "pinned_field": h.get("PinnedField")}
            for h in ad.get("Headlines") or []
        ]
        out["descriptions"] = [
            {"text": d["Asset"]["Text"], "pinned_field": d.get("PinnedField")}
            for d in ad.get("Descriptions") or []
        ]
    return out


def get_ads(config: BingloopConfig, *, ad_group_id: str, account_id: str = "") -> dict:
    """Every ad in an ad group, with RSA text and pins."""
    account_id = require_account(config, account_id)
    resp = _call(
        config, "Ads/QueryByAdGroupId",
        {"AdGroupId": int(ad_group_id),
         "AdTypes": ["ResponsiveSearch", "ExpandedText", "DynamicSearch", "ResponsiveAd"]},
        account_id,
    )
    ads = [_ad_summary(a) for a in resp.get("Ads") or [] if a]
    return {"ad_group_id": str(ad_group_id), "ads": ads, "total_ads": len(ads)}


def read_rsa(config: BingloopConfig, account_id: str, ad_group_id: int, ad_id: int) -> dict:
    resp = _call(
        config, "Ads/QueryByIds",
        {"AdGroupId": ad_group_id, "AdIds": [ad_id], "AdTypes": ["ResponsiveSearch"]},
        account_id,
    )
    ads = [a for a in resp.get("Ads") or [] if a]
    if not ads:
        return {"error": f"Ad {ad_id} not found in ad group {ad_group_id} (or it is not a Responsive Search Ad)"}
    return _ad_summary(ads[0])


def read_associations(
    config: BingloopConfig, account_id: str, level: str, entity_id: int, field_type: str
) -> list[dict]:
    resp = _call(
        config, "AdExtensionsAssociations/Query",
        {"AccountId": int(account_id), "AdExtensionType": _EXTENSION_TYPE[field_type],
         "AssociationType": _ASSOCIATION_TYPE[level], "EntityIds": [entity_id]},
        account_id,
    )
    out = []
    for coll in resp.get("AdExtensionAssociationCollection") or []:
        for assoc in (coll or {}).get("AdExtensionAssociations") or []:
            ext = assoc["AdExtension"]
            out.append({
                "extension_id": str(ext["Id"]),
                "type": ext["Type"],
                "link_text": ext.get("DisplayText"),
                "description1": ext.get("Description1"),
                "description2": ext.get("Description2"),
                "callout_text": ext.get("Text"),
                "final_urls": ext.get("FinalUrls") or [],
                "editorial_status": assoc.get("EditorialStatus"),
            })
    return out


# ---------------------------------------------------------------------------
# Draft tools
# ---------------------------------------------------------------------------


def draft_update_rsa(
    config: BingloopConfig,
    *,
    account_id: str = "",
    ad_id: str = "",
    headline_swaps: list[dict] | None = None,
    description_swaps: list[dict] | None = None,
) -> dict:
    """Draft in-place headline/description swaps on an existing RSA ("adGroupId~adId")."""
    from bingloop.safety.guards import SafetyViolation, check_blocked_operation
    from bingloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("update_responsive_search_ad", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    try:
        ad_group_id, ad_num = _split(ad_id)
    except (ValueError, AttributeError):
        return {"error": "ad_id must be 'adGroupId~adId' (Bing needs the ad group ID)"}
    if not headline_swaps and not description_swaps:
        return {"error": "Give at least one headline_swaps or description_swaps entry"}

    account_id = require_account(config, account_id)
    current = read_rsa(config, account_id, ad_group_id, ad_num)
    if "error" in current:
        return current

    new_h, err_h = apply_text_swaps(
        current["headlines"], headline_swaps or [],
        kind="headline", max_len=HEADLINE_MAX, min_count=3, max_count=15,
    )
    new_d, err_d = apply_text_swaps(
        current["descriptions"], description_swaps or [],
        kind="description", max_len=DESCRIPTION_MAX, min_count=2, max_count=4,
    )
    if err_h or err_d:
        return {"error": "Validation failed", "details": err_h + err_d}

    plan = ChangePlan(
        operation="update_responsive_search_ad",
        entity_type="ad",
        entity_id=str(ad_id),
        account_id=account_id,
        customer_id="",
        changes={
            "ad_group_id": ad_group_id,
            "ad_id": ad_num,
            "status": current["status"],
            "headline_swaps": headline_swaps or [],
            "description_swaps": description_swaps or [],
            "before": {"headlines": current["headlines"], "descriptions": current["descriptions"]},
            "after": {"headlines": new_h, "descriptions": new_d},
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = [
        "The ad keeps its ID but goes back through editorial review.",
        "Bing has no validate-only mode: dry runs are checked by this server only.",
    ]
    return preview


def draft_swap_asset(
    config: BingloopConfig,
    *,
    account_id: str = "",
    level: str = "",
    parent_id: str = "",
    field_type: str = "",
    old_extension_id: str = "",
    new_extension: dict | None = None,
) -> dict:
    """Draft replacing/adding/unlinking a sitelink, callout or image extension."""
    from bingloop.safety.guards import SafetyViolation, check_blocked_operation
    from bingloop.safety.preview import ChangePlan, store_plan

    try:
        check_blocked_operation("swap_asset", config.safety)
    except SafetyViolation as e:
        return {"error": str(e)}

    field_type = (field_type or "").upper()
    new = dict(new_extension or {})
    errors = []
    if level not in ASSET_LEVELS:
        errors.append(f"level must be one of {sorted(ASSET_LEVELS)}")
    if level in ("campaign", "ad_group") and not str(parent_id).isdigit():
        errors.append(f"parent_id (the {level} ID) is required for level '{level}'")
    if field_type not in _EXTENSION_TYPE:
        errors.append(f"field_type must be one of {sorted(_EXTENSION_TYPE)}")
    if not old_extension_id and not new:
        errors.append("Give old_extension_id (to unlink), new_extension (to add), or both (to swap)")
    if old_extension_id and not str(old_extension_id).isdigit():
        errors.append("old_extension_id must be the numeric extension ID")
    if new and field_type in _EXTENSION_TYPE:
        errors += validate_new_extension(field_type, new)
    if errors:
        return {"error": "Validation failed", "details": errors}

    account_id = require_account(config, account_id)
    entity_id = int(account_id) if level == "account" else int(parent_id)

    linked = read_associations(config, account_id, level, entity_id, field_type)
    linked_ids = {a["extension_id"] for a in linked}

    old = None
    if old_extension_id:
        old = next((a for a in linked if a["extension_id"] == str(old_extension_id)), None)
        if old is None:
            return {
                "error": f"Extension {old_extension_id} is not associated with {level} "
                f"{entity_id} as {field_type}. Check the level: an extension on the "
                "campaign is not removed from an ad group, and vice versa."
            }
    if new.get("extension_id") and str(new["extension_id"]) in linked_ids:
        return {"error": f"Extension {new['extension_id']} is already associated with this {level}"}

    action = "swap" if old and new else ("unlink" if old else "add")
    plan = ChangePlan(
        operation="swap_asset",
        entity_type=f"{level}_extension",
        entity_id=str(entity_id),
        account_id=account_id,
        customer_id="",
        changes={
            "action": action,
            "level": level,
            "entity_id": entity_id,
            "field_type": field_type,
            "old": old,
            "new": new or None,
        },
    )
    store_plan(plan)
    preview = plan.to_preview()
    preview["warnings"] = (
        ["Only the association is removed; the old extension stays wherever else it is used."]
        if old else []
    ) + ["Bing has no validate-only mode: dry runs are checked by this server only."]
    return preview


# ---------------------------------------------------------------------------
# Apply (called from write._execute_plan)
# ---------------------------------------------------------------------------


def apply_update_rsa(config: BingloopConfig, plan) -> dict:
    ch = plan.changes
    live = read_rsa(config, plan.account_id, ch["ad_group_id"], ch["ad_id"])
    if "error" in live:
        raise RuntimeError(live["error"])
    if live["headlines"] != ch["before"]["headlines"] or live["descriptions"] != ch["before"]["descriptions"]:
        raise RuntimeError(
            "The ad changed since this plan was drafted. Draft it again so the "
            "swaps apply to the current text."
        )
    body = {
        "AdGroupId": ch["ad_group_id"],
        "Ads": [{
            "Id": ch["ad_id"],
            "Type": "ResponsiveSearch",
            "Headlines": _text_assets(ch["after"]["headlines"]),
            "Descriptions": _text_assets(ch["after"]["descriptions"]),
        }],
    }
    resp = _call(config, "Ads", body, plan.account_id, method="PUT")
    raise_on_partial_errors(resp, "UpdateAds")
    return {"ad_group_id": ch["ad_group_id"], "ad_id": ch["ad_id"], "updated": True}


def _association_body(account_id: str, level: str, extension_id: int, entity_id: int) -> dict:
    return {
        "AccountId": int(account_id),
        "AdExtensionIdToEntityIdAssociations": [
            {"AdExtensionId": int(extension_id), "EntityId": int(entity_id)}
        ],
        "AssociationType": _ASSOCIATION_TYPE[level],
    }


def apply_swap_asset(config: BingloopConfig, plan) -> dict:
    ch = plan.changes
    level, entity_id, field_type = ch["level"], ch["entity_id"], ch["field_type"]
    new, old = ch.get("new"), ch.get("old")
    acct = plan.account_id
    steps: list[str] = []

    new_id = None
    if new:
        if new.get("extension_id"):
            new_id = int(new["extension_id"])
        else:
            resp = _call(
                config, "AdExtensions",
                {"AccountId": int(acct), "AdExtensions": [build_extension(field_type, new)]},
                acct,
            )
            raise_on_partial_errors(resp, "AddAdExtensions")
            nested = [e for e in (resp.get("NestedPartialErrors") or []) if e]
            if nested:
                raise RuntimeError("AddAdExtensions failed: " + "; ".join(
                    f"{e.get('ErrorCode', '')}: {e.get('Message', '')}" for e in nested))
            identities = resp.get("AdExtensionIdentities") or []
            if not identities or not identities[0]:
                raise RuntimeError(f"AddAdExtensions returned no ID: {resp}")
            new_id = int(identities[0]["Id"])
            steps.append(f"created extension {new_id}")

        resp = _call(
            config, "AdExtensionsAssociations/Set",
            _association_body(acct, level, new_id, entity_id), acct,
        )
        raise_on_partial_errors(resp, "SetAdExtensionsAssociations")
        steps.append(f"associated {new_id} with {level} {entity_id}")

    if old:
        try:
            resp = _call(
                config, "AdExtensionsAssociations",
                _association_body(acct, level, int(old["extension_id"]), entity_id), acct,
                method="DELETE",
            )
            raise_on_partial_errors(resp, "DeleteAdExtensionsAssociations")
        except Exception as e:
            raise RuntimeError(
                f"Done so far: {steps or ['nothing']}. Removing the old association "
                f"({old['extension_id']}) failed, so both are linked now: {e}"
            ) from e
        steps.append(f"removed {old['extension_id']} from {level} {entity_id}")

    return {"new_extension_id": new_id, "steps": steps}
