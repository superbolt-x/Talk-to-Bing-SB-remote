"""Microsoft Advertising OAuth — rotating refresh token management.

Bing Ads rotates the refresh token ON EVERY USE: exchanging a refresh token for
an access token invalidates the old refresh token and returns a new one. Losing
the new one means re-running the browser consent flow. Consequences for a
remote (Railway) deployment:

* The live refresh token CANNOT live in an env var — env vars are static, and
  the token changes on the first refresh. It is persisted to a file on a
  mounted volume (``BING_ADS_TOKEN_PATH``, default ``/data/...``).
* On first boot the file does not exist yet; it is seeded from the
  ``BING_ADS_REFRESH_TOKEN_SEED`` env var (minted via the one-time consent
  flow — see README). After that, the file is the single source of truth and
  the seed var is ignored.
* This server must use its OWN token chain — do not reuse the refresh token
  that the ETL cron scripts rotate, or each side will invalidate the other.

Access tokens (~1h lifetime) are cached in-process with a safety margin, so
the token endpoint is hit roughly once per hour, not per tool call.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bingloop.config import BingloopConfig

logger = logging.getLogger("bingloop.auth")

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
SCOPE = "https://ads.microsoft.com/msads.manage offline_access"


class ConfigError(RuntimeError):
    pass


class BingAdsAuthError(RuntimeError):
    """Raised when auth fails in a way that requires re-running the browser consent flow."""


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: float  # unix timestamp


_cached_token: TokenBundle | None = None
_lock = threading.Lock()


def _load_refresh_token(config: BingloopConfig) -> str:
    """Read the live refresh token: volume file first, env seed as first-boot fallback."""
    path = Path(config.ads.token_path)
    try:
        token = path.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass

    seed = config.ads.refresh_token_seed.strip()
    if seed:
        logger.info("Token file %s not found — seeding from BING_ADS_REFRESH_TOKEN_SEED", path)
        return seed

    raise ConfigError(
        f"No refresh token available: {path} does not exist and "
        "BING_ADS_REFRESH_TOKEN_SEED is not set. Run the one-time OAuth "
        "consent flow (see README) and set the seed env var."
    )


def _save_refresh_token(config: BingloopConfig, token: str) -> None:
    """Persist the rotated refresh token durably — write-then-rename so a crash
    mid-write never leaves a truncated token file."""
    path = Path(config.ads.token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(token)
    os.replace(tmp, path)


def _refresh_access_token(config: BingloopConfig, refresh_token: str) -> TokenBundle:
    """Exchange a refresh token for a new access token + rotated refresh token.

    Raises BingAdsAuthError on invalid_grant (requires re-running browser consent).
    """
    import requests

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": config.ads.client_id,
            "client_secret": config.ads.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": SCOPE,
        },
        timeout=30,
    )

    if resp.status_code == 400:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if body.get("error") == "invalid_grant":
            raise BingAdsAuthError(
                "Refresh token is no longer valid (revoked, expired, or rotated "
                "elsewhere). Re-run the browser consent flow, set the new token "
                "as BING_ADS_REFRESH_TOKEN_SEED, and delete the stale token file "
                f"at {config.ads.token_path}."
            )
    resp.raise_for_status()

    payload = resp.json()
    bundle = TokenBundle(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=time.time() + payload["expires_in"] - 60,  # 60s safety margin
    )
    # Persist BEFORE returning — if the new refresh token is lost, the chain is dead.
    _save_refresh_token(config, bundle.refresh_token)
    logger.info("Access token refreshed. Valid for ~%d seconds.", payload["expires_in"])
    return bundle


def get_access_token(config: BingloopConfig, force_refresh: bool = False) -> str:
    """Return a valid access token, refreshing only when needed (cached in-process).

    Thread-safe: FastMCP may serve concurrent tool calls, and a double refresh
    would rotate the refresh token twice, orphaning one chain.
    """
    global _cached_token

    with _lock:
        if not force_refresh and _cached_token and time.time() < _cached_token.expires_at:
            return _cached_token.access_token

        current = _cached_token.refresh_token if _cached_token else _load_refresh_token(config)
        _cached_token = _refresh_access_token(config, current)
        return _cached_token.access_token


def check_config(config: BingloopConfig) -> None:
    """Raise ConfigError listing any missing required credentials."""
    missing = []
    if not config.ads.client_id:
        missing.append("BING_ADS_CLIENT_ID")
    if not config.ads.client_secret:
        missing.append("BING_ADS_CLIENT_SECRET")
    if not config.ads.developer_token:
        missing.append("BING_ADS_DEVELOPER_TOKEN")
    if not config.ads.customer_id:
        missing.append("BING_ADS_CUSTOMER_ID")
    if missing:
        raise ConfigError(
            f"Missing required environment variables: {', '.join(missing)}."
        )
