#!/usr/bin/env python3
"""Mint the one-time seed refresh token for the Talk-to-Bing MCP server.

Bing rotates the refresh token on every use, so the MCP server must run on its
OWN token chain — do NOT reuse the token the ETL cron scripts rotate. Run a
FRESH browser consent (see --print-url), then exchange the returned code here.

The refresh token is printed to stdout for you to paste into Railway as
BING_ADS_REFRESH_TOKEN_SEED. It is NOT written to disk (the live token is
persisted by the server itself, on its Railway volume).

Usage:
    # 1. print the consent URL, open it, approve, copy the ?code= from the redirect
    python scripts/mint_seed_token.py --print-url

    # 2. exchange the code for a refresh token
    python scripts/mint_seed_token.py --code "M.C5..."

Credentials default to the BING_ADS_CLIENT_ID / BING_ADS_CLIENT_SECRET env vars;
override with --client-id / --client-secret.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlencode

import requests

AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
SCOPE = "https://ads.microsoft.com/msads.manage offline_access"
DEFAULT_REDIRECT = "http://localhost:8080/callback"


def build_consent_url(client_id: str, redirect_uri: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "response_mode": "query",
        "prompt": "consent",  # fresh consent -> independent refresh-token chain
        "state": "bingloop-mcp",
    })


def exchange(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
    }, timeout=30)
    if resp.status_code != 200:
        print(f"Token exchange failed: {resp.status_code}\n{resp.text}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", default=os.environ.get("BING_ADS_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("BING_ADS_CLIENT_SECRET", ""))
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT)
    parser.add_argument("--print-url", action="store_true",
                        help="Print the consent URL and exit")
    parser.add_argument("--code", help="Authorization code from the redirect URL")
    args = parser.parse_args()

    if not args.client_id:
        print("Missing client id — set BING_ADS_CLIENT_ID or pass --client-id.", file=sys.stderr)
        return 1

    if args.print_url:
        print(build_consent_url(args.client_id, args.redirect_uri))
        print("\nOpen this URL, sign in with the account that manages the Bing Ads "
              "manager (CID), approve, then copy the `code` value from the "
              "localhost redirect URL (the page itself will fail to load — that's "
              "expected).", file=sys.stderr)
        return 0

    if not args.code:
        print("Provide --code (or --print-url first).", file=sys.stderr)
        return 1
    if not args.client_secret:
        print("Missing client secret — set BING_ADS_CLIENT_SECRET or pass "
              "--client-secret.", file=sys.stderr)
        return 1

    tokens = exchange(args.client_id, args.client_secret, args.code, args.redirect_uri)
    print("\n=== Seed refresh token (set as BING_ADS_REFRESH_TOKEN_SEED in Railway) ===\n")
    print(tokens["refresh_token"])
    print(f"\n(access token valid ~{tokens.get('expires_in', '?')}s; not needed — the "
          "server refreshes on its own)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
