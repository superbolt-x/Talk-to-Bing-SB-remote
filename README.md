# Talk-to-Bing-SB-remote

An MCP server that connects **Microsoft Advertising (Bing Ads)** to Claude —
the Bing counterpart of `Talk-to-Google-SB-remote`, for everything that repo
does for **Google Ads** (GA4 and the GA4 cross-reference tools are out of
scope). Built on FastMCP, deployed on Railway with the same authless +
shared-secret pattern.

The internal package is `bingloop`.

## Tools

**Read**
- `health_check` — config → OAuth token → accounts probe
- `list_accounts` — every account the authenticated user can access, across **all**
  customers, each tagged with the customer_id (CID) resolved for it
- `split_date_range` — safe non-overlapping week windows (inclusive-range guard)
- `get_campaign_performance`, `get_ad_group_performance`, `get_ad_performance`,
  `get_keyword_performance`, `get_search_terms`, `get_product_performance`,
  `get_asset_group_performance`, `get_asset_performance`,
  `get_extension_performance`
- `get_negative_keywords`
- `list_report_types` + `run_report` — the generic escape hatch (Bing has no
  query language, so this replaces `run_gaql`)

**Write** (all behind the draft → `confirm_and_apply` safety layer)
- `draft_campaign`, `draft_responsive_search_ad`, `draft_keywords`,
  `add_negative_keywords`
- `pause_entity`, `enable_entity`, `remove_entity`
- `confirm_and_apply` (defaults to `dry_run=True`)

**Planning**
- `estimate_budget` — Ad Insight keyword traffic estimates

### Differences from the Google server (API-driven)
- **No `run_gaql`** — Bing has no query language. `run_report` + `list_report_types`
  fill the gap using named report types + explicit column lists.
- **No per-ad asset view.** Google's `ad_group_ad_asset_view` has no Bing
  equivalent; `get_asset_performance` is ad-group + asset grain (no `AdId`), and
  there are no per-asset metrics inside PMax asset groups.
- **Conversions column quirk.** The classic reports expose `ConversionsQualified`
  (plain `Conversions` is deprecated and returns 0); the AssetGroup/Asset reports
  expose only plain `Conversions`, which is live there. The tools pick the right
  column per report automatically.
- **Negative keywords** support EXACT/PHRASE only (no broad-match negatives).
- **Composite entity IDs** on write ops: child entities need the parent ID —
  `pause_entity(entity_type="keyword", entity_id="adGroupId~keywordId")`.

## Configuration (Railway env vars)

| Var | Purpose |
|---|---|
| `BING_ADS_CLIENT_ID` | Azure app (client) ID |
| `BING_ADS_CLIENT_SECRET` | Azure app secret |
| `BING_ADS_DEVELOPER_TOKEN` | Microsoft Advertising developer token |
| `BING_ADS_CUSTOMER_ID` | *(optional)* fallback CID; normally unset — see multi-account note |
| `BING_ADS_ACCOUNT_ID` | *(optional)* default account (AID) when a tool call omits `account_id` |
| `BING_ADS_REFRESH_TOKEN_SEED` | one-time refresh token from the consent flow |
| `BING_ADS_TOKEN_PATH` | volume-backed path for the live token (default `/data/bing_ads_refresh_token.txt`) |
| `BING_ADS_DEFAULT_TIMEZONE` | timezone set on new campaigns (default `EasternTimeUSCanada`) |
| `MCP_AUTH_TOKEN` | shared secret required on every MCP request (Parker-style) |
| `SERVER_URL` | public URL, for DNS-rebinding host allow-listing |
| `BINGLOOP_MAX_DAILY_BUDGET` | budget cap for `draft_campaign` (default 50) |
| `BINGLOOP_REQUIRE_DRY_RUN` | force every apply to dry-run (default `true`) |

### Multi-account / multi-customer access

The server is **multi-tenant**: it targets any ad account the authenticated user
can reach, across every customer — not one hardcoded manager. On first use it
calls `GetUser` + `SearchAccounts` to discover all accessible accounts and build
an **account → customer (AID → CID)** map, cached in-process.

This matters because Bing's `CustomerId` header is not a universal key: each
report/management call must send the CID *through which the user reaches that
account*, or it fails with `106 UserIsNotAuthorized` / `1490 InvalidCustomerId`.
The map (sourced from `GetUser.CustomerRoles`, which also handles accounts linked
into sub-managers) lets every tool resolve the right CID from the `account_id`
you pass. Just call `list_accounts`, then pass `account_id` to any tool.

`BING_ADS_CUSTOMER_ID` / `BING_ADS_ACCOUNT_ID` are therefore optional — leave
them unset for pure multi-account use. Set `BING_ADS_ACCOUNT_ID` only if you want
a default account when a call omits `account_id`; set `BING_ADS_CUSTOMER_ID` only
as a fallback CID for accounts not yet surfaced by discovery.

### ⚠️ Refresh token rotation — read this before deploying

Bing rotates the refresh token **on every use**. Two consequences:

1. **This server needs its OWN token chain.** Do not reuse the refresh token
   the ETL cron scripts rotate — whichever side refreshes first invalidates the
   other. Run the consent flow once more (same Azure app) to mint a dedicated
   token for this server.
2. **The token can't live only in an env var.** `BING_ADS_REFRESH_TOKEN_SEED`
   seeds the *first* boot; after that the live token is persisted to
   `BING_ADS_TOKEN_PATH` on a **Railway volume**. Mount a volume at `/data` (or
   point `BING_ADS_TOKEN_PATH` elsewhere) so rotations survive restarts.

If the token dies (revoked / rotated elsewhere), tools return an auth error with
recovery steps: re-run consent, reset `BING_ADS_REFRESH_TOKEN_SEED`, delete the
stale token file on the volume.

### Minting the seed refresh token (one-time)

1. Browse to the Microsoft consent URL for the Azure app with
   `scope=https://ads.microsoft.com/msads.manage offline_access` and
   `redirect_uri=http://localhost:8080/callback`.
2. Approve; copy the `code` from the redirect.
3. Exchange it for tokens (same flow as `dev/bing_ads_refresh_token.py`) and
   copy the `refresh_token` into `BING_ADS_REFRESH_TOKEN_SEED`.

## Local development

```bash
pip install -e ".[dev]"
pytest                        # unit tests (no network)
MCP_TRANSPORT=stdio python -m bingloop     # stdio, for Claude Desktop/CLI
python -m bingloop                          # HTTP on :8000 (streamable-http)
```

## Deploy (Railway)

`railway.toml` builds the `Dockerfile` and runs `python -m bingloop` with a
`/health` healthcheck. Set the env vars above, mount a volume for the token
path, and connect Claude to `https://<domain>/mcp?access_token=<MCP_AUTH_TOKEN>`.
