# Investment MCP Server

Private, authenticated MCP server for your SQL Server investment database.

It exposes read-only tools over:

- `dbo.Series`
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

GRANT SELECT ON dbo.Series TO mcp_investments_user;
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
- `get_research_snapshot`
- `compare_symbols`
- `screen_instruments`
- `get_watched_symbols`
- `get_traded_symbols`
- `get_data_freshness`
- `get_market_indicators`

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

The second migration adds idempotency records, order status history, the
`(UserId, AccountId, ClientOrderId)` uniqueness rule, and soft-deletion fields.

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

Each user can have multiple independently revocable tokens. SQL Server stores
only a SHA-256 digest of each cryptographically random 256-bit token. The MCP
runtime cannot read token hashes or issue tokens; it can only execute
`invest.AuthenticateApiToken`.

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
url = "https://investments-mcp.torusystems.com/mcp"
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
