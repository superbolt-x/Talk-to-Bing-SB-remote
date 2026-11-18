"""Tests for in-place RSA swaps and ad-extension swaps (no network)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bingloop.ads import swaps
from bingloop.config import AdsConfig, BingloopConfig, SafetyConfig


def _items(*texts, pins=None):
    pins = pins or {}
    return [{"text": t, "pinned_field": pins.get(t)} for t in texts]


HEADLINES = _items("Erie Home Metal Roofs", "Top Metal Roofing", "One-Day Installation",
                   pins={"Erie Home Metal Roofs": "Headline1"})


@pytest.fixture
def config():
    return BingloopConfig(ads=AdsConfig(account_id="149034657"), safety=SafetyConfig())


class TestApplyTextSwaps:
    def _run(self, current, swap_list):
        return swaps.apply_text_swaps(current, swap_list, kind="headline", max_len=30, min_count=3, max_count=15)

    def test_keeps_pin_and_position(self):
        new, errors = self._run(HEADLINES, [{"old": "Erie Home Metal Roofs", "new": "Erie Home Roofs"}])
        assert errors == []
        assert new[0] == {"text": "Erie Home Roofs", "pinned_field": "Headline1"}

    def test_missing_old(self):
        _, errors = self._run(HEADLINES, [{"old": "Nope", "new": "X"}])
        assert any("is not in this ad" in e for e in errors)

    def test_length(self):
        _, errors = self._run(HEADLINES, [{"old": "Top Metal Roofing", "new": "x" * 31}])
        assert any("max 30" in e for e in errors)

    def test_add(self):
        new, errors = self._run(HEADLINES, [{"old": "", "new": "Book a Consult"}])
        assert errors == [] and new[-1] == {"text": "Book a Consult", "pinned_field": None}


class TestTextAssets:
    def test_pin_only_when_set(self):
        out = swaps._text_assets(HEADLINES[:2])
        assert out[0] == {"Asset": {"Type": "TextAsset", "Text": "Erie Home Metal Roofs"}, "PinnedField": "Headline1"}
        assert "PinnedField" not in out[1]


class TestExtensions:
    def test_sitelink_json(self):
        ext = swaps.build_extension("SITELINK", {
            "link_text": "Erie Home Financing", "description1": "Same-day decisions",
            "description2": "No payments for 90 days", "final_url": "https://x.com",
        })
        assert ext == {"Type": "SitelinkAdExtension", "DisplayText": "Erie Home Financing",
                       "FinalUrls": ["https://x.com"], "Description1": "Same-day decisions",
                       "Description2": "No payments for 90 days"}

    def test_callout_json(self):
        assert swaps.build_extension("CALLOUT", {"text": "Fast Pricing"}) == {
            "Type": "CalloutAdExtension", "Text": "Fast Pricing"}

    def test_sitelink_limits(self):
        errors = swaps.validate_new_extension("SITELINK", {
            "link_text": "x" * 26, "description1": "y" * 36, "description2": "z", "final_url": "u"})
        assert any("max 25" in e for e in errors) and any("max 35" in e for e in errors)

    def test_new_image_upload_not_supported(self):
        assert swaps.validate_new_extension("IMAGE", {"image_url": "https://x/y.png"})
        assert swaps.validate_new_extension("IMAGE", {"extension_id": "8109044365631"}) == []

    def test_association_body(self):
        assert swaps._association_body("149034657", "ad_group", 5, 7) == {
            "AccountId": 149034657,
            "AdExtensionIdToEntityIdAssociations": [{"AdExtensionId": 5, "EntityId": 7}],
            "AssociationType": "AdGroup",
        }


class TestDraftUpdateRsa:
    LIVE = {"ad_id": "1", "status": "Active", "headlines": HEADLINES, "descriptions": _items("D one", "D two")}

    def test_needs_composite_id(self, config):
        out = swaps.draft_update_rsa(config, ad_id="78958898610698", headline_swaps=[{"old": "a", "new": "b"}])
        assert "adGroupId~adId" in out["error"]

    def test_preview(self, config):
        with patch.object(swaps, "read_rsa", return_value=self.LIVE):
            out = swaps.draft_update_rsa(config, ad_id="12~34",
                                         headline_swaps=[{"old": "Top Metal Roofing", "new": "Erie Metal Roofing"}])
        assert out["status"] == "PENDING_CONFIRMATION"
        assert out["changes"]["after"]["headlines"][1]["text"] == "Erie Metal Roofing"
        assert out["changes"]["ad_group_id"] == 12 and out["changes"]["ad_id"] == 34


class TestApplyUpdateRsa:
    def _plan(self):
        return SimpleNamespace(account_id="149034657", changes={
            "ad_group_id": 12, "ad_id": 34,
            "before": {"headlines": HEADLINES, "descriptions": _items("D one", "D two")},
            "after": {"headlines": HEADLINES, "descriptions": _items("D one", "D three")},
        })

    def test_refuses_if_ad_changed(self, config):
        changed = {"headlines": _items("Other", "A", "B"), "descriptions": _items("D one", "D two")}
        with patch.object(swaps, "read_rsa", return_value=changed), patch.object(swaps, "_call") as call:
            with pytest.raises(RuntimeError, match="changed since"):
                swaps.apply_update_rsa(config, self._plan())
        call.assert_not_called()

    def test_sends_full_lists(self, config):
        live = {"headlines": HEADLINES, "descriptions": _items("D one", "D two")}
        with patch.object(swaps, "read_rsa", return_value=live), patch.object(swaps, "_call", return_value={}) as call:
            swaps.apply_update_rsa(config, self._plan())
        path, body = call.call_args.args[1], call.call_args.args[2]
        assert path == "Ads" and call.call_args.kwargs["method"] == "PUT"
        ad = body["Ads"][0]
        assert ad["Id"] == 34 and ad["Type"] == "ResponsiveSearch"
        assert [d["Asset"]["Text"] for d in ad["Descriptions"]] == ["D one", "D three"]


class TestDraftSwapAsset:
    LINKED = [{"extension_id": "777", "type": "SitelinkAdExtension"}]
    NEW = {"link_text": "Erie Home Financing", "description1": "Same-day decisions",
           "description2": "No payments for 90 days", "final_url": "https://x.com"}

    def test_swap(self, config):
        with patch.object(swaps, "read_associations", return_value=self.LINKED):
            out = swaps.draft_swap_asset(config, level="ad_group", parent_id="5", field_type="sitelink",
                                         old_extension_id="777", new_extension=self.NEW)
        assert out["changes"]["action"] == "swap"

    def test_old_not_at_level(self, config):
        with patch.object(swaps, "read_associations", return_value=[]):
            out = swaps.draft_swap_asset(config, level="campaign", parent_id="5", field_type="SITELINK",
                                         old_extension_id="777")
        assert "not associated" in out["error"]

    def test_account_level_uses_account_id(self, config):
        with patch.object(swaps, "read_associations", return_value=[]) as read:
            out = swaps.draft_swap_asset(config, level="account", field_type="CALLOUT", new_extension={"text": "x"})
        assert read.call_args.args[3] == 149034657
        assert out["changes"]["action"] == "add"


class TestApplySwapAsset:
    def _plan(self, new, old):
        return SimpleNamespace(account_id="149034657", changes={
            "level": "ad_group", "entity_id": 5, "field_type": "SITELINK", "new": new, "old": old})

    def test_order_add_set_delete(self, config):
        responses = [{"AdExtensionIdentities": [{"Id": 999, "Version": 1}]}, {}, {}]
        with patch.object(swaps, "_call", side_effect=responses) as call:
            out = swaps.apply_swap_asset(config, self._plan(TestDraftSwapAsset.NEW, {"extension_id": "777"}))
        paths = [c.args[1] for c in call.call_args_list]
        assert paths == ["AdExtensions", "AdExtensionsAssociations/Set", "AdExtensionsAssociations"]
        assert call.call_args_list[2].kwargs["method"] == "DELETE"
        assert out["new_extension_id"] == 999

    def test_delete_failure_reports_state(self, config):
        responses = [{}, RuntimeError("boom")]
        with patch.object(swaps, "_call", side_effect=responses):
            with pytest.raises(RuntimeError, match="both are linked"):
                swaps.apply_swap_asset(config, self._plan({"extension_id": "888"}, {"extension_id": "777"}))

    def test_add_extension_nested_error_surfaces(self, config):
        resp = {"AdExtensionIdentities": [None], "NestedPartialErrors": [
            {"ErrorCode": "CampaignServiceSiteLinkDisplayTextTooLong", "Message": "The site link's display text is too long."}]}
        with patch.object(swaps, "_call", return_value=resp):
            with pytest.raises(RuntimeError, match="DisplayTextTooLong"):
                swaps.apply_swap_asset(config, self._plan(TestDraftSwapAsset.NEW, None))
