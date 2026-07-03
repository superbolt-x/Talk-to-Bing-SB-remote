"""Microsoft Advertising REST plumbing — shared HTTP, auth headers, Reporting API.

All Bing Ads v13 services are called over plain REST/JSON with ``requests`` —
no SOAP SDK. The Reporting flow (submit → poll → download zipped CSV → parse)
is ported from Superbolt's proven ETL scripts.

Report value formats: numeric cells arrive as CSV strings, percent columns
include a trailing ``%`` (values stay in percent units), thousands separators
appear in large numbers, and blank cells mean "no data". ``to_int``/``to_float``
normalize these.
"""

from __future__ import annotations

import csv
import io
import logging
import random
import time
import zipfile
from typing import TYPE_CHECKING, Any

import requests

from bingloop.auth import BingAdsAuthError, get_access_token

if TYPE_CHECKING:
    from bingloop.config import BingloopConfig

logger = logging.getLogger("bingloop.client")

REPORTING_BASE = "https://reporting.api.bingads.microsoft.com/Reporting/v13"
CAMPAIGN_BASE = "https://campaign.api.bingads.microsoft.com/CampaignManagement/v13"
CUSTOMER_BASE = "https://clientcenter.api.bingads.microsoft.com/CustomerManagement/v13"
ADINSIGHT_BASE = "https://adinsight.api.bingads.microsoft.com/AdInsight/v13"

REPORT_TIME_ZONE = "EasternTimeUSCanada"


# ---------------------------------------------------------------------------
# Shared HTTP
# ---------------------------------------------------------------------------


def _request_with_retry(
    method: str,
    url: str,
    max_attempts: int = 5,
    base_delay: float = 1.5,
    **kwargs: Any,
) -> requests.Response:
    """requests.request with exponential backoff + jitter.

    Retries on network errors, 429, and 5xx (respecting Retry-After). Does NOT
    retry other 4xx — those need a code fix or a fresh token, not a retry.
    """
    last_exception: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, timeout=60, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exception = exc
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "Network error on attempt %d/%d (%s). Retrying in %.1fs.",
                attempt, max_attempts, exc, delay,
            )
            time.sleep(delay)
            continue

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, 0.5)
            logger.warning(
                "Got HTTP %d on attempt %d/%d. Retrying in %.1fs. Body: %s",
                resp.status_code, attempt, max_attempts, delay, resp.text[:300],
            )
            time.sleep(delay)
            last_exception = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            continue

        return resp

    raise RuntimeError(
        f"Request to {url} failed after {max_attempts} attempts."
    ) from last_exception


def build_headers(
    config: BingloopConfig,
    customer_id: str,
    account_id: str,
    *,
    include_account_headers: bool = True,
) -> dict:
    """Standard Bing Ads REST headers.

    Campaign Management / Reporting / Ad Insight need all four headers;
    Customer Management takes only Authorization + DeveloperToken
    (CustomerId/CustomerAccountId are "not applicable" there).
    """
    headers = {
        "Authorization": f"Bearer {get_access_token(config)}",
        "DeveloperToken": config.ads.developer_token,
        "Content-Type": "application/json",
    }
    if include_account_headers:
        headers["CustomerId"] = str(customer_id)
        headers["CustomerAccountId"] = str(account_id)
    return headers


def call_service(
    config: BingloopConfig,
    base: str,
    path: str,
    body: dict,
    *,
    customer_id: str = "",
    account_id: str = "",
    method: str = "POST",
) -> dict:
    """POST a JSON body to a Bing Ads REST service and return the parsed response.

    Retries once with a forced token refresh on 401 (access token invalidated
    mid-flight). Raises RuntimeError with the response body on other errors so
    tool callers surface actionable API messages (invalid column, bad ID, ...).
    """
    account_id = account_id or config.ads.account_id
    url = f"{base}/{path.lstrip('/')}"
    include_account = base != CUSTOMER_BASE

    # Customer Management ignores the account headers, so skip CID resolution
    # there (it would recurse — discovery itself calls Customer Management).
    if include_account and not customer_id:
        from bingloop.ads.accounts import resolve_customer_id

        customer_id = resolve_customer_id(config, account_id)
    elif not customer_id:
        customer_id = config.ads.customer_id

    headers = build_headers(config, customer_id, account_id,
                            include_account_headers=include_account)
    resp = _request_with_retry(method, url, headers=headers, json=body)

    if resp.status_code == 401:
        logger.warning("Got 401 on %s. Forcing token refresh and retrying once.", path)
        get_access_token(config, force_refresh=True)
        headers = build_headers(config, customer_id, account_id,
                                include_account_headers=include_account)
        resp = _request_with_retry(method, url, headers=headers, json=body)

    if resp.status_code >= 400:
        raise RuntimeError(f"{path} failed: HTTP {resp.status_code} — {resp.text[:2000]}")

    if not resp.content:
        return {}
    return resp.json()


def raise_on_partial_errors(response: dict, operation: str) -> None:
    """Surface Bing's PartialErrors array (per-item failures inside HTTP 200)."""
    errors = response.get("PartialErrors") or []
    real = [e for e in errors if e]
    if real:
        details = "; ".join(
            f"[{e.get('Code', '?')}] {e.get('ErrorCode', '')}: {e.get('Message', '')}"
            for e in real
        )
        raise RuntimeError(f"{operation} returned partial errors: {details}")


# ---------------------------------------------------------------------------
# Reporting API — submit → poll → download → parse
# ---------------------------------------------------------------------------


def _date_parts(iso_date: str) -> dict:
    from datetime import date

    d = date.fromisoformat(iso_date)
    return {"Year": d.year, "Month": d.month, "Day": d.day}


def build_report_request(
    report_type: str,
    columns: list[str],
    scope: dict,
    *,
    aggregation: str = "Summary",
    date_range_start: str = "",
    date_range_end: str = "",
    predefined_time: str = "Last30Days",
    report_name: str = "Bingloop pull",
) -> dict:
    """Assemble a Reporting API v13 report request body.

    aggregation="Summary" returns one row per entity aggregated over the whole
    period (TimePeriod must NOT be in columns). Use "Daily" + a TimePeriod
    column for per-day rows.

    Custom dates are INCLUSIVE on both ends; empty start/end falls back to
    predefined_time.
    """
    if date_range_start and date_range_end:
        time_block: dict = {
            "CustomDateRangeStart": _date_parts(date_range_start),
            "CustomDateRangeEnd": _date_parts(date_range_end),
            "ReportTimeZone": REPORT_TIME_ZONE,
        }
    else:
        time_block = {"PredefinedTime": predefined_time, "ReportTimeZone": REPORT_TIME_ZONE}

    return {
        "Type": report_type,
        "ReportName": report_name,
        "Format": "Csv",
        "Aggregation": aggregation,
        "ExcludeColumnHeaders": False,
        "ExcludeReportFooter": True,
        "ExcludeReportHeader": True,
        "ReturnOnlyCompleteData": False,
        "Columns": columns,
        "Scope": scope,
        "Time": time_block,
    }


def get_report(
    config: BingloopConfig,
    report_request: dict,
    *,
    customer_id: str = "",
    account_id: str = "",
    max_polls: int = 24,
    base_delay: float = 5.0,
) -> list[dict]:
    """Run a report end-to-end and return rows as a list of dicts.

    An empty report (no download URL or no rows) returns [] — Bing omits the
    download URL entirely when a report has no data.
    """
    submit = call_service(
        config, REPORTING_BASE, "GenerateReport/Submit",
        {"ReportRequest": report_request},
        customer_id=customer_id, account_id=account_id,
    )
    report_request_id = submit["ReportRequestId"]
    logger.info("Report submitted. ReportRequestId=%s", report_request_id)

    download_url = ""
    for attempt in range(1, max_polls + 1):
        poll = call_service(
            config, REPORTING_BASE, "GenerateReport/Poll",
            {"ReportRequestId": report_request_id},
            customer_id=customer_id, account_id=account_id,
        )
        status_obj = poll["ReportRequestStatus"]
        status = status_obj["Status"]
        logger.info("Poll attempt %d/%d: status=%s", attempt, max_polls, status)

        if status == "Success":
            download_url = status_obj.get("ReportDownloadUrl") or ""
            break
        if status == "Error":
            raise RuntimeError(
                f"Report generation failed server-side for request {report_request_id}."
            )
        # known async job, linear-ish backoff is fine
        time.sleep(min(base_delay * attempt, 30))
    else:
        raise TimeoutError(
            f"Report {report_request_id} did not complete after {max_polls} polls."
        )

    if not download_url:
        return []

    resp = _request_with_retry("GET", download_url)
    resp.raise_for_status()

    rows: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        csv_filename = z.namelist()[0]
        with z.open(csv_filename) as f:
            text = io.TextIOWrapper(f, encoding="utf-8-sig")
            reader = csv.DictReader(text)
            for row in reader:
                rows.append(row)

    logger.info("Downloaded and parsed %d rows.", len(rows))
    return rows


# ---------------------------------------------------------------------------
# CSV value normalization
# ---------------------------------------------------------------------------


def to_float(value: Any) -> float | None:
    """Parse a report CSV cell: strip %/commas (percent columns stay in percent
    units), blank or '--' -> None."""
    if value is None:
        return None
    s = str(value).replace("%", "").replace(",", "").strip()
    if s in ("", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    f = to_float(value)
    return int(f) if f is not None else None


__all__ = [
    "REPORTING_BASE",
    "CAMPAIGN_BASE",
    "CUSTOMER_BASE",
    "ADINSIGHT_BASE",
    "BingAdsAuthError",
    "build_headers",
    "build_report_request",
    "call_service",
    "get_report",
    "raise_on_partial_errors",
    "to_float",
    "to_int",
]
