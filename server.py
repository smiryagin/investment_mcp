from __future__ import annotations

import os
import re
import secrets
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyodbc
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import uvicorn


def _load_local_env() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


_load_local_env()

mcp = FastMCP(
    "Investment SQL Server",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:8000",
            "127.0.0.1:*",
            "localhost:8000",
            "localhost:*",
            "investments-mcp.torusystems.com",
        ],
        allowed_origins=[
            "https://investments-mcp.torusystems.com",
        ],
    ),
)

SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,50}$")
PROC_RE = re.compile(r"^[A-Za-z0-9_\[\].]+$")

MAX_ROWS = int(os.getenv("MCP_MAX_ROWS", "2500"))
SQL_TIMEOUT_SECONDS = int(os.getenv("MCP_SQL_TIMEOUT_SECONDS", "30"))
RESEARCH_PROCEDURE = os.getenv(
    "MCP_RESEARCH_PROCEDURE",
    "dbo.TradeGetDaysChangeReturn",
)
RESEARCH_PROCEDURE_HAS_FILTERS = os.getenv(
    "MCP_RESEARCH_PROCEDURE_HAS_FILTERS",
    "false",
).lower() in {"1", "true", "yes", "on"}


class BearerAuthASGI:
    def __init__(self, app: Any, token: str, protected_path: str) -> None:
        self.app = app
        self.token = token
        self.protected_path = protected_path.rstrip("/") or "/"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self._is_protected_path(scope):
            authorization = self._header(scope, "authorization")
            expected = f"Bearer {self.token}"
            if not secrets.compare_digest(authorization, expected):
                await self._unauthorized(send)
                return

        await self.app(scope, receive, send)

    def _is_protected_path(self, scope: dict[str, Any]) -> bool:
        path = scope.get("path", "")
        return path == self.protected_path or path.startswith(f"{self.protected_path}/")

    def _header(self, scope: dict[str, Any], name: str) -> str:
        wanted = name.lower().encode("latin1")
        for key, value in scope.get("headers", []):
            if key.lower() == wanted:
                return value.decode("latin1")
        return ""

    async def _unauthorized(self, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"www-authenticate", b'Bearer realm="investment-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"Unauthorized"})


def _run_streamable_http() -> None:
    token = os.getenv("MCP_BEARER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MCP_BEARER_TOKEN must be set for streamable-http mode.")

    host = os.getenv("FASTMCP_HOST", "127.0.0.1")
    port = int(os.getenv("FASTMCP_PORT", "8000"))
    protected_path = os.getenv("FASTMCP_STREAMABLE_HTTP_PATH", "/mcp")
    app = BearerAuthASGI(mcp.streamable_http_app(), token, protected_path)
    uvicorn.run(app, host=host, port=port)


def _connection_string() -> str:
    explicit = os.getenv("SQLSERVER_CONN")
    if explicit:
        return explicit

    driver = os.getenv("SQLSERVER_DRIVER", "ODBC Driver 18 for SQL Server")
    server = os.environ["SQLSERVER_SERVER"]
    database = os.environ["SQLSERVER_DATABASE"]
    encrypt = os.getenv("SQLSERVER_ENCRYPT", "yes")
    trust_cert = os.getenv("SQLSERVER_TRUST_SERVER_CERTIFICATE", "yes")

    trusted = os.getenv("SQLSERVER_TRUSTED_CONNECTION", "yes").lower()
    if trusted in {"1", "true", "yes", "on"}:
        auth = "Trusted_Connection=yes;"
    else:
        user = os.environ["SQLSERVER_USER"]
        password = os.environ["SQLSERVER_PASSWORD"]
        auth = f"UID={user};PWD={password};"

    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"Encrypt={encrypt};"
        f"TrustServerCertificate={trust_cert};"
        f"{auth}"
    )


def _connect() -> pyodbc.Connection:
    return pyodbc.connect(
        _connection_string(),
        timeout=SQL_TIMEOUT_SECONDS,
        autocommit=True,
    )


def _serialize(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _rows_from_cursor(cursor: pyodbc.Cursor) -> list[dict[str, Any]]:
    while cursor.description is None:
        if not cursor.nextset():
            return []

    columns = [column[0] for column in cursor.description]
    return [
        {columns[index]: _serialize(value) for index, value in enumerate(row)}
        for row in cursor.fetchall()
    ]


def _fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with closing(_connect()) as connection:
        with closing(connection.cursor()) as cursor:
            if hasattr(cursor, "set_timeout"):
                cursor.set_timeout(SQL_TIMEOUT_SECONDS)
            cursor.execute(sql, params)
            return _rows_from_cursor(cursor)


def _clamp_limit(limit: int | None, default: int = 100) -> int:
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_ROWS))


def _clean_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not SYMBOL_RE.match(cleaned):
        raise ValueError(f"Invalid symbol: {symbol!r}")
    return cleaned


def _clean_symbols(symbols: list[str]) -> list[str]:
    cleaned: list[str] = []
    for symbol in symbols:
        normalized = _clean_symbol(symbol)
        if normalized not in cleaned:
            cleaned.append(normalized)
    if not cleaned:
        raise ValueError("At least one symbol is required.")
    return cleaned


def _parse_date(value: str | None, name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date like 2026-07-02.") from exc


def _proc_name() -> str:
    if not PROC_RE.match(RESEARCH_PROCEDURE):
        raise ValueError("MCP_RESEARCH_PROCEDURE contains unsupported characters.")
    return RESEARCH_PROCEDURE


def _truthy(value: Any) -> bool:
    return value is True or value == 1 or str(value).lower() in {"true", "1", "yes"}


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _passes_max(value: Any, maximum: float) -> bool:
    numeric = _number(value)
    return numeric is not None and numeric <= maximum


def _passes_min(value: Any, minimum: float) -> bool:
    numeric = _number(value)
    return numeric is not None and numeric >= minimum


def _get(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _load_research_rows(
    symbols: list[str] | None = None,
    asset_type: str | None = None,
    watched_only: bool = False,
    traded_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = _clamp_limit(limit)
    proc = _proc_name()

    if RESEARCH_PROCEDURE_HAS_FILTERS:
        rows = []
        target_symbols = symbols or [None]
        for symbol in target_symbols:
            rows.extend(
                _fetch_all(
                    (
                        f"EXEC {proc} "
                        "@Symbol = ?, @AssetType = ?, @WatchedOnly = ?, "
                        "@TradedOnly = ?, @Top = ?"
                    ),
                    (symbol, asset_type, int(watched_only), int(traded_only), limit),
                )
            )
    else:
        rows = _fetch_all(f"EXEC {proc}")

    if symbols:
        wanted = set(symbols)
        rows = [row for row in rows if str(row.get("Symbol", "")).upper() in wanted]
    if asset_type:
        rows = [
            row
            for row in rows
            if str(row.get("AssetType", "")).lower() == asset_type.lower()
        ]
    if watched_only:
        rows = [row for row in rows if _truthy(row.get("IsWatched"))]
    if traded_only:
        rows = [row for row in rows if _truthy(row.get("IsTraded"))]

    return rows[:limit]


@mcp.tool()
def search_symbols(
    query: str = "",
    asset_type: str | None = None,
    active_only: bool = True,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search instruments in dbo.Series by symbol, internal symbol, or name."""
    limit = _clamp_limit(limit, default=25)
    filters = []
    params: list[Any] = []

    if query.strip():
        pattern = f"%{query.strip()}%"
        filters.append("(Symbol LIKE ? OR ISymbol LIKE ? OR Name LIKE ?)")
        params.extend([pattern, pattern, pattern])
    if asset_type:
        filters.append("AssetType = ?")
        params.append(asset_type)
    if active_only:
        filters.append("Active = 1")

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    return _fetch_all(
        f"""
        SELECT TOP ({limit})
            SeriesId,
            Symbol,
            ISymbol,
            Name,
            Type,
            Active,
            StatusId,
            IsWatched,
            IsTraded,
            TradePrice,
            TradeDate,
            MaxDataDate,
            MinDataDate,
            PE,
            Volatility,
            Yield,
            EPS,
            DivAmount,
            Exchange,
            AssetType,
            AssetSubType
        FROM dbo.Series
        {where}
        ORDER BY Symbol;
        """,
        tuple(params),
    )


@mcp.tool()
def get_symbol_profile(symbol: str) -> dict[str, Any] | None:
    """Return profile and fundamental fields for one instrument from dbo.Series."""
    cleaned = _clean_symbol(symbol)
    rows = _fetch_all(
        """
        SELECT TOP (1)
            SeriesId,
            Name,
            Symbol,
            ISymbol,
            Type,
            Intraday,
            Active,
            MaxDataDate,
            MinDataDate,
            StatusId,
            IsTraded,
            IsWatched,
            Created,
            Updated,
            TradePrice,
            TradeDate,
            Quantity,
            PE,
            Volatility,
            Yield,
            _52WkHigh,
            _52WkLow,
            Description,
            Calculated,
            SectorId,
            IndustryId,
            NextEarningsDate,
            Rank,
            IsStopLimitOn,
            EPS,
            DivAmount,
            Exchange,
            AssetType,
            AssetSubType
        FROM dbo.Series
        WHERE UPPER(Symbol) = ? OR UPPER(ISymbol) = ?
        ORDER BY Active DESC, Symbol;
        """,
        (cleaned, cleaned),
    )
    return rows[0] if rows else None


@mcp.tool()
def get_latest_prices(symbols: list[str]) -> list[dict[str, Any]]:
    """Return latest trade price/date fields from dbo.Series for selected symbols."""
    cleaned = _clean_symbols(symbols)
    placeholders = ", ".join("?" for _ in cleaned)
    return _fetch_all(
        f"""
        SELECT
            Symbol,
            ISymbol,
            Name,
            TradePrice,
            TradeDate,
            MaxDataDate,
            MinDataDate,
            Updated,
            Type,
            AssetType,
            AssetSubType,
            Exchange
        FROM dbo.Series
        WHERE UPPER(Symbol) IN ({placeholders})
           OR UPPER(ISymbol) IN ({placeholders})
        ORDER BY Symbol;
        """,
        tuple(cleaned + cleaned),
    )


@mcp.tool()
def get_price_history(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Return daily OHLCV history from dbo.SeriesData for one symbol."""
    cleaned = _clean_symbol(symbol)
    end_value = _parse_date(end_date, "end_date") or date.today()
    start_value = _parse_date(start_date, "start_date") or (
        end_value - timedelta(days=365 * 6)
    )
    if start_value > end_value:
        raise ValueError("start_date must be before or equal to end_date.")

    limit = _clamp_limit(limit, default=2000)
    return _fetch_all(
        f"""
        SELECT TOP ({limit})
            s.Symbol,
            sd.Date,
            sd.OpenValue,
            sd.HighValue,
            sd.LowValue,
            sd.LastValue,
            sd.Volume,
            sd.TradeTypeId,
            sd.Created,
            sd.Updated
        FROM dbo.SeriesData sd
        JOIN dbo.Series s
            ON s.SeriesId = sd.SeriesId
        WHERE (UPPER(s.Symbol) = ? OR UPPER(s.ISymbol) = ?)
          AND sd.Date >= ?
          AND sd.Date <= ?
        ORDER BY sd.Date;
        """,
        (cleaned, cleaned, start_value, end_value),
    )


@mcp.tool()
def get_research_snapshot(
    symbols: list[str] | None = None,
    asset_type: str | None = None,
    watched_only: bool = False,
    traded_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return research/ranking rows from the configured research stored procedure."""
    cleaned = _clean_symbols(symbols) if symbols else None
    return _load_research_rows(
        symbols=cleaned,
        asset_type=asset_type,
        watched_only=watched_only,
        traded_only=traded_only,
        limit=limit,
    )


@mcp.tool()
def compare_symbols(symbols: list[str]) -> list[dict[str, Any]]:
    """Compare selected symbols using the research stored procedure output."""
    cleaned = _clean_symbols(symbols)
    return _load_research_rows(symbols=cleaned, limit=len(cleaned))


@mcp.tool()
def screen_instruments(
    asset_type: str | None = None,
    asset_sub_type: str | None = None,
    watched_only: bool = False,
    traded_only: bool = False,
    max_pe: float | None = None,
    min_yield: float | None = None,
    min_y1: float | None = None,
    min_y3: float | None = None,
    min_y5: float | None = None,
    min_sharpe_ratio: float | None = None,
    max_drawdown: float | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Screen instruments using fields returned by the research stored procedure."""
    rows = _load_research_rows(
        asset_type=asset_type,
        watched_only=watched_only,
        traded_only=traded_only,
        limit=MAX_ROWS,
    )

    if asset_sub_type:
        rows = [
            row
            for row in rows
            if str(row.get("AssetSubType", "")).lower() == asset_sub_type.lower()
        ]
    if max_pe is not None:
        rows = [row for row in rows if _passes_max(row.get("PE"), max_pe)]
    if min_yield is not None:
        rows = [row for row in rows if _passes_min(row.get("Yield"), min_yield)]
    if min_y1 is not None:
        rows = [row for row in rows if _passes_min(row.get("Y1"), min_y1)]
    if min_y3 is not None:
        rows = [row for row in rows if _passes_min(row.get("Y3"), min_y3)]
    if min_y5 is not None:
        rows = [row for row in rows if _passes_min(row.get("Y5"), min_y5)]
    if min_sharpe_ratio is not None:
        rows = [
            row
            for row in rows
            if _passes_min(row.get("SharpeRatio"), min_sharpe_ratio)
        ]
    if max_drawdown is not None:
        rows = [
            row
            for row in rows
            if _passes_max(_get(row, "MaximumDrawdown", "MaxDrawdown"), max_drawdown)
        ]

    return rows[: _clamp_limit(limit)]


@mcp.tool()
def get_watched_symbols(limit: int = 200) -> list[dict[str, Any]]:
    """Return instruments marked IsWatched in dbo.Series."""
    limit = _clamp_limit(limit, default=200)
    return _fetch_all(
        f"""
        SELECT TOP ({limit})
            SeriesId,
            Symbol,
            ISymbol,
            Name,
            Type,
            Active,
            StatusId,
            IsWatched,
            IsTraded,
            TradePrice,
            TradeDate,
            MaxDataDate,
            MinDataDate,
            PE,
            Volatility,
            Yield,
            EPS,
            DivAmount,
            Exchange,
            AssetType,
            AssetSubType
        FROM dbo.Series
        WHERE IsWatched = 1
        ORDER BY Symbol;
        """
    )


@mcp.tool()
def get_traded_symbols(limit: int = 200) -> list[dict[str, Any]]:
    """Return instruments marked IsTraded in dbo.Series."""
    limit = _clamp_limit(limit, default=200)
    return _fetch_all(
        f"""
        SELECT TOP ({limit})
            SeriesId,
            Symbol,
            ISymbol,
            Name,
            Type,
            Active,
            StatusId,
            IsWatched,
            IsTraded,
            TradePrice,
            TradeDate,
            MaxDataDate,
            MinDataDate,
            PE,
            Volatility,
            Yield,
            EPS,
            DivAmount,
            Exchange,
            AssetType,
            AssetSubType
        FROM dbo.Series
        WHERE IsTraded = 1
        ORDER BY Symbol;
        """
    )


@mcp.tool()
def get_data_freshness() -> dict[str, Any]:
    """Summarize instrument counts and latest available data dates."""
    summary = _fetch_all(
        """
        SELECT
            COUNT(*) AS InstrumentCount,
            SUM(CASE WHEN Active = 1 THEN 1 ELSE 0 END) AS ActiveInstrumentCount,
            SUM(CASE WHEN IsWatched = 1 THEN 1 ELSE 0 END) AS WatchedInstrumentCount,
            SUM(CASE WHEN IsTraded = 1 THEN 1 ELSE 0 END) AS TradedInstrumentCount,
            MIN(MinDataDate) AS EarliestDataDate,
            MAX(MaxDataDate) AS LatestDataDate,
            MAX(Updated) AS LatestSeriesUpdate
        FROM dbo.Series;
        """
    )
    date_buckets = _fetch_all(
        """
        SELECT TOP (10)
            CAST(MaxDataDate AS date) AS DataDate,
            COUNT(*) AS InstrumentCount
        FROM dbo.Series
        WHERE MaxDataDate IS NOT NULL
        GROUP BY CAST(MaxDataDate AS date)
        ORDER BY DataDate DESC;
        """
    )
    return {
        "summary": summary[0] if summary else {},
        "latest_date_distribution": date_buckets,
    }


@mcp.resource("investment://schema")
def schema() -> str:
    """Describe the SQL objects used by this MCP server."""
    return """
This MCP server uses these SQL Server objects:

dbo.Series:
  Instrument master data: SeriesId, Symbol, ISymbol, Name, Type, Active,
  MaxDataDate, MinDataDate, IsWatched, IsTraded, TradePrice, TradeDate,
  PE, Volatility, Yield, EPS, DivAmount, Exchange, AssetType, AssetSubType.

dbo.SeriesData:
  Daily history: SeriesDataId, SeriesId, Date, OpenValue, HighValue,
  LowValue, LastValue, Volume, Created, Updated, TradeTypeId.

Research procedure:
  The configured stored procedure returns performance, rank, PE, yield,
  volatility, Sharpe ratio, annualized return, and drawdown fields.
"""


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        _run_streamable_http()
    else:
        mcp.run(transport=transport)
