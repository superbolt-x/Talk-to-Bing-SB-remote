"""Load Bingloop configuration from environment variables for remote deployment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class AdsConfig:
    developer_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    customer_id: str = ""          # manager account CustomerId (CID)
    account_id: str = ""           # default ad account CustomerAccountId (AID)
    refresh_token_seed: str = ""   # one-time seed; rotations persist to token_path
    token_path: str = ""           # durable file (Railway volume) holding the live refresh token
    default_timezone: str = ""     # required on campaign create (e.g. EasternTimeUSCanada)


@dataclass
class SafetyConfig:
    max_daily_budget: float = 50.0
    max_bid_increase_pct: int = 100
    require_dry_run: bool = True
    log_file: str = "/tmp/bingloop-audit.log"
    blocked_operations: list[str] = field(default_factory=list)


@dataclass
class BingloopConfig:
    ads: AdsConfig = field(default_factory=AdsConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)


def load_config(config_path: str | None = None) -> BingloopConfig:
    """Load configuration from environment variables.

    config_path is ignored in remote mode — all settings come from env vars.
    """
    return BingloopConfig(
        ads=AdsConfig(
            developer_token=os.environ.get("BING_ADS_DEVELOPER_TOKEN", ""),
            client_id=os.environ.get("BING_ADS_CLIENT_ID", ""),
            client_secret=os.environ.get("BING_ADS_CLIENT_SECRET", ""),
            customer_id=os.environ.get("BING_ADS_CUSTOMER_ID", ""),
            account_id=os.environ.get("BING_ADS_ACCOUNT_ID", ""),
            refresh_token_seed=os.environ.get("BING_ADS_REFRESH_TOKEN_SEED", ""),
            token_path=os.environ.get(
                "BING_ADS_TOKEN_PATH", "/data/bing_ads_refresh_token.txt"
            ),
            default_timezone=os.environ.get(
                "BING_ADS_DEFAULT_TIMEZONE", "EasternTimeUSCanada"
            ),
        ),
        safety=SafetyConfig(
            max_daily_budget=float(os.environ.get("BINGLOOP_MAX_DAILY_BUDGET", "50.0")),
            max_bid_increase_pct=int(os.environ.get("BINGLOOP_MAX_BID_INCREASE_PCT", "100")),
            require_dry_run=os.environ.get("BINGLOOP_REQUIRE_DRY_RUN", "true").lower() != "false",
            log_file="/tmp/bingloop-audit.log",
        ),
    )
