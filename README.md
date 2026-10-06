# Investment MCP Server

Private, authenticated MCP server for your SQL Server investment database.

It exposes read-only tools over:

- `invest.McpInstruments`
- `dbo.SeriesData`
- `dbo.InvestmentMcpGetResearch`

The server does not expose a raw SQL tool. All database access is parameterized.
Private portfolio tools derive the caller from the bearer token and scope every
account query to that authenticated user.

## Install

```powershell
cd "C:\Users\asmir\source\repos\investment_mcp"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with your SQL Server name and database name.

## SQL Permissions

Use a read-only SQL login/user if possible:

```sql
CREATE LOGIN mcp_investments_login WITH PASSWORD = 'replace-with-strong-password';
CREATE USER mcp_investments_user FOR LOGIN mcp_investments_login;

GRANT SELECT ON invest.McpInstruments TO mcp_investments_user;
DENY SELECT ON dbo.Series TO mcp_investments_user;
GRANT SELECT ON dbo.SeriesData TO mcp_investments_user;
GRANT EXECUTE ON dbo.InvestmentMcpGetResearch TO mcp_investments_user;
```

If you use Windows authentication instead, grant the same permissions to your Windows user.

## Run Locally

```powershell
.\.venv\Scripts\Activate.ps1
python server.py
```

With `MCP_TRANSPORT=stdio`, the command starts silently and waits for an MCP client. A blank terminal is expected. Press `Ctrl+C` to stop it.

For MCP Inspector:

```powershell
mcp dev server.py
```

## Codex MCP Config Example

Add something like this to your Codex MCP config, adjusting paths and connection string:

```toml
[mcp_servers.investments]
command = "C:\\Users\\asmir\\source\\repos\\investment_mcp\\.venv\\Scripts\\python.exe"
args = ["C:\\Users\\asmir\\source\\repos\\investment_mcp\\server.py"]
env = { SQLSERVER_CONN = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost;DATABASE=Investments;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;" }
```

## Tools

- `search_symbols`
- `get_symbol_profile`
- `get_latest_prices`
- `get_price_history`
- `get_market_hours`
- `get_market_movers`
- `get_research_snapshot`
- `compare_symbols`
- `screen_instruments`
- `get_watched_symbols`
- `get_traded_symbols`
- `get_data_freshness`
- `get_market_indicators`

Global scoring and candidate-review tools:

- `get_reference_universe`
- `get_fund_holdings`
- `get_scoring_model`
- `score_instrument`
- `rank_candidates`
- `get_score_history`

Caller-scoped portfolio tools:

- `create_account`
- `get_my_accounts`
- `get_my_portfolio`
- `import_opening_positions`
- `get_my_open_orders`
- `record_trade_execution`
- `create_limit_order_record`
- `update_limit_order_record`
- `cancel_limit_order_record`
- `update_cash_balance`
- `get_my_strategy`
- `update_my_strategy`

Explicit sharing tools:

- `share_portfolio`
- `revoke_portfolio_access`
- `list_portfolio_access`
- `get_shared_portfolios`
- `get_shared_portfolio`

`get_shared_portfolios` lists accounts shared with the caller.
`get_shared_portfolio(account_id)` returns cash balances and transaction-derived
positions only when the caller has an active, unexpired `VIEW` grant. It returns
the same `Account not found` error for missing and inaccessible accounts and does
not grant access to order or write operations.

Write tools require UUID idempotency keys. Concurrent updates use SQL Server
`rowversion` values returned as hexadecimal strings.

Limit-order create and update tools accept optional `duration` and `expires_on`
fields. `duration` accepts `DAY`, `GTC`, `GTD`, `IOC`, or `FOK` (including common
long-form aliases), and `expires_on` uses `YYYY-MM-DD`. For example, an order
good through October 2, 2026 uses `duration="GTD"` and
`expires_on="2026-10-02"`.

## Private schema migrations

Run these in order against the investment database:

1. `sql/001_create_invest_schema.sql`
2. `sql/002_add_private_tool_safety.sql`
3. `sql/003_grant_mcp_connector_runtime.sql`
4. `sql/004_add_database_api_tokens.sql`
5. `sql/005_add_order_duration_expiration.sql`
6. `sql/006_add_account_and_opening_position_writes.sql`
7. `sql/007_create_investment_mcp_research_sp.sql`
8. `sql/008_add_schwab_oauth_runtime.sql`
9. `sql/009_create_mcp_instruments_view.sql`
10. `sql/010_add_api_token_usage_log.sql`
11. `sql/011_limit_active_api_tokens.sql`
12. `sql/012_add_portal_integration.sql`
13. `sql/013_add_reference_instrument_classification.sql`
14. `sql/014_add_investment_scoring.sql`
15. `sql/015_grant_scoring_runtime.sql`
16. `sql/016_add_holdings_overlap_and_scoring_v1_1.sql`
18. `sql/018_reject_duplicate_active_token_names.sql`
19. `sql/019_add_portal_cash_valuation.sql`

The second migration adds idempotency records, order status history, the
`(UserId, AccountId, ClientOrderId)` uniqueness rule, and soft-deletion fields.

Migration `009` creates `invest.McpInstruments`, a least-privilege view over the
instrument fields used by the MCP. It includes active rows only; excludes the
internal `TEMP` and `PORTF` calculation series; and omits status, watch/trade,
fallback-price, alternate-symbol, and data-range fields. It grants
`mcp_connector` access to the view and denies direct reads from `dbo.Series`.
Watched/traded tools use the filtered research procedure instead of the view.

Migration `012` adds the least-privilege WiseLinePortal contract. It provisions
portal identities idempotently, synchronizes paid/trial entitlement boundaries,
exposes owned portfolio summaries and positions, and wraps MCP token management.
The portal runtime receives procedure execution only; it does not receive direct
table access. Existing non-portal Investment MCP users remain independent of the
portal entitlement table.

Migration `013` keeps `dbo.Series` as the one global curated reference
universe. It adds scoring classifications, backfills active instruments, and
installs a set-based `AFTER INSERT, UPDATE` trigger. The trigger uses local SQL
rules only, preserves manual classifications, and marks uncertain instruments
for review. The existing symbol-management application does not need to call a
new procedure.

Review an automatic classification and, when necessary, replace it without
changing the symbol-management application:

```sql
EXEC invest.SetReferenceInstrumentClassification
    @Symbol = 'VBR',
    @CandidateClass = 'ETF',
    @Archetype = 'SmallMidFactorEquity',
    @InclusionReason = N'Small-value portfolio role';
```

Migration `014` adds immutable model definitions, shared feature/score
snapshots, and caller/account-scoped portfolio-fit snapshots. Migration `015`
grants the runtime access to the safe scoring view and snapshot procedures but
denies direct scoring-table writes.

Migration `016` corrects value-fund classification (`VTV` becomes
`LargeValueEquity` while `VBR` and `AVUV` remain `SmallMidFactorEquity`), adds
version `1.1`, and creates an auditable global fund-holdings snapshot store. The
MCP runtime can read the latest holdings view but cannot replace holdings.

## Transparent investment scoring

`invest.McpScoringReferenceInstruments` is the safe global scoring view over
active `dbo.Series` instruments. `TEMP` and `PORTF` remain excluded. Instruments
with `NeedsReview=1` remain visible for review but are not used as scoring peers.

The scoring engine is implemented in `scoring/` and uses no additional Python
packages. It calculates class/archetype-relative quality, valuation, growth,
trend, risk, liquidity, and nearest-peer similarity. Missing properties receive
a disclosed neutral contribution rather than zero; `DataCompletenessScore` and
`MissingFeatures` make the limitation visible.

Without an account, `score_instrument` returns `StandaloneCandidateScore`.
When a caller-owned `account_id` is supplied, it also returns a portfolio-fit
score and the configured composite score. Model `1.1` combines classification
concentration with constituent-level fund overlap and return correlation against
the owned portfolio. It reports data coverage, observation counts, explicit
penalties, and source/as-of metadata. If an ETF or mutual fund has no holdings
snapshot, portfolio fit is capped at `60`; fewer than 60 overlapping return
observations cap it at `75`. Missing data therefore cannot produce a misleading
perfect fit score. Holdings older than 45 days cap portfolio fit at `65`.
When required portfolio-fit data is missing or stale, the composite score is
also capped at `64`, below the positive-review threshold.

Configure the service with:

```text
MCP_SCORING_MODEL_VERSION=1.1
MCP_SCORING_BENCHMARK=VOO
MCP_SCORING_MIN_PEER_COUNT=5
MCP_SCORING_MAX_CANDIDATES=10
MCP_SCORING_DEFAULT_TARGET_WEIGHT_PERCENT=2
MCP_SCORING_PERSIST_SNAPSHOTS=true
```

Schwab quote/fundamental responses used by this project do not include complete
fund constituents. Load normalized issuer or licensed-provider exports through
the dedicated data-loader procedure. Add an administrative database user to the
role once:

```sql
ALTER ROLE investment_data_loader ADD MEMBER [investment_holdings_loader];
```

Create a CSV with these columns (one row per constituent):

```text
FundSymbol,AsOfDate,SourceName,SourceUrl,HoldingKey,HoldingSymbol,HoldingName,WeightPercent,ReportedCoveragePercent,IsComplete
```

`HoldingKey` may be omitted when `HoldingSymbol` is present; the importer creates
`TICKER:<symbol>`. Use `CUSIP:<cusip>` when a provider does not supply a ticker.
Set a separate loader connection and import the provider file:

```powershell
$env:HOLDINGS_SQLSERVER_CONN = 'DRIVER={ODBC Driver 17 for SQL Server};SERVER=...;DATABASE=Trade;UID=investment_holdings_loader;PWD=...;Encrypt=yes;TrustServerCertificate=yes;'
.\.venv\Scripts\python.exe .\scripts\import_fund_holdings.py .\data\fund-holdings.csv
```

Do not add `mcp_connector` to `investment_data_loader`. Use
`get_fund_holdings(symbol)` to audit exactly which latest snapshot influences a
score.

After the daily price-data load, refresh shared scores from the server project
directory with:

```powershell
$env:MCP_SCORING_REFRESH_AUTH_SUBJECT = 'local:andreySr'
.\.venv\Scripts\python.exe .\scripts\refresh_scoring.py
```

This can later run as a Windows Scheduled Task. Deploy `server.py`, the complete
`scoring` directory, `scripts/refresh_scoring.py`, and
`scripts/import_fund_holdings.py`; copying only `server.py` is no longer
sufficient.

## Schwab OAuth access-token refresh

Migration `008` creates two least-privilege procedures over the existing
`dbo.TDAconfig` table and grants the MCP runtime account permission to execute
only those procedures:

- `invest.GetSchwabOAuthConfig`
- `invest.UpdateSchwabAccessToken`

Configure the row used by the service:

```text
SCHWAB_TDACONFIG_ID=1
SCHWAB_TOKEN_URL=https://api.schwabapi.com/v1/oauth/token
SCHWAB_ACCESS_TOKEN_TTL_SECONDS=1800
SCHWAB_ACCESS_TOKEN_REFRESH_BUFFER_SECONDS=300
SCHWAB_HTTP_TIMEOUT_SECONDS=20
```

Before each Schwab market-data request, `_get_schwab_access_token` reads the
stored token timestamp. It reuses a token younger than 25 minutes and otherwise
uses the refresh token to obtain a new access token, saves it with a UTC update
timestamp, and returns it to the request. Concurrent requests in the Windows
service are protected by an in-process lock. A market-data request that still
receives HTTP 401 forces one refresh and one retry.

Schwab market data extends the investment tools without exposing brokerage
trading operations:

- `search_symbols` searches SQL first, then Schwab instrument lookup.
- `get_symbol_profile` merges Schwab fundamentals with local metadata.
- `get_latest_prices` uses current Schwab quotes, with SQL fallback.
- `get_price_history` uses SQL history first and Schwab for unknown symbols.
- `get_market_hours` supports equity, bond, futures, and forex markets.
- `get_market_movers` supports equity markets and major indexes.

Responses are normalized before being returned. In-memory caching uses 15
seconds for quotes, 60 seconds for movers, five minutes for market hours, 15
minutes for external price history, and one hour for instrument data. The cache
reduces duplicate upstream requests across MCP users and never stores Schwab
access or refresh tokens.

## Rate limits and Schwab units

Limits aggregate by the authenticated database `UserId`, not by bearer token,
so issuing multiple tokens does not multiply a user's allowance. The defaults
are:

- all MCP tools: 30 calls/minute/token with a burst of 10;
- all MCP tools: 60 calls/minute/user with a burst of 15;
- authenticated POST requests: at most 2 concurrent requests/token and 4/user;
- Schwab cache misses: 6 units/minute/user with a burst of 3 requests;
- Schwab daily allowance: 100 units/user/UTC day;
- external history: 2 calls/minute/user and 20 calls/user/UTC day;
- quotes: at most 200 unique symbols per tool call;
- shared Schwab capacity: 60 upstream requests/minute, burst 10, and at most 5
  concurrent requests.

SQL-only work and Schwab cache hits cost zero Schwab units. Instrument search,
profiles, market hours, and movers cost 1 unit. Quotes cost 1 unit for 1-50
symbols, 2 for 51-100, 3 for 101-150, and 4 for 151-200. External price history
costs 5 units. A Schwab HTTP 401 retry consumes another global upstream request
but does not charge the user twice for the same logical cache miss.

When a limit is reached, the tool returns a structured error containing
`error=rate_limit_exceeded`, a safe human-readable `message`, the limiting
scope, and `retry_after_seconds`.
Because MCP tool failures are JSON-RPC results, this retry value is carried in
the tool error rather than an HTTP `Retry-After` response header.

The defaults can be adjusted with:

```text
MCP_USER_RATE_PER_MINUTE=60
MCP_USER_BURST=15
MCP_TOKEN_RATE_PER_MINUTE=30
MCP_TOKEN_BURST=10
MCP_TOKEN_MAX_CONCURRENT_REQUESTS=2
MCP_USER_MAX_CONCURRENT_REQUESTS=4
SCHWAB_USER_UNITS_PER_MINUTE=6
SCHWAB_USER_REQUEST_BURST=3
SCHWAB_USER_DAILY_UNITS=100
SCHWAB_HISTORY_CALLS_PER_MINUTE=2
SCHWAB_HISTORY_BURST=2
SCHWAB_HISTORY_DAILY_CALLS=20
SCHWAB_GLOBAL_REQUESTS_PER_MINUTE=60
SCHWAB_GLOBAL_BURST=10
SCHWAB_MAX_CONCURRENT_REQUESTS=5
SCHWAB_MAX_QUOTE_SYMBOLS=200
```

These counters are in process, which is appropriate for the current single
NSSM Windows service. They reset when `InvestmentMcp` restarts. Before running
multiple MCP processes or servers, move the counters to Redis or another shared
atomic store. Set the global request allowance no higher than the confirmed
quota for the Schwab developer application; 60/minute is a conservative
provisional value, not a statement of Schwab entitlement.

## API token usage telemetry

Migration `010` creates `invest.ApiTokenUsageLog`, the insert-only
`invest.RecordApiTokenUsage` runtime procedure, and the aggregate
`invest.ApiTokenUsageDaily` reporting view. The runtime login can execute the
insert procedure but cannot read, insert, update, or delete the table directly.

Each authenticated database-token POST records:

- `UserId` and `ApiTokenId`;
- request start/end, duration, HTTP status, outcome, and response size;
- MCP RPC method and tool name, but not tool arguments;
- Schwab units, actual upstream requests, and cache hits;
- hostname, Cloudflare Ray ID, country, IP/network according to privacy mode,
  user agent, and hashed MCP session/client fingerprint values;
- rate-limit scope and a safe exception type when applicable.

Bearer tokens, authorization headers, request arguments, response bodies,
portfolio data, and Schwab credentials are never written to the usage table.
Legacy environment tokens are not persisted because they have no database
`ApiTokenId`; database-backed tokens are required for usage reports.

Configure telemetry with:

```text
MCP_TOKEN_USAGE_LOG_ENABLED=true
MCP_TOKEN_USAGE_IP_MODE=prefix
MCP_TOKEN_USAGE_MAX_CAPTURE_BYTES=131072
```

`prefix` is the recommended IP mode and stores IPv4 `/24` or IPv6 `/64`
networks. `full` additionally stores the exact client IP; use it only with a
documented retention and privacy policy. `none` stores neither IP nor network.
The service trusts `CF-Connecting-IP` because its listener is private behind the
Cloudflare Tunnel; do not trust that header if the origin later becomes directly
Internet-accessible. Establish a retention job before public launch (for
example, retain detailed rows for 90 days and retain daily aggregates longer).

Option-chain and brokerage order-submission endpoints are intentionally not
implemented. Existing order tools only record caller-owned portfolio state in
SQL Server and never send an order to Schwab.

`dbo.TDAconfig.URLtoGetCode` is preserved as the legacy interactive
authorization URL used to obtain the initial code and refresh token. The MCP
runtime never uses that value for background refreshes. It uses only
`SCHWAB_TOKEN_URL` from server configuration and validates that it is Schwab's
HTTPS token endpoint before transmitting credentials. The refresh token is not
automatically rotated; continue the separate refresh-token renewal process
required for the Schwab application.

The migration does not grant `mcp_connector` direct access to `dbo.TDAconfig`.
Because that legacy table contains plaintext OAuth secrets, restrict database
administrator access, encrypted backups, and SQL diagnostic logging accordingly.

## Account setup and opening positions

`create_account` creates an account owned by the authenticated caller. The tool
does not accept a user ID, so callers cannot create accounts for other users.

`import_opening_positions` establishes current holdings without reconstructing
historical trades. Each input contains a symbol, quantity, and total cost basis:

```json
{
  "account_id": "00000000-0000-0000-0000-000000000000",
  "positions": [
    {
      "symbol": "VOO",
      "quantity": 149.191,
      "total_cost_basis": 92107.44
    }
  ],
  "as_of": "2026-08-10T15:00:00-04:00",
  "idempotency_key": "00000000-0000-4000-8000-000000000001"
}
```

The import writes `OPENING_POSITION` rows to `invest.Transactions`, derives and
stores unit cost in `Price`, and stores total cost basis in `GrossAmount`. It
never inserts or updates `invest.CashBalances`. A unique database index prevents
more than one active opening position for the same user, account, and symbol.

## Database-backed bearer identity

Each user can have at most two active, independently revocable tokens. Expired
and revoked tokens do not count toward the limit. SQL Server stores
only a SHA-256 digest of each cryptographically random 256-bit token. The MCP
runtime cannot read token hashes or issue tokens; it can only execute
`invest.AuthenticateApiToken`.

After migration `004`, run `sql/011_limit_active_api_tokens.sql` to enforce the
two-active-token maximum in `invest.IssueApiToken`.

Issue a token from an administrator connection in SSMS:

```sql
EXEC invest.IssueApiToken
    @AuthenticationSubject = N'local:andreySr',
    @TokenName = N'Andrey Codex desktop',
    @ExpiresAt = '2027-08-10T00:00:00-04:00';
```

Copy `PlaintextToken` from the result immediately. It is returned only once and
must be delivered to the user through a secure channel. Configure the hosted
service with:

```text
MCP_TOKEN_AUTH_MODE=database
```

The user's Codex configuration continues to reference an environment variable:

```toml
[mcp_servers.investments]
url = "https://mcp.wiselinetrade.com/mcp"
bearer_token_env_var = "INVESTMENTS_MCP_TOKEN"
startup_timeout_sec = 30
tool_timeout_sec = 120
```

Set `INVESTMENTS_MCP_TOKEN` to the issued plaintext token on that user's
computer. Never store plaintext tokens in SQL Server, GitHub, logs, or support
messages.

List or revoke tokens from an administrator connection:

```sql
EXEC invest.ListApiTokens
    @AuthenticationSubject = N'local:andreySr';

EXEC invest.RevokeApiToken
    @AuthenticationSubject = N'local:andreySr',
    @ApiTokenId = '00000000-0000-0000-0000-000000000000';
```

### Safe migration from environment tokens

1. Run migration `004` and issue a new database token.
2. Set `MCP_TOKEN_AUTH_MODE=hybrid`, retaining the old token variables.
3. Deploy and restart the service. Both old and database tokens work.
4. Move every client to its new database token and verify caller isolation.
5. Set `MCP_TOKEN_AUTH_MODE=database`, delete `MCP_BEARER_TOKEN`,
   `MCP_DEFAULT_AUTH_SUBJECT`, and `MCP_TOKEN_SUBJECTS_JSON`, then restart.

A future website should authenticate the human user, enforce subscription or
entitlement rules separately from the token table, and call the issue/list/revoke
procedures through a separate least-privilege database principal. The MCP runtime
database principal must never receive token-administration permissions. For a
public self-service integration, plan to add MCP-standard OAuth 2.1 rather than
making permanent API keys the only login method.

The research tools call the filtered, MCP-specific procedure by default:

```text
MCP_RESEARCH_PROCEDURE=dbo.InvestmentMcpGetResearch
MCP_RESEARCH_PROCEDURE_HAS_FILTERS=true
```

`IsWatched` and `IsTraded` are accepted as procedure filters but are not exposed
in its result set. The procedure also omits `TradePrice`, `%Chng`, `StatusId`,
`KeepMonths`, and `Rank`; the underlying score expression is used only in the
`ORDER BY` clause to preserve result order.
To remove additional research properties, edit only the final `SELECT` list in
`sql/007_create_investment_mcp_research_sp.sql`, rerun the migration, and verify
that no MCP screening tool depends on the removed field.

## Important

The order tools record portfolio state only. They do not submit orders to a
brokerage. Codex approval prompts are supplemental protection; authorization is
always enforced by this server.
