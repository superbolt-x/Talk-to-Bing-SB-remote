"""Tests for REST client helpers — CSV parsing, report request building, partial errors."""

import pytest

from bingloop.ads.client import (
    build_report_request,
    raise_on_partial_errors,
    to_float,
    to_int,
)


class TestNumberParsing:
    def test_strips_percent_and_keeps_percent_units(self):
        assert to_float("12.5%") == 12.5

    def test_strips_thousands_separator(self):
        assert to_int("1,234") == 1234

    def test_blank_is_none(self):
        assert to_float("") is None
        assert to_int("--") is None

    def test_plain_numbers(self):
        assert to_float("3.14") == 3.14
        assert to_int("42") == 42

    def test_none_passthrough(self):
        assert to_float(None) is None


class TestReportRequest:
    def test_summary_uses_predefined_time_by_default(self):
        req = build_report_request(
            "CampaignPerformanceReportRequest", ["CampaignId", "Spend"],
            {"AccountIds": [1]},
        )
        assert req["Time"]["PredefinedTime"] == "Last30Days"
        assert req["Aggregation"] == "Summary"
        assert "CustomDateRangeStart" not in req["Time"]

    def test_custom_dates_are_inclusive_date_parts(self):
        req = build_report_request(
            "CampaignPerformanceReportRequest", ["CampaignId", "Spend"],
            {"AccountIds": [1]},
            date_range_start="2026-06-01", date_range_end="2026-06-30",
        )
        assert req["Time"]["CustomDateRangeStart"] == {"Year": 2026, "Month": 6, "Day": 1}
        assert req["Time"]["CustomDateRangeEnd"] == {"Year": 2026, "Month": 6, "Day": 30}
        assert "PredefinedTime" not in req["Time"]

    def test_report_type_is_the_type_discriminator(self):
        req = build_report_request("KeywordPerformanceReportRequest", ["Spend"], {})
        assert req["Type"] == "KeywordPerformanceReportRequest"


class TestPartialErrors:
    def test_no_errors_passes(self):
        raise_on_partial_errors({"PartialErrors": []}, "AddCampaigns")
        raise_on_partial_errors({}, "AddCampaigns")

    def test_real_error_raises(self):
        resp = {"PartialErrors": [{"Code": 123, "ErrorCode": "CampaignServiceInvalidBudget",
                                   "Message": "bad budget"}]}
        with pytest.raises(RuntimeError, match="partial errors"):
            raise_on_partial_errors(resp, "AddCampaigns")
