# Investment MCP Server

Private MCP server for your SQL Server investment database.

It exposes read-only tools over:

- `dbo.Series`
- `dbo.SeriesData`
- `dbo.TradeGetDaysChangeReturn`

The server does not expose a raw SQL tool. All database access is parameterized and scoped to your investment tables/procedure.

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
GRANT EXECUTE ON dbo.TradeGetDaysChangeReturn TO mcp_investments_user;
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

The research tools call `dbo.TradeGetDaysChangeReturn` by default. If that procedure returns a large result set, create the optional filtered wrapper in `sql/create_mcp_research_sp.sql` and set:

```text
MCP_RESEARCH_PROCEDURE=dbo.McpGetResearch
MCP_RESEARCH_PROCEDURE_HAS_FILTERS=true
```

## Important

This server provides research data for analysis. It should not be connected to brokerage trading permissions, account numbers, or order-entry functions.
