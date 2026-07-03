"""Account discovery + account→customer (AID→CID) resolution.

This server is multi-tenant: it targets any ad account the authenticated user
can reach, across ALL customers — not one hardcoded manager. Microsoft's
`CustomerId` header is not a universal manager key: to call Reporting / Campaign
Management / Ad Insight for an account (CustomerAccountId=AID), the `CustomerId`
header must be the customer *through which the user reaches that account*, or
the call fails with 106 UserIsNotAuthorized / 1490 InvalidCustomerId.

So we discover the full picture once and cache it:

  * GetUser (nil UserId) -> the user's Id + CustomerRoles. Each CustomerRole is
    {CustomerId, AccountIds, LinkedAccountIds}: the AUTHORITATIVE managing CID
    for every account reachable through it (this also covers accounts linked
    into a sub-manager, where the managing CID differs from the owning one).
  * SearchAccounts by UserId (paged) -> every AdvertiserAccount across all
    customers, with Name / Number / status / ParentCustomerId for display and
    as a CID fallback.

resolve_customer_id(account_id) then returns the correct header CID for any
account. The map is cached in-process and refreshed lazily.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from bingloop.ads.client import CUSTOMER_BASE, call_service

if TYPE_CHECKING:
    from bingloop.config import BingloopConfig

logger = logging.getLogger("bingloop.accounts")

_PAGE_SIZE = 100


class MissingAccount(RuntimeError):
    """No account_id was given and no default is configured."""


def require_account(config: BingloopConfig, account_id: str) -> str:
    """Resolve the target account: explicit arg, else configured default, else error."""
    resolved = account_id or config.ads.account_id
    if not resolved:
        raise MissingAccount(
            "No account_id provided and no default configured. Call list_accounts "
            "to see the accounts you can access, then pass account_id."
        )
    return resolved

_lock = threading.Lock()
# Cached discovery result: {"accounts": [...], "cid_by_aid": {aid: cid}} or None.
_cache: dict | None = None


def _build_cid_map(roles: list[dict], accounts: list[dict]) -> tuple[list[dict], dict[int, int]]:
    """Pure merge of GetUser CustomerRoles + SearchAccounts results.

    CustomerRoles win for the header CID (they encode the managing customer,
    which for a linked account is the CID you must send — not always the owning
    ParentCustomerId). SearchAccounts supplies display metadata and a CID
    fallback for any account a role didn't cover.
    """
    cid_by_aid: dict[int, int] = {}
    for role in roles or []:
        cid = role.get("CustomerId")
        if cid is None:
            continue
        reachable = (role.get("AccountIds") or []) + (role.get("LinkedAccountIds") or [])
        for aid in reachable:
            cid_by_aid.setdefault(int(aid), int(cid))

    records = []
    for a in accounts or []:
        if a.get("Id") is None:
            continue
        aid = int(a["Id"])
        parent = a.get("ParentCustomerId")
        if aid not in cid_by_aid and parent is not None:
            cid_by_aid[aid] = int(parent)
        records.append({
            "account_id": aid,
            "account_name": a.get("Name"),
            "account_number": a.get("Number"),
            "customer_id": cid_by_aid.get(aid),
            "parent_customer_id": parent,
            "status": a.get("AccountLifeCycleStatus"),
            "pause_reason": a.get("PauseReason"),
        })

    records.sort(key=lambda r: (r["account_name"] or "").lower())
    return records, cid_by_aid


def _discover(config: BingloopConfig) -> dict:
    """Call GetUser + SearchAccounts and assemble the account list + CID map."""
    user_resp = call_service(config, CUSTOMER_BASE, "User/Query", {"UserId": None})
    user = user_resp.get("User") or {}
    user_id = user.get("Id")
    # CustomerRoles may sit at the response top level or inside User — accept both.
    roles = user_resp.get("CustomerRoles") or user.get("CustomerRoles") or []

    accounts: list[dict] = []
    if user_id is not None:
        index = 0
        while True:
            resp = call_service(
                config, CUSTOMER_BASE, "Accounts/Search",
                {
                    "Predicates": [
                        {"Field": "UserId", "Operator": "Equals", "Value": str(user_id)}
                    ],
                    "Ordering": [{"Field": "Id", "Order": "Ascending"}],
                    "PageInfo": {"Index": index, "Size": _PAGE_SIZE},
                    "ReturnAdditionalFields": None,
                },
            )
            page = resp.get("Accounts") or []
            accounts.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            index += 1

    records, cid_by_aid = _build_cid_map(roles, accounts)
    logger.info("Discovered %d accessible Bing Ads accounts across %d customers.",
                len(records), len({c for c in cid_by_aid.values()}))
    return {"accounts": records, "cid_by_aid": cid_by_aid, "user_id": user_id}


def get_accounts(config: BingloopConfig, *, refresh: bool = False) -> dict:
    """Return cached discovery (accounts + CID map), discovering on first use."""
    global _cache
    with _lock:
        if _cache is None or refresh:
            _cache = _discover(config)
        return _cache


def resolve_customer_id(config: BingloopConfig, account_id: str) -> str:
    """Return the header CustomerId (CID) to use for a given ad account (AID).

    Falls back to a configured BING_ADS_CUSTOMER_ID when the account isn't in
    the discovered map (e.g. a brand-new account not yet propagated). Raises a
    guiding error when neither is available.
    """
    if not account_id:
        # No account selected: use the configured default CID if any (may be "").
        return config.ads.customer_id

    aid = int(account_id)
    cid = get_accounts(config)["cid_by_aid"].get(aid)
    if cid is not None:
        return str(cid)

    if config.ads.customer_id:
        logger.warning("Account %s not found in discovery; falling back to "
                       "configured BING_ADS_CUSTOMER_ID.", account_id)
        return config.ads.customer_id

    raise RuntimeError(
        f"Account {account_id} is not accessible by the authenticated user, or "
        "discovery is stale. Call list_accounts to see available accounts."
    )
