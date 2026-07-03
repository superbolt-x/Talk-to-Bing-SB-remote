"""Tests for write-tool validation and bidding-scheme mapping (no network)."""

import pytest

from bingloop.ads import campaign_mgmt as cm
from bingloop.ads.write import (
    _validate_campaign,
    _validate_entity_ref,
    _REMOVABLE_ENTITY_TYPES,
    _VALID_ENTITY_TYPES,
)


class TestBiddingScheme:
    def test_maximize_conversions(self):
        assert cm._bidding_scheme("MAXIMIZE_CONVERSIONS", None, None) == {
            "Type": "MaxConversionsBiddingScheme"
        }

    def test_target_cpa_maps_onto_maxconversions(self):
        # TargetCpaBiddingScheme was removed for new campaigns (Aug 2025).
        scheme = cm._bidding_scheme("TARGET_CPA", 25.0, None)
        assert scheme == {"Type": "MaxConversionsBiddingScheme", "TargetCpa": 25.0}

    def test_target_roas_maps_onto_maxconversionvalue(self):
        scheme = cm._bidding_scheme("TARGET_ROAS", None, 4.0)
        assert scheme == {"Type": "MaxConversionValueBiddingScheme", "TargetRoas": 4.0}

    def test_manual_cpc(self):
        assert cm._bidding_scheme("MANUAL_CPC", None, None) == {"Type": "ManualCpcBiddingScheme"}

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            cm._bidding_scheme("NONSENSE", None, None)


class TestCompositeIdValidation:
    def test_campaign_needs_no_tilde(self):
        assert _validate_entity_ref("campaign", "12345", _VALID_ENTITY_TYPES) == []

    def test_ad_group_requires_parent_id(self):
        errors = _validate_entity_ref("ad_group", "12345", _VALID_ENTITY_TYPES)
        assert errors and "campaignId~" in errors[0]

    def test_ad_group_with_composite_ok(self):
        assert _validate_entity_ref("ad_group", "111~222", _VALID_ENTITY_TYPES) == []

    def test_negative_keyword_removable(self):
        assert _validate_entity_ref("negative_keyword", "111~222", _REMOVABLE_ENTITY_TYPES) == []

    def test_missing_id(self):
        errors = _validate_entity_ref("campaign", "", _VALID_ENTITY_TYPES)
        assert any("required" in e for e in errors)


class TestCampaignValidation:
    def test_valid_search_campaign(self):
        errors, _ = _validate_campaign(
            campaign_name="Test", daily_budget=20.0,
            bidding_strategy="MAXIMIZE_CONVERSIONS", target_cpa=0, target_roas=0,
            campaign_type="SEARCH", keywords=None,
        )
        assert errors == []

    def test_target_cpa_requires_value(self):
        errors, _ = _validate_campaign(
            campaign_name="Test", daily_budget=20.0,
            bidding_strategy="TARGET_CPA", target_cpa=0, target_roas=0,
            campaign_type="SEARCH", keywords=None,
        )
        assert any("target_cpa is required" in e for e in errors)

    def test_broad_match_needs_smart_bidding(self):
        errors, _ = _validate_campaign(
            campaign_name="Test", daily_budget=20.0,
            bidding_strategy="MANUAL_CPC", target_cpa=0, target_roas=0,
            campaign_type="SEARCH",
            keywords=[{"text": "shoes", "match_type": "BROAD"}],
        )
        assert any("BROAD" in e for e in errors)

    def test_keywords_rejected_for_non_search(self):
        errors, _ = _validate_campaign(
            campaign_name="Test", daily_budget=20.0,
            bidding_strategy="MAXIMIZE_CONVERSIONS", target_cpa=0, target_roas=0,
            campaign_type="PERFORMANCE_MAX",
            keywords=[{"text": "shoes", "match_type": "EXACT"}],
        )
        assert any("SEARCH" in e for e in errors)

    def test_low_budget_warns(self):
        _, warnings = _validate_campaign(
            campaign_name="Test", daily_budget=20.0,
            bidding_strategy="TARGET_CPA", target_cpa=25.0, target_roas=0,
            campaign_type="SEARCH", keywords=None,
        )
        assert any("5x target CPA" in w for w in warnings)
