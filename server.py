from __future__ import annotations

import asyncio
import os
import re
import secrets
import hashlib
import json
import logging
import struct
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import pyodbc
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
import uvicorn


LOGGER = logging.getLogger(__name__)


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
    "dbo.InvestmentMcpGetResearch",
)
RESEARCH_PROCEDURE_HAS_FILTERS = os.getenv(
    "MCP_RESEARCH_PROCEDURE_HAS_FILTERS",
    "true",
).lower() in {"1", "true", "yes", "on"}

READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
DESTRUCTIVE_WRITE_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)


class OpeningPositionInput(BaseModel):
    """One holding to establish without changing cash."""

    symbol: str = Field(description="Investment symbol, such as VOO or MSFT.")
    quantity: float = Field(
        gt=0,
        allow_inf_nan=False,
        description="Opening quantity currently held.",
    )
    total_cost_basis: float = Field(
        ge=0,
        allow_inf_nan=False,
        description="Total cost basis for this opening quantity in account currency.",
    )


class BearerAuthASGI:
    def __init__(
        self,
        app: Any,
        token_resolver: Callable[[str], str | None],
        protected_path: str,
    ) -> None:
        self.app = app
        self.token_resolver = token_resolver
        self.protected_path = protected_path.rstrip("/") or "/"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self._is_protected_path(scope):
            authorization = self._header(scope, "authorization")
            auth_parts = authorization.split(None, 1)
            token = (
                auth_parts[1]
                if len(auth_parts) == 2 and auth_parts[0].lower() == "bearer"
                else ""
            )
            if not token or len(token) > 512:
                await self._unauthorized(send)
                return

            try:
                authentication_subject = await asyncio.to_thread(
                    self.token_resolver,
                    token,
                )
            except Exception:
                LOGGER.exception("Bearer token validation failed unexpectedly.")
                await self._service_unavailable(send)
                return

            if not authentication_subject:
                await self._unauthorized(send)
                return

            scope = dict(scope)
            state = dict(scope.get("state") or {})
            state["authentication_subject"] = authentication_subject
            scope["state"] = state

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

    async def _service_unavailable(self, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"Authentication service unavailable",
            }
        )


def _load_token_subjects() -> dict[str, str]:
    """Load legacy environment-backed tokens for a controlled migration."""
    configured: dict[str, str] = {}
    raw_mapping = os.getenv("MCP_TOKEN_SUBJECTS_JSON", "").strip()
    if raw_mapping:
        try:
            parsed = json.loads(raw_mapping)
        except json.JSONDecodeError as exc:
            raise RuntimeError("MCP_TOKEN_SUBJECTS_JSON must contain valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("MCP_TOKEN_SUBJECTS_JSON must be a JSON object.")
        for token_hash, subject in parsed.items():
            normalized_hash = str(token_hash).lower().strip()
            normalized_subject = str(subject).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
                raise RuntimeError(
                    "MCP_TOKEN_SUBJECTS_JSON keys must be SHA-256 hex digests."
                )
            if not normalized_subject:
                raise RuntimeError(
                    "MCP_TOKEN_SUBJECTS_JSON values must be authentication subjects."
                )
            configured[normalized_hash] = normalized_subject

    # Backward compatibility for the currently deployed single bearer token.
    legacy_token = os.getenv("MCP_BEARER_TOKEN", "").strip()
    if legacy_token:
        legacy_subject = os.getenv("MCP_DEFAULT_AUTH_SUBJECT", "").strip()
        if not legacy_subject:
            raise RuntimeError(
                "MCP_DEFAULT_AUTH_SUBJECT is required with MCP_BEARER_TOKEN."
            )
        configured[hashlib.sha256(legacy_token.encode("utf-8")).hexdigest()] = (
            legacy_subject
        )

    return configured


def _resolve_legacy_token(
    token: str,
    token_subjects: dict[str, str],
) -> str | None:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    matched_hash = next(
        (
            configured_hash
            for configured_hash in token_subjects
            if secrets.compare_digest(token_hash, configured_hash)
        ),
        None,
    )
    return token_subjects.get(matched_hash) if matched_hash else None


def _resolve_database_token(token: str) -> str | None:
    """Resolve a bearer token through the least-privilege SQL procedure."""
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    row = _fetch_one(
        "EXEC invest.AuthenticateApiToken @TokenHash = ?;",
        (token_hash,),
    )
    if not row:
        return None
    subject = str(row.get("AuthenticationSubject") or "").strip()
    return subject or None


def _build_token_resolver() -> Callable[[str], str | None]:
    legacy_tokens = _load_token_subjects()
    configured_mode = os.getenv("MCP_TOKEN_AUTH_MODE", "").strip().lower()
    mode = configured_mode or ("legacy" if legacy_tokens else "database")
    if mode not in {"database", "hybrid", "legacy"}:
        raise RuntimeError(
            "MCP_TOKEN_AUTH_MODE must be database, hybrid, or legacy."
        )
    if mode == "legacy" and not legacy_tokens:
        raise RuntimeError(
            "Legacy token mode requires MCP_BEARER_TOKEN or "
            "MCP_TOKEN_SUBJECTS_JSON."
        )

    def resolve(token: str) -> str | None:
        if mode in {"database", "hybrid"}:
            try:
                subject = _resolve_database_token(token)
            except pyodbc.Error:
                if mode == "database":
                    raise
                LOGGER.exception(
                    "Database token validation failed; trying temporary legacy fallback."
                )
            else:
                if subject:
                    return subject

        if mode in {"legacy", "hybrid"}:
            return _resolve_legacy_token(token, legacy_tokens)
        return None

    return resolve


def _run_streamable_http() -> None:
    host = os.getenv("FASTMCP_HOST", "127.0.0.1")
    port = int(os.getenv("FASTMCP_PORT", "8000"))
    protected_path = os.getenv("FASTMCP_STREAMABLE_HTTP_PATH", "/mcp")
    app = BearerAuthASGI(
        mcp.streamable_http_app(),
        _build_token_resolver(),
        protected_path,
    )
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


def _decode_datetimeoffset(raw_value: bytes) -> datetime:
    """Decode SQL Server's ODBC SQL_SS_TIMESTAMPOFFSET value (type -155)."""
    parts = struct.unpack("<6hI2h", raw_value)
    offset = timezone(timedelta(hours=parts[7], minutes=parts[8]))
    return datetime(
        parts[0],
        parts[1],
        parts[2],
        parts[3],
        parts[4],
        parts[5],
        parts[6] // 1000,
        offset,
    )


def _connect(*, autocommit: bool = True) -> pyodbc.Connection:
    connection = pyodbc.connect(
        _connection_string(),
        timeout=SQL_TIMEOUT_SECONDS,
        autocommit=autocommit,
    )
    connection.add_output_converter(-155, _decode_datetimeoffset)
    return connection


def _serialize(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, UUID):
        return str(value)
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


def _fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = _fetch_all(sql, params)
    return rows[0] if rows else None


def _canonical_uuid(value: str, name: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a valid UUID.") from exc


def _normalize_order_duration(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[\s-]+", "_", value.strip().upper())
    aliases = {
        "DAY": "DAY",
        "DAY_ONLY": "DAY",
        "GTC": "GTC",
        "GOOD_TILL_CANCELLED": "GTC",
        "GOOD_TIL_CANCELLED": "GTC",
        "GOOD_TILL_CANCELED": "GTC",
        "GOOD_TIL_CANCELED": "GTC",
        "GTD": "GTD",
        "GOOD_TILL_DATE": "GTD",
        "GOOD_TIL_DATE": "GTD",
        "IOC": "IOC",
        "IMMEDIATE_OR_CANCEL": "IOC",
        "FOK": "FOK",
        "FILL_OR_KILL": "FOK",
    }
    if normalized not in aliases:
        raise ValueError("duration must be DAY, GTC, GTD, IOC, or FOK.")
    return aliases[normalized]


def _parse_order_expiration(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expires_on must be a date in YYYY-MM-DD format.") from exc


def _request_authentication_subject(ctx: Context) -> str:
    request = getattr(ctx.request_context, "request", None)
    if request is not None:
        subject = str(request.scope.get("state", {}).get("authentication_subject", ""))
        if subject:
            return subject

    subject = os.getenv("MCP_DEFAULT_AUTH_SUBJECT", "").strip()
    if subject:
        return subject
    raise PermissionError("Authenticated user identity is required.")


def _current_user_id(ctx: Context) -> str:
    subject = _request_authentication_subject(ctx)
    row = _fetch_one(
        """
        SELECT UserId
        FROM invest.Users
        WHERE AuthenticationSubject = ?
          AND IsActive = 1;
        """,
        (subject,),
    )
    if not row:
        raise PermissionError("Authenticated user identity is not active.")
    return str(row["UserId"])


def _require_owned_account(
    cursor: pyodbc.Cursor,
    user_id: str,
    account_id: str,
) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT AccountId, OwnerUserId, AccountName, AccountType, BaseCurrency
        FROM invest.Accounts
        WHERE AccountId = ?
          AND OwnerUserId = ?
          AND IsActive = 1;
        """,
        (account_id, user_id),
    )
    rows = _rows_from_cursor(cursor)
    if not rows:
        raise ValueError("Account not found.")
    return rows[0]


def _write_audit(
    cursor: pyodbc.Cursor,
    actor_user_id: str,
    operation: str,
    target_type: str,
    target_id: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    cursor.execute(
        """
        INSERT INTO invest.AuditLog
            (ActorUserId, Operation, TargetType, TargetId, DetailsJson)
        VALUES (?, ?, ?, ?, ?);
        """,
        (
            actor_user_id,
            operation,
            target_type,
            target_id,
            json.dumps(details, separators=(",", ":")) if details else None,
        ),
    )


def _begin_write() -> pyodbc.Connection:
    connection = _connect(autocommit=False)
    connection.execute("SET XACT_ABORT ON;")
    return connection


def _run_idempotent_write(
    user_id: str,
    tool_name: str,
    idempotency_key: str,
    payload: dict[str, Any],
    operation: Callable[[pyodbc.Cursor], dict[str, Any]],
) -> dict[str, Any]:
    key = _canonical_uuid(idempotency_key, "idempotency_key")
    request_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(request_json.encode("utf-8")).digest()
    connection = _begin_write()
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute(
                """
                SELECT RequestHash, Status, ResponseJson
                FROM invest.IdempotencyKeys WITH (UPDLOCK, HOLDLOCK)
                WHERE UserId = ?
                  AND ToolName = ?
                  AND IdempotencyKey = ?;
                """,
                (user_id, tool_name, key),
            )
            existing = _rows_from_cursor(cursor)
            if existing:
                row = existing[0]
                stored_hash = row["RequestHash"]
                if isinstance(stored_hash, str):
                    stored_hash = bytes.fromhex(stored_hash)
                if not secrets.compare_digest(stored_hash, request_hash):
                    raise ValueError(
                        "The idempotency key was already used with different input."
                    )
                if row["Status"] == "COMPLETED" and row.get("ResponseJson"):
                    connection.commit()
                    return json.loads(row["ResponseJson"])
                raise ValueError("The idempotent operation is already in progress.")

            cursor.execute(
                """
                INSERT INTO invest.IdempotencyKeys
                    (UserId, ToolName, IdempotencyKey, RequestHash, Status)
                VALUES (?, ?, ?, ?, 'STARTED');
                """,
                (user_id, tool_name, key, request_hash),
            )
            result = operation(cursor)
            response_json = json.dumps(result, separators=(",", ":"), default=_serialize)
            cursor.execute(
                """
                UPDATE invest.IdempotencyKeys
                SET Status = 'COMPLETED',
                    ResponseJson = ?,
                    CompletedAt = SYSDATETIMEOFFSET()
                WHERE UserId = ?
                  AND ToolName = ?
                  AND IdempotencyKey = ?;
                """,
                (response_json, user_id, tool_name, key),
            )
            connection.commit()
            return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
    if watched_only and not RESEARCH_PROCEDURE_HAS_FILTERS:
        rows = [row for row in rows if _truthy(row.get("IsWatched"))]
    if traded_only and not RESEARCH_PROCEDURE_HAS_FILTERS:
        rows = [row for row in rows if _truthy(row.get("IsTraded"))]

    return rows[:limit]


@mcp.tool(annotations=READ_ONLY_TOOL)
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


@mcp.tool(annotations=READ_ONLY_TOOL)
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


@mcp.tool(annotations=READ_ONLY_TOOL)
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


@mcp.tool(annotations=READ_ONLY_TOOL)
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


@mcp.tool(annotations=READ_ONLY_TOOL)
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


@mcp.tool(annotations=READ_ONLY_TOOL)
def compare_symbols(symbols: list[str]) -> list[dict[str, Any]]:
    """Compare selected symbols using the research stored procedure output."""
    cleaned = _clean_symbols(symbols)
    return _load_research_rows(symbols=cleaned, limit=len(cleaned))


@mcp.tool(annotations=READ_ONLY_TOOL)
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


@mcp.tool(annotations=READ_ONLY_TOOL)
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


@mcp.tool(annotations=READ_ONLY_TOOL)
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


@mcp.tool(annotations=READ_ONLY_TOOL)
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


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_market_indicators(limit: int = 100) -> list[dict[str, Any]]:
    """Return the highest-ranked current market research indicators."""
    rows = _load_research_rows(limit=_clamp_limit(limit))
    return sorted(
        rows,
        key=lambda row: (_number(_get(row, "Rank", "Score")) is not None,
                         _number(_get(row, "Rank", "Score")) or 0),
        reverse=True,
    )[: _clamp_limit(limit)]


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_my_accounts(ctx: Context) -> list[dict[str, Any]]:
    """Return only investment accounts owned by the authenticated caller."""
    user_id = _current_user_id(ctx)
    return _fetch_all(
        """
        SELECT
            AccountId,
            AccountName,
            AccountType,
            ProviderName,
            BaseCurrency,
            IsActive,
            CreatedAt,
            UpdatedAt,
            RowVersion
        FROM invest.Accounts
        WHERE OwnerUserId = ?
          AND IsActive = 1
        ORDER BY AccountName, AccountId;
        """,
        (user_id,),
    )


@mcp.tool(annotations=WRITE_TOOL)
def create_account(
    account_name: str,
    idempotency_key: str,
    ctx: Context,
    account_type: str | None = None,
    provider_name: str | None = None,
    provider_account_id: str | None = None,
    base_currency: str = "USD",
) -> dict[str, Any]:
    """Create an investment account owned by the authenticated caller."""
    user_id = _current_user_id(ctx)
    normalized_name = account_name.strip()
    normalized_type = account_type.strip() if account_type else None
    normalized_provider = provider_name.strip() if provider_name else None
    normalized_provider_account_id = (
        provider_account_id.strip() if provider_account_id else None
    )
    normalized_currency = base_currency.strip().upper()
    if not normalized_name or len(normalized_name) > 200:
        raise ValueError("account_name must contain 1 to 200 characters.")
    if normalized_type is not None and len(normalized_type) > 50:
        raise ValueError("account_type cannot exceed 50 characters.")
    if normalized_provider is not None and len(normalized_provider) > 100:
        raise ValueError("provider_name cannot exceed 100 characters.")
    if (
        normalized_provider_account_id is not None
        and len(normalized_provider_account_id) > 200
    ):
        raise ValueError("provider_account_id cannot exceed 200 characters.")
    if normalized_provider_account_id is not None and normalized_provider is None:
        raise ValueError(
            "provider_name is required when provider_account_id is provided."
        )
    if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
        raise ValueError("base_currency must be a three-letter uppercase code.")
    payload = {
        "account_name": normalized_name,
        "account_type": normalized_type,
        "provider_name": normalized_provider,
        "provider_account_id": normalized_provider_account_id,
        "base_currency": normalized_currency,
    }

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        cursor.execute(
            """
            INSERT INTO invest.Accounts
                (OwnerUserId, AccountName, AccountType, ProviderName,
                 ProviderAccountId, BaseCurrency)
            OUTPUT
                inserted.AccountId,
                inserted.AccountName,
                inserted.AccountType,
                inserted.ProviderName,
                inserted.BaseCurrency,
                inserted.IsActive,
                inserted.CreatedAt,
                inserted.UpdatedAt,
                inserted.RowVersion
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                user_id,
                normalized_name,
                normalized_type,
                normalized_provider,
                normalized_provider_account_id,
                normalized_currency,
            ),
        )
        account = _rows_from_cursor(cursor)[0]
        _write_audit(
            cursor,
            user_id,
            "CREATE_ACCOUNT",
            "Account",
            str(account["AccountId"]),
            {
                "account_name": normalized_name,
                "account_type": normalized_type,
                "provider_name": normalized_provider,
                "base_currency": normalized_currency,
            },
        )
        return account

    return _run_idempotent_write(
        user_id,
        "create_account",
        idempotency_key,
        payload,
        operation,
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_my_portfolio(
    ctx: Context,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Return caller-owned accounts, cash balances, and transaction-derived positions."""
    user_id = _current_user_id(ctx)
    normalized_account_id = (
        _canonical_uuid(account_id, "account_id") if account_id else None
    )
    account_filter = "AND a.AccountId = ?" if normalized_account_id else ""
    params: tuple[Any, ...] = (
        (user_id, normalized_account_id)
        if normalized_account_id
        else (user_id,)
    )
    accounts = _fetch_all(
        f"""
        SELECT
            a.AccountId,
            a.AccountName,
            a.AccountType,
            a.BaseCurrency,
            a.RowVersion
        FROM invest.Accounts a
        WHERE a.OwnerUserId = ?
          AND a.IsActive = 1
          {account_filter}
        ORDER BY a.AccountName;
        """,
        params,
    )
    if normalized_account_id and not accounts:
        raise ValueError("Account not found.")

    balances = _fetch_all(
        f"""
        SELECT
            cb.AccountId,
            cb.Currency,
            cb.TotalAmount,
            cb.AvailableAmount,
            cb.AsOf,
            cb.RowVersion
        FROM invest.CashBalances cb
        JOIN invest.Accounts a
          ON a.AccountId = cb.AccountId
         AND a.OwnerUserId = cb.UserId
        WHERE cb.UserId = ?
          AND a.IsActive = 1
          {account_filter}
        ORDER BY cb.AccountId, cb.Currency;
        """,
        params,
    )
    positions = _fetch_all(
        f"""
        SELECT
            t.AccountId,
            t.Symbol,
            SUM(
                CASE
                    WHEN UPPER(t.TransactionType) IN
                         ('BUY', 'PURCHASE', 'OPENING_POSITION')
                        THEN COALESCE(t.Quantity, 0)
                    WHEN UPPER(t.TransactionType) IN ('SELL', 'SALE')
                        THEN -COALESCE(t.Quantity, 0)
                    ELSE 0
                END
            ) AS Quantity,
            MAX(t.OccurredAt) AS LastActivityAt,
            SUM(
                CASE
                    WHEN UPPER(t.TransactionType) = 'OPENING_POSITION'
                        THEN t.GrossAmount
                    ELSE 0
                END
            ) AS OpeningCostBasis,
            CASE
                WHEN SUM(
                    CASE
                        WHEN UPPER(t.TransactionType) = 'OPENING_POSITION'
                            THEN COALESCE(t.Quantity, 0)
                        ELSE 0
                    END
                ) = 0 THEN NULL
                ELSE SUM(
                    CASE
                        WHEN UPPER(t.TransactionType) = 'OPENING_POSITION'
                            THEN t.GrossAmount
                        ELSE 0
                    END
                ) / SUM(
                    CASE
                        WHEN UPPER(t.TransactionType) = 'OPENING_POSITION'
                            THEN COALESCE(t.Quantity, 0)
                        ELSE 0
                    END
                )
            END AS OpeningAverageCost
        FROM invest.Transactions t
        JOIN invest.Accounts a
          ON a.AccountId = t.AccountId
         AND a.OwnerUserId = t.UserId
        WHERE t.UserId = ?
          AND t.IsDeleted = 0
          AND t.Symbol IS NOT NULL
          AND a.IsActive = 1
          {account_filter}
        GROUP BY t.AccountId, t.Symbol
        HAVING SUM(
            CASE
                WHEN UPPER(t.TransactionType) IN
                     ('BUY', 'PURCHASE', 'OPENING_POSITION')
                    THEN COALESCE(t.Quantity, 0)
                WHEN UPPER(t.TransactionType) IN ('SELL', 'SALE')
                    THEN -COALESCE(t.Quantity, 0)
                ELSE 0
            END
        ) <> 0
        ORDER BY t.AccountId, t.Symbol;
        """,
        params,
    )
    return {"accounts": accounts, "cash_balances": balances, "positions": positions}


@mcp.tool(annotations=WRITE_TOOL)
def import_opening_positions(
    account_id: str,
    positions: list[OpeningPositionInput],
    idempotency_key: str,
    ctx: Context,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Import opening quantities and cost bases without changing account cash."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    batch_key = _canonical_uuid(idempotency_key, "idempotency_key")
    if not positions:
        raise ValueError("positions must contain at least one opening position.")
    if len(positions) > 500:
        raise ValueError("positions cannot contain more than 500 entries.")
    try:
        occurred = (
            datetime.fromisoformat(as_of)
            if as_of
            else datetime.now().astimezone()
        )
    except ValueError as exc:
        raise ValueError("as_of must be an ISO-8601 timestamp.") from exc
    if occurred.tzinfo is None:
        raise ValueError("as_of must include a UTC offset or timezone.")

    normalized_positions: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for position in positions:
        symbol = _clean_symbol(position.symbol)
        if symbol in seen_symbols:
            raise ValueError(f"positions contains duplicate symbol {symbol}.")
        seen_symbols.add(symbol)
        quantity = Decimal(str(position.quantity))
        total_cost_basis = Decimal(str(position.total_cost_basis))
        if quantity <= 0:
            raise ValueError(f"quantity for {symbol} must be greater than zero.")
        if total_cost_basis < 0:
            raise ValueError(f"total_cost_basis for {symbol} cannot be negative.")
        unit_cost = total_cost_basis / quantity
        normalized_positions.append(
            {
                "symbol": symbol,
                "quantity": str(quantity),
                "total_cost_basis": str(total_cost_basis),
                "unit_cost": str(unit_cost),
            }
        )

    payload = {
        "account_id": normalized_account_id,
        "positions": normalized_positions,
        "as_of": occurred.isoformat(),
    }

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        account = _require_owned_account(
            cursor,
            user_id,
            normalized_account_id,
        )
        currency = str(account["BaseCurrency"])
        imported: list[dict[str, Any]] = []
        for position in normalized_positions:
            cursor.execute(
                """
                SELECT 1
                FROM invest.Transactions WITH (UPDLOCK, HOLDLOCK)
                WHERE UserId = ?
                  AND AccountId = ?
                  AND Symbol = ?
                  AND TransactionType = 'OPENING_POSITION'
                  AND IsDeleted = 0;
                """,
                (user_id, normalized_account_id, position["symbol"]),
            )
            if _rows_from_cursor(cursor):
                raise ValueError(
                    f"Opening position for {position['symbol']} already exists."
                )
            external_id = f"opening:{batch_key}:{position['symbol']}"
            metadata_json = json.dumps(
                {
                    "source": "opening_position_import",
                    "cash_impact": False,
                    "idempotency_key": batch_key,
                },
                separators=(",", ":"),
            )
            cursor.execute(
                """
                INSERT INTO invest.Transactions
                    (UserId, AccountId, ExternalTransactionId,
                     TransactionType, Symbol, Quantity, Price, GrossAmount,
                     Fees, Currency, TradeDate, OccurredAt, MetadataJson)
                OUTPUT
                    inserted.TransactionId,
                    inserted.AccountId,
                    inserted.ExternalTransactionId,
                    inserted.TransactionType,
                    inserted.Symbol,
                    inserted.Quantity,
                    inserted.Price,
                    inserted.GrossAmount,
                    inserted.Currency,
                    inserted.TradeDate,
                    inserted.OccurredAt
                VALUES
                    (?, ?, ?, 'OPENING_POSITION', ?, ?, ?, ?, 0, ?, ?, ?, ?);
                """,
                (
                    user_id,
                    normalized_account_id,
                    external_id,
                    position["symbol"],
                    Decimal(position["quantity"]),
                    Decimal(position["unit_cost"]),
                    Decimal(position["total_cost_basis"]),
                    currency,
                    occurred.date(),
                    occurred,
                    metadata_json,
                ),
            )
            imported.append(_rows_from_cursor(cursor)[0])

        _write_audit(
            cursor,
            user_id,
            "IMPORT_OPENING_POSITIONS",
            "Account",
            normalized_account_id,
            {
                "position_count": len(imported),
                "symbols": [row["Symbol"] for row in imported],
                "as_of": occurred.isoformat(),
                "cash_impact": False,
            },
        )
        return {
            "account_id": normalized_account_id,
            "position_count": len(imported),
            "cash_updated": False,
            "positions": imported,
        }

    return _run_idempotent_write(
        user_id,
        "import_opening_positions",
        batch_key,
        payload,
        operation,
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_my_open_orders(
    ctx: Context,
    account_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return non-deleted open orders owned by the authenticated caller."""
    user_id = _current_user_id(ctx)
    normalized_account_id = (
        _canonical_uuid(account_id, "account_id") if account_id else None
    )
    account_filter = "AND o.AccountId = ?" if normalized_account_id else ""
    params: tuple[Any, ...] = (
        (user_id, normalized_account_id)
        if normalized_account_id
        else (user_id,)
    )
    return _fetch_all(
        f"""
        SELECT
            o.OrderId,
            o.AccountId,
            o.ClientOrderId,
            o.Symbol,
            o.Side,
            o.OrderType,
            o.Quantity,
            o.FilledQuantity,
            o.LimitPrice,
            o.StopPrice,
            o.Duration,
            o.ExpiresOn,
            CASE
                WHEN o.ExpiresOn IS NOT NULL
                 AND o.ExpiresOn < CONVERT(date, SYSDATETIMEOFFSET())
                    THEN CAST(1 AS bit)
                ELSE CAST(0 AS bit)
            END AS IsPastExpiration,
            o.Status,
            o.SubmittedAt,
            o.UpdatedAt,
            o.RowVersion
        FROM invest.OpenOrders o
        JOIN invest.Accounts a
          ON a.AccountId = o.AccountId
         AND a.OwnerUserId = o.UserId
        WHERE o.UserId = ?
          AND o.IsDeleted = 0
          AND o.Status IN ('PENDING', 'OPEN', 'PARTIALLY_FILLED')
          AND a.IsActive = 1
          {account_filter}
        ORDER BY o.SubmittedAt DESC;
        """,
        params,
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_my_strategy(
    ctx: Context,
    account_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return strategy rules owned by the authenticated caller."""
    user_id = _current_user_id(ctx)
    normalized_account_id = (
        _canonical_uuid(account_id, "account_id") if account_id else None
    )
    account_filter = "AND AccountId = ?" if normalized_account_id else ""
    params: tuple[Any, ...] = (
        (user_id, normalized_account_id)
        if normalized_account_id
        else (user_id,)
    )
    return _fetch_all(
        f"""
        SELECT
            StrategyRuleId,
            AccountId,
            RuleName,
            RuleType,
            RuleJson,
            IsEnabled,
            CreatedAt,
            UpdatedAt,
            RowVersion
        FROM invest.StrategyRules
        WHERE UserId = ?
          {account_filter}
        ORDER BY RuleName, StrategyRuleId;
        """,
        params,
    )


@mcp.tool(annotations=WRITE_TOOL)
def create_limit_order_record(
    account_id: str,
    client_order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    limit_price: float,
    idempotency_key: str,
    ctx: Context,
    duration: str | None = None,
    expires_on: str | None = None,
) -> dict[str, Any]:
    """Create a caller-owned limit-order record with optional duration and expiration."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    normalized_symbol = _clean_symbol(symbol)
    normalized_side = side.strip().upper()
    normalized_client_order_id = client_order_id.strip()
    normalized_duration = _normalize_order_duration(duration)
    expiration_date = _parse_order_expiration(expires_on)
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL.")
    if quantity <= 0 or limit_price <= 0:
        raise ValueError("quantity and limit_price must be greater than zero.")
    if not normalized_client_order_id or len(normalized_client_order_id) > 200:
        raise ValueError("client_order_id must contain 1 to 200 characters.")
    payload = {
        "account_id": normalized_account_id,
        "client_order_id": normalized_client_order_id,
        "symbol": normalized_symbol,
        "side": normalized_side,
        "quantity": quantity,
        "limit_price": limit_price,
        "duration": normalized_duration,
        "expires_on": expiration_date.isoformat() if expiration_date else None,
    }

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        _require_owned_account(cursor, user_id, normalized_account_id)
        cursor.execute(
            """
            INSERT INTO invest.OpenOrders
                (UserId, AccountId, ClientOrderId, Symbol, Side, OrderType,
                 Quantity, LimitPrice, Duration, ExpiresOn, Status, SubmittedAt)
            OUTPUT
                inserted.OrderId,
                inserted.AccountId,
                inserted.ClientOrderId,
                inserted.Symbol,
                inserted.Side,
                inserted.Quantity,
                inserted.LimitPrice,
                inserted.Duration,
                inserted.ExpiresOn,
                inserted.Status,
                inserted.SubmittedAt,
                inserted.RowVersion
            VALUES (?, ?, ?, ?, ?, 'LIMIT', ?, ?, ?, ?, 'OPEN', SYSDATETIMEOFFSET());
            """,
            (
                user_id,
                normalized_account_id,
                normalized_client_order_id,
                normalized_symbol,
                normalized_side,
                quantity,
                limit_price,
                normalized_duration,
                expiration_date,
            ),
        )
        order = _rows_from_cursor(cursor)[0]
        cursor.execute(
            """
            INSERT INTO invest.OrderStatusEvents
                (OrderId, UserId, AccountId, PreviousStatus, NewStatus, ActorUserId)
            VALUES (?, ?, ?, NULL, 'OPEN', ?);
            """,
            (order["OrderId"], user_id, normalized_account_id, user_id),
        )
        _write_audit(
            cursor,
            user_id,
            "CREATE_LIMIT_ORDER_RECORD",
            "OpenOrder",
            str(order["OrderId"]),
            payload,
        )
        return order

    return _run_idempotent_write(
        user_id,
        "create_limit_order_record",
        idempotency_key,
        payload,
        operation,
    )


@mcp.tool(annotations=WRITE_TOOL)
def update_limit_order_record(
    account_id: str,
    order_id: str,
    expected_version: str,
    idempotency_key: str,
    ctx: Context,
    quantity: float | None = None,
    limit_price: float | None = None,
    duration: str | None = None,
    expires_on: str | None = None,
) -> dict[str, Any]:
    """Update quantity, price, duration, or expiration using optimistic locking."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    normalized_order_id = _canonical_uuid(order_id, "order_id")
    try:
        version = bytes.fromhex(expected_version)
    except ValueError as exc:
        raise ValueError("expected_version must be a hexadecimal rowversion.") from exc
    if len(version) != 8:
        raise ValueError("expected_version must represent an 8-byte rowversion.")
    if quantity is None and limit_price is None and duration is None and expires_on is None:
        raise ValueError("Provide quantity, limit_price, duration, or expires_on to update.")
    if quantity is not None and quantity <= 0:
        raise ValueError("quantity must be greater than zero.")
    if limit_price is not None and limit_price <= 0:
        raise ValueError("limit_price must be greater than zero.")
    normalized_duration = _normalize_order_duration(duration)
    expiration_date = _parse_order_expiration(expires_on)
    payload = {
        "account_id": normalized_account_id,
        "order_id": normalized_order_id,
        "expected_version": expected_version.lower(),
        "quantity": quantity,
        "limit_price": limit_price,
        "duration": normalized_duration,
        "expires_on": expiration_date.isoformat() if expiration_date else None,
    }

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        _require_owned_account(cursor, user_id, normalized_account_id)
        assignments: list[str] = []
        values: list[Any] = []
        if quantity is not None:
            assignments.append("Quantity = ?")
            values.append(quantity)
        if limit_price is not None:
            assignments.append("LimitPrice = ?")
            values.append(limit_price)
        if duration is not None:
            assignments.append("Duration = ?")
            values.append(normalized_duration)
        if expires_on is not None:
            assignments.append("ExpiresOn = ?")
            values.append(expiration_date)
        assignments.append("UpdatedAt = SYSDATETIMEOFFSET()")
        values.extend([normalized_order_id, user_id, normalized_account_id, version])
        cursor.execute(
            f"""
            UPDATE invest.OpenOrders
            SET {', '.join(assignments)}
            OUTPUT
                inserted.OrderId,
                inserted.AccountId,
                inserted.ClientOrderId,
                inserted.Quantity,
                inserted.LimitPrice,
                inserted.Duration,
                inserted.ExpiresOn,
                inserted.Status,
                inserted.UpdatedAt,
                inserted.RowVersion
            WHERE OrderId = ?
              AND UserId = ?
              AND AccountId = ?
              AND RowVersion = ?
              AND IsDeleted = 0
              AND Status IN ('PENDING', 'OPEN', 'PARTIALLY_FILLED');
            """,
            tuple(values),
        )
        updated = _rows_from_cursor(cursor)
        if not updated:
            cursor.execute(
                """
                SELECT 1
                FROM invest.OpenOrders
                WHERE OrderId = ? AND UserId = ? AND AccountId = ? AND IsDeleted = 0;
                """,
                (normalized_order_id, user_id, normalized_account_id),
            )
            if not _rows_from_cursor(cursor):
                raise ValueError("Order not found.")
            raise ValueError("Order version conflict; refresh the order and retry.")
        order = updated[0]
        _write_audit(
            cursor,
            user_id,
            "UPDATE_LIMIT_ORDER_RECORD",
            "OpenOrder",
            normalized_order_id,
            payload,
        )
        return order

    return _run_idempotent_write(
        user_id,
        "update_limit_order_record",
        idempotency_key,
        payload,
        operation,
    )


@mcp.tool(annotations=DESTRUCTIVE_WRITE_TOOL)
def cancel_limit_order_record(
    account_id: str,
    order_id: str,
    expected_version: str,
    idempotency_key: str,
    ctx: Context,
    reason: str | None = None,
) -> dict[str, Any]:
    """Soft-cancel a caller-owned order and append its status history."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    normalized_order_id = _canonical_uuid(order_id, "order_id")
    try:
        version = bytes.fromhex(expected_version)
    except ValueError as exc:
        raise ValueError("expected_version must be a hexadecimal rowversion.") from exc
    if len(version) != 8:
        raise ValueError("expected_version must represent an 8-byte rowversion.")
    payload = {
        "account_id": normalized_account_id,
        "order_id": normalized_order_id,
        "expected_version": expected_version.lower(),
        "reason": reason,
    }

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        _require_owned_account(cursor, user_id, normalized_account_id)
        cursor.execute(
            """
            UPDATE invest.OpenOrders
            SET Status = 'CANCELLED',
                IsDeleted = 1,
                DeletedAt = SYSDATETIMEOFFSET(),
                DeletedByUserId = ?,
                UpdatedAt = SYSDATETIMEOFFSET()
            OUTPUT
                inserted.OrderId,
                inserted.AccountId,
                inserted.ClientOrderId,
                inserted.Status,
                inserted.DeletedAt,
                inserted.RowVersion
            WHERE OrderId = ?
              AND UserId = ?
              AND AccountId = ?
              AND RowVersion = ?
              AND IsDeleted = 0
              AND Status IN ('PENDING', 'OPEN', 'PARTIALLY_FILLED');
            """,
            (user_id, normalized_order_id, user_id, normalized_account_id, version),
        )
        updated = _rows_from_cursor(cursor)
        if not updated:
            cursor.execute(
                """
                SELECT 1 FROM invest.OpenOrders
                WHERE OrderId = ? AND UserId = ? AND AccountId = ? AND IsDeleted = 0;
                """,
                (normalized_order_id, user_id, normalized_account_id),
            )
            if not _rows_from_cursor(cursor):
                raise ValueError("Order not found.")
            raise ValueError("Order version conflict; refresh the order and retry.")
        order = updated[0]
        cursor.execute(
            """
            INSERT INTO invest.OrderStatusEvents
                (OrderId, UserId, AccountId, PreviousStatus, NewStatus, ActorUserId, Reason)
            VALUES (?, ?, ?, NULL, 'CANCELLED', ?, ?);
            """,
            (normalized_order_id, user_id, normalized_account_id, user_id, reason),
        )
        _write_audit(cursor, user_id, "CANCEL_LIMIT_ORDER_RECORD", "OpenOrder", normalized_order_id, {"reason": reason})
        return order

    return _run_idempotent_write(
        user_id,
        "cancel_limit_order_record",
        idempotency_key,
        payload,
        operation,
    )


@mcp.tool(annotations=WRITE_TOOL)
def record_trade_execution(
    account_id: str,
    client_execution_id: str,
    transaction_type: str,
    symbol: str,
    quantity: float,
    price: float,
    gross_amount: float,
    currency: str,
    idempotency_key: str,
    ctx: Context,
    fees: float = 0,
    order_id: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Append a trade execution and atomically update cash and an optional order record."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    normalized_order_id = _canonical_uuid(order_id, "order_id") if order_id else None
    normalized_type = transaction_type.strip().upper()
    normalized_symbol = _clean_symbol(symbol)
    normalized_currency = currency.strip().upper()
    execution_id = client_execution_id.strip()
    if normalized_type not in {"BUY", "SELL"}:
        raise ValueError("transaction_type must be BUY or SELL.")
    if quantity <= 0 or price <= 0 or gross_amount < 0 or fees < 0:
        raise ValueError("quantity and price must be positive; amounts cannot be negative.")
    if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
        raise ValueError("currency must be a three-letter uppercase code.")
    if not execution_id or len(execution_id) > 200:
        raise ValueError("client_execution_id must contain 1 to 200 characters.")
    try:
        occurred = datetime.fromisoformat(occurred_at) if occurred_at else datetime.now().astimezone()
    except ValueError as exc:
        raise ValueError("occurred_at must be an ISO-8601 timestamp.") from exc
    cash_delta = (
        -(Decimal(str(gross_amount)) + Decimal(str(fees)))
        if normalized_type == "BUY"
        else Decimal(str(gross_amount)) - Decimal(str(fees))
    )
    payload = {
        "account_id": normalized_account_id,
        "client_execution_id": execution_id,
        "transaction_type": normalized_type,
        "symbol": normalized_symbol,
        "quantity": quantity,
        "price": price,
        "gross_amount": gross_amount,
        "fees": fees,
        "currency": normalized_currency,
        "order_id": normalized_order_id,
        "occurred_at": occurred.isoformat(),
    }

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        _require_owned_account(cursor, user_id, normalized_account_id)
        cursor.execute(
            """
            INSERT INTO invest.Transactions
                (UserId, AccountId, ExternalTransactionId, TransactionType,
                 Symbol, Quantity, Price, GrossAmount, Fees, Currency, OccurredAt)
            OUTPUT
                inserted.TransactionId,
                inserted.AccountId,
                inserted.ExternalTransactionId,
                inserted.TransactionType,
                inserted.Symbol,
                inserted.Quantity,
                inserted.Price,
                inserted.GrossAmount,
                inserted.Fees,
                inserted.Currency,
                inserted.OccurredAt
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                user_id,
                normalized_account_id,
                execution_id,
                normalized_type,
                normalized_symbol,
                quantity,
                price,
                gross_amount,
                fees,
                normalized_currency,
                occurred,
            ),
        )
        transaction = _rows_from_cursor(cursor)[0]
        cursor.execute(
            """
            UPDATE invest.CashBalances WITH (UPDLOCK, SERIALIZABLE)
            SET TotalAmount = TotalAmount + ?,
                AvailableAmount = AvailableAmount + ?,
                AsOf = ?,
                UpdatedAt = SYSDATETIMEOFFSET()
            OUTPUT inserted.AccountId
            WHERE UserId = ? AND AccountId = ? AND Currency = ?;
            """,
            (cash_delta, cash_delta, occurred, user_id, normalized_account_id, normalized_currency),
        )
        updated_balances = _rows_from_cursor(cursor)
        if not updated_balances:
            cursor.execute(
                """
                INSERT INTO invest.CashBalances
                    (UserId, AccountId, Currency, TotalAmount, AvailableAmount, AsOf)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (user_id, normalized_account_id, normalized_currency, cash_delta, cash_delta, occurred),
            )

        order_result: dict[str, Any] | None = None
        if normalized_order_id:
            cursor.execute(
                """
                SELECT Status, Quantity, FilledQuantity
                FROM invest.OpenOrders WITH (UPDLOCK, HOLDLOCK)
                WHERE OrderId = ?
                  AND UserId = ?
                  AND AccountId = ?
                  AND IsDeleted = 0;
                """,
                (normalized_order_id, user_id, normalized_account_id),
            )
            current_rows = _rows_from_cursor(cursor)
            if not current_rows:
                raise ValueError("Order not found.")
            current = current_rows[0]
            new_filled = Decimal(str(current["FilledQuantity"])) + Decimal(str(quantity))
            order_quantity = Decimal(str(current["Quantity"]))
            if new_filled > order_quantity:
                raise ValueError("Execution quantity exceeds the order's remaining quantity.")
            new_status = "FILLED" if new_filled == order_quantity else "PARTIALLY_FILLED"
            cursor.execute(
                """
                UPDATE invest.OpenOrders
                SET FilledQuantity = ?, Status = ?, UpdatedAt = SYSDATETIMEOFFSET()
                OUTPUT inserted.OrderId, inserted.FilledQuantity, inserted.Status, inserted.RowVersion
                WHERE OrderId = ? AND UserId = ? AND AccountId = ?;
                """,
                (new_filled, new_status, normalized_order_id, user_id, normalized_account_id),
            )
            order_result = _rows_from_cursor(cursor)[0]
            cursor.execute(
                """
                INSERT INTO invest.OrderStatusEvents
                    (OrderId, UserId, AccountId, PreviousStatus, NewStatus, ActorUserId, Reason)
                VALUES (?, ?, ?, ?, ?, ?, 'TRADE_EXECUTION');
                """,
                (normalized_order_id, user_id, normalized_account_id, current["Status"], new_status, user_id),
            )

        _write_audit(
            cursor,
            user_id,
            "RECORD_TRADE_EXECUTION",
            "Transaction",
            str(transaction["TransactionId"]),
            {"account_id": normalized_account_id, "client_execution_id": execution_id},
        )
        return {"transaction": transaction, "order": order_result, "cash_delta": float(cash_delta)}

    return _run_idempotent_write(
        user_id,
        "record_trade_execution",
        idempotency_key,
        payload,
        operation,
    )


@mcp.tool(annotations=WRITE_TOOL)
def update_cash_balance(
    account_id: str,
    currency: str,
    total_amount: float,
    available_amount: float,
    as_of: str,
    idempotency_key: str,
    ctx: Context,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Set an owned account's cash balance with idempotency and optional optimistic locking."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    normalized_currency = currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
        raise ValueError("currency must be a three-letter uppercase code.")
    try:
        as_of_value = datetime.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError("as_of must be an ISO-8601 timestamp.") from exc
    version: bytes | None = None
    if expected_version:
        try:
            version = bytes.fromhex(expected_version)
        except ValueError as exc:
            raise ValueError("expected_version must be a hexadecimal rowversion.") from exc
        if len(version) != 8:
            raise ValueError("expected_version must represent an 8-byte rowversion.")
    payload = {
        "account_id": normalized_account_id,
        "currency": normalized_currency,
        "total_amount": total_amount,
        "available_amount": available_amount,
        "as_of": as_of_value.isoformat(),
        "expected_version": expected_version,
    }

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        _require_owned_account(cursor, user_id, normalized_account_id)
        version_filter = "AND RowVersion = ?" if version is not None else ""
        params: list[Any] = [
            total_amount,
            available_amount,
            as_of_value,
            user_id,
            normalized_account_id,
            normalized_currency,
        ]
        if version is not None:
            params.append(version)
        cursor.execute(
            f"""
            UPDATE invest.CashBalances
            SET TotalAmount = ?, AvailableAmount = ?, AsOf = ?,
                UpdatedAt = SYSDATETIMEOFFSET()
            OUTPUT
                inserted.AccountId, inserted.Currency, inserted.TotalAmount,
                inserted.AvailableAmount, inserted.AsOf, inserted.RowVersion
            WHERE UserId = ? AND AccountId = ? AND Currency = ? {version_filter};
            """,
            tuple(params),
        )
        rows = _rows_from_cursor(cursor)
        if not rows:
            if version is not None:
                raise ValueError("Cash balance version conflict or balance not found.")
            cursor.execute(
                """
                INSERT INTO invest.CashBalances
                    (UserId, AccountId, Currency, TotalAmount, AvailableAmount, AsOf)
                OUTPUT
                    inserted.AccountId, inserted.Currency, inserted.TotalAmount,
                    inserted.AvailableAmount, inserted.AsOf, inserted.RowVersion
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (user_id, normalized_account_id, normalized_currency, total_amount, available_amount, as_of_value),
            )
            rows = _rows_from_cursor(cursor)
        result = rows[0]
        _write_audit(cursor, user_id, "UPDATE_CASH_BALANCE", "CashBalance", f"{normalized_account_id}:{normalized_currency}")
        return result

    return _run_idempotent_write(user_id, "update_cash_balance", idempotency_key, payload, operation)


@mcp.tool(annotations=WRITE_TOOL)
def update_my_strategy(
    rule_name: str,
    rule_type: str,
    rule_json: str,
    idempotency_key: str,
    ctx: Context,
    strategy_rule_id: str | None = None,
    account_id: str | None = None,
    is_enabled: bool = True,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Create or update a caller-owned strategy rule."""
    user_id = _current_user_id(ctx)
    normalized_strategy_id = _canonical_uuid(strategy_rule_id, "strategy_rule_id") if strategy_rule_id else None
    normalized_account_id = _canonical_uuid(account_id, "account_id") if account_id else None
    try:
        parsed_rule = json.loads(rule_json)
    except json.JSONDecodeError as exc:
        raise ValueError("rule_json must contain valid JSON.") from exc
    canonical_rule_json = json.dumps(parsed_rule, separators=(",", ":"))
    version = bytes.fromhex(expected_version) if expected_version else None
    if version is not None and len(version) != 8:
        raise ValueError("expected_version must represent an 8-byte rowversion.")
    payload = {
        "strategy_rule_id": normalized_strategy_id,
        "account_id": normalized_account_id,
        "rule_name": rule_name,
        "rule_type": rule_type,
        "rule_json": canonical_rule_json,
        "is_enabled": is_enabled,
        "expected_version": expected_version,
    }

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        if normalized_account_id:
            _require_owned_account(cursor, user_id, normalized_account_id)
        if normalized_strategy_id:
            if version is None:
                raise ValueError("expected_version is required when updating a strategy.")
            cursor.execute(
                """
                UPDATE invest.StrategyRules
                SET AccountId = ?, RuleName = ?, RuleType = ?, RuleJson = ?,
                    IsEnabled = ?, UpdatedAt = SYSDATETIMEOFFSET()
                OUTPUT
                    inserted.StrategyRuleId, inserted.AccountId, inserted.RuleName,
                    inserted.RuleType, inserted.RuleJson, inserted.IsEnabled,
                    inserted.UpdatedAt, inserted.RowVersion
                WHERE StrategyRuleId = ? AND UserId = ? AND RowVersion = ?;
                """,
                (normalized_account_id, rule_name, rule_type, canonical_rule_json, int(is_enabled), normalized_strategy_id, user_id, version),
            )
            rows = _rows_from_cursor(cursor)
            if not rows:
                raise ValueError("Strategy not found or version conflict.")
        else:
            cursor.execute(
                """
                INSERT INTO invest.StrategyRules
                    (UserId, AccountId, RuleName, RuleType, RuleJson, IsEnabled)
                OUTPUT
                    inserted.StrategyRuleId, inserted.AccountId, inserted.RuleName,
                    inserted.RuleType, inserted.RuleJson, inserted.IsEnabled,
                    inserted.UpdatedAt, inserted.RowVersion
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (user_id, normalized_account_id, rule_name, rule_type, canonical_rule_json, int(is_enabled)),
            )
            rows = _rows_from_cursor(cursor)
        result = rows[0]
        _write_audit(cursor, user_id, "UPDATE_MY_STRATEGY", "StrategyRule", str(result["StrategyRuleId"]))
        return result

    return _run_idempotent_write(user_id, "update_my_strategy", idempotency_key, payload, operation)


@mcp.tool(annotations=READ_ONLY_TOOL)
def list_portfolio_access(account_id: str, ctx: Context) -> list[dict[str, Any]]:
    """List active grants for a portfolio owned by the authenticated caller."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    rows = _fetch_all(
        """
        SELECT
            pg.PortfolioGrantId,
            pg.AccountId,
            pg.RecipientUserId,
            u.DisplayName AS RecipientDisplayName,
            pg.PermissionsJson,
            pg.GrantedAt,
            pg.ExpiresAt,
            pg.RevokedAt,
            pg.RowVersion
        FROM invest.PortfolioGrants pg
        JOIN invest.Accounts a
          ON a.AccountId = pg.AccountId AND a.OwnerUserId = pg.OwnerUserId
        JOIN invest.Users u ON u.UserId = pg.RecipientUserId
        WHERE pg.AccountId = ? AND pg.OwnerUserId = ?
        ORDER BY pg.GrantedAt DESC;
        """,
        (normalized_account_id, user_id),
    )
    if not rows and not _fetch_one(
        "SELECT AccountId FROM invest.Accounts WHERE AccountId = ? AND OwnerUserId = ? AND IsActive = 1;",
        (normalized_account_id, user_id),
    ):
        raise ValueError("Account not found.")
    return rows


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_shared_portfolios(ctx: Context) -> list[dict[str, Any]]:
    """Return portfolios explicitly shared with the authenticated caller."""
    user_id = _current_user_id(ctx)
    return _fetch_all(
        """
        SELECT
            pg.PortfolioGrantId,
            a.AccountId,
            a.AccountName,
            a.AccountType,
            a.BaseCurrency,
            pg.OwnerUserId,
            owner.DisplayName AS OwnerDisplayName,
            pg.PermissionsJson,
            pg.GrantedAt,
            pg.ExpiresAt
        FROM invest.PortfolioGrants pg
        JOIN invest.Accounts a
          ON a.AccountId = pg.AccountId AND a.OwnerUserId = pg.OwnerUserId
        JOIN invest.Users owner ON owner.UserId = pg.OwnerUserId
        WHERE pg.RecipientUserId = ?
          AND pg.RevokedAt IS NULL
          AND (pg.ExpiresAt IS NULL OR pg.ExpiresAt > SYSDATETIMEOFFSET())
          AND a.IsActive = 1
        ORDER BY a.AccountName;
        """,
        (user_id,),
    )


@mcp.tool(annotations=WRITE_TOOL)
def share_portfolio(
    account_id: str,
    recipient_user_id: str,
    permissions: list[str],
    idempotency_key: str,
    ctx: Context,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Grant explicit portfolio permissions to another active user."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    normalized_recipient_id = _canonical_uuid(recipient_user_id, "recipient_user_id")
    if normalized_recipient_id == user_id:
        raise ValueError("A portfolio cannot be shared with its owner.")
    allowed_permissions = {"VIEW", "TRADE", "MANAGE"}
    normalized_permissions = sorted({permission.strip().upper() for permission in permissions})
    if not normalized_permissions or not set(normalized_permissions) <= allowed_permissions:
        raise ValueError("permissions must contain VIEW, TRADE, and/or MANAGE.")
    try:
        expires = datetime.fromisoformat(expires_at) if expires_at else None
    except ValueError as exc:
        raise ValueError("expires_at must be an ISO-8601 timestamp.") from exc
    permissions_json = json.dumps(normalized_permissions, separators=(",", ":"))
    payload = {
        "account_id": normalized_account_id,
        "recipient_user_id": normalized_recipient_id,
        "permissions": normalized_permissions,
        "expires_at": expires.isoformat() if expires else None,
    }

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        _require_owned_account(cursor, user_id, normalized_account_id)
        cursor.execute("SELECT 1 FROM invest.Users WHERE UserId = ? AND IsActive = 1;", (normalized_recipient_id,))
        if not _rows_from_cursor(cursor):
            raise ValueError("Recipient not found.")
        cursor.execute(
            """
            UPDATE invest.PortfolioGrants
            SET PermissionsJson = ?, GrantedAt = SYSDATETIMEOFFSET(),
                ExpiresAt = ?, RevokedAt = NULL
            OUTPUT
                inserted.PortfolioGrantId, inserted.AccountId,
                inserted.RecipientUserId, inserted.PermissionsJson,
                inserted.GrantedAt, inserted.ExpiresAt, inserted.RowVersion
            WHERE AccountId = ? AND OwnerUserId = ? AND RecipientUserId = ?;
            """,
            (permissions_json, expires, normalized_account_id, user_id, normalized_recipient_id),
        )
        rows = _rows_from_cursor(cursor)
        if not rows:
            cursor.execute(
                """
                INSERT INTO invest.PortfolioGrants
                    (AccountId, OwnerUserId, RecipientUserId, PermissionsJson, ExpiresAt)
                OUTPUT
                    inserted.PortfolioGrantId, inserted.AccountId,
                    inserted.RecipientUserId, inserted.PermissionsJson,
                    inserted.GrantedAt, inserted.ExpiresAt, inserted.RowVersion
                VALUES (?, ?, ?, ?, ?);
                """,
                (normalized_account_id, user_id, normalized_recipient_id, permissions_json, expires),
            )
            rows = _rows_from_cursor(cursor)
        result = rows[0]
        _write_audit(cursor, user_id, "SHARE_PORTFOLIO", "PortfolioGrant", str(result["PortfolioGrantId"]), payload)
        return result

    return _run_idempotent_write(user_id, "share_portfolio", idempotency_key, payload, operation)


@mcp.tool(annotations=DESTRUCTIVE_WRITE_TOOL)
def revoke_portfolio_access(
    account_id: str,
    recipient_user_id: str,
    idempotency_key: str,
    ctx: Context,
) -> dict[str, Any]:
    """Revoke a portfolio grant owned by the authenticated caller."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    normalized_recipient_id = _canonical_uuid(recipient_user_id, "recipient_user_id")
    payload = {"account_id": normalized_account_id, "recipient_user_id": normalized_recipient_id}

    def operation(cursor: pyodbc.Cursor) -> dict[str, Any]:
        _require_owned_account(cursor, user_id, normalized_account_id)
        cursor.execute(
            """
            UPDATE invest.PortfolioGrants
            SET RevokedAt = COALESCE(RevokedAt, SYSDATETIMEOFFSET())
            OUTPUT
                inserted.PortfolioGrantId, inserted.AccountId,
                inserted.RecipientUserId, inserted.RevokedAt, inserted.RowVersion
            WHERE AccountId = ? AND OwnerUserId = ? AND RecipientUserId = ?;
            """,
            (normalized_account_id, user_id, normalized_recipient_id),
        )
        rows = _rows_from_cursor(cursor)
        if not rows:
            raise ValueError("Portfolio access not found.")
        result = rows[0]
        _write_audit(cursor, user_id, "REVOKE_PORTFOLIO_ACCESS", "PortfolioGrant", str(result["PortfolioGrantId"]))
        return result

    return _run_idempotent_write(user_id, "revoke_portfolio_access", idempotency_key, payload, operation)


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

invest.Users and invest.Accounts:
  Authenticated user identities and caller-owned investment accounts.
  Authenticated callers can create their own accounts through an idempotent
  write tool; callers cannot choose or impersonate an owner user ID.

invest.ApiTokens:
  Hashed, expiring, independently revocable bearer credentials. Plaintext
  tokens are never stored, and the MCP runtime validates them through a
  least-privilege stored procedure.

invest.Transactions, invest.OpenOrders, invest.CashBalances:
  Append-oriented trade history, soft-cancelled order records, and account cash.
  Account ownership is enforced with (AccountId, UserId) foreign keys.
  Open orders include duration/time-in-force and an optional expiration date.
  Opening-position imports preserve quantity and cost basis as transactions and
  intentionally do not modify cash balances.

invest.StrategyRules and invest.PortfolioGrants:
  Caller-owned strategy configuration and explicit portfolio sharing.

invest.IdempotencyKeys, invest.OrderStatusEvents, invest.AuditLog:
  Duplicate-write prevention, append-only order status history, and audit events.
"""


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        _run_streamable_http()
    else:
        mcp.run(transport=transport)
