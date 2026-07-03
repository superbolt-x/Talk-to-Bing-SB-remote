"""Tests for multi-customer account discovery / AID->CID resolution (no network)."""

import pytest

from bingloop.ads.accounts import MissingAccount, _build_cid_map, require_account
from bingloop.config import AdsConfig, BingloopConfig


class TestBuildCidMap:
    def test_role_maps_direct_and_linked_accounts(self):
        roles = [
            {"CustomerId": 100, "AccountIds": [11, 12], "LinkedAccountIds": [13]},
            {"CustomerId": 200, "AccountIds": [21], "LinkedAccountIds": []},
        ]
        _, cid = _build_cid_map(roles, [])
        assert cid == {11: 100, 12: 100, 13: 100, 21: 200}

    def test_managing_cid_wins_over_parent_customer(self):
        # Account 13 is reached THROUGH customer 100 (a sub-manager link) even
        # though its owning ParentCustomerId is 999 — the header must use 100.
        roles = [{"CustomerId": 100, "AccountIds": [], "LinkedAccountIds": [13]}]
        accounts = [{"Id": 13, "Name": "Client", "ParentCustomerId": 999}]
        records, cid = _build_cid_map(roles, accounts)
        assert cid[13] == 100
        assert records[0]["customer_id"] == 100
        assert records[0]["parent_customer_id"] == 999

    def test_parent_customer_fallback_when_no_role(self):
        accounts = [{"Id": 50, "Name": "Solo", "ParentCustomerId": 500}]
        records, cid = _build_cid_map([], accounts)
        assert cid[50] == 500
        assert records[0]["customer_id"] == 500

    def test_spans_multiple_customers(self):
        accounts = [
            {"Id": 1, "Name": "B acct", "ParentCustomerId": 10},
            {"Id": 2, "Name": "A acct", "ParentCustomerId": 20},
        ]
        records, cid = _build_cid_map([], accounts)
        assert {r["customer_id"] for r in records} == {10, 20}
        # sorted by account_name -> "A acct" first
        assert records[0]["account_name"] == "A acct"


class TestRequireAccount:
    def test_explicit_account_wins(self):
        cfg = BingloopConfig(ads=AdsConfig(account_id="999"))
        assert require_account(cfg, "123") == "123"

    def test_falls_back_to_default(self):
        cfg = BingloopConfig(ads=AdsConfig(account_id="999"))
        assert require_account(cfg, "") == "999"

    def test_raises_when_no_account_anywhere(self):
        cfg = BingloopConfig(ads=AdsConfig(account_id=""))
        with pytest.raises(MissingAccount):
            require_account(cfg, "")
