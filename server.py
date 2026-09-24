from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import hashlib
import ipaddress
import json
import logging
import struct
import threading
from copy import deepcopy
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import UUID

import pyodbc
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
import uvicorn

from scoring import (
    classify_instrument as classify_scoring_instrument,
    compute_portfolio_fit,
    compute_price_features,
    review_tier as scoring_review_tier,
    score_candidate,
)


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
            "mcp.wiselinetrade.com",
        ],
        allowed_origins=[
            "https://investments-mcp.torusystems.com",
            "https://mcp.wiselinetrade.com",
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

SCHWAB_TDACONFIG_ID = int(os.getenv("SCHWAB_TDACONFIG_ID", "1"))
SCHWAB_TOKEN_URL = os.getenv(
    "SCHWAB_TOKEN_URL",
    "https://api.schwabapi.com/v1/oauth/token",
).strip()
SCHWAB_ACCESS_TOKEN_TTL_SECONDS = int(
    os.getenv("SCHWAB_ACCESS_TOKEN_TTL_SECONDS", "1800")
)
SCHWAB_ACCESS_TOKEN_REFRESH_BUFFER_SECONDS = int(
    os.getenv("SCHWAB_ACCESS_TOKEN_REFRESH_BUFFER_SECONDS", "300")
)
SCHWAB_HTTP_TIMEOUT_SECONDS = int(os.getenv("SCHWAB_HTTP_TIMEOUT_SECONDS", "20"))
SCHWAB_MARKET_DATA_BASE_URL = "https://api.schwabapi.com/marketdata/v1"
SCHWAB_ALLOWED_API_HOST = "api.schwabapi.com"

MCP_USER_RATE_PER_MINUTE = int(os.getenv("MCP_USER_RATE_PER_MINUTE", "60"))
MCP_USER_BURST = int(os.getenv("MCP_USER_BURST", "15"))
MCP_TOKEN_RATE_PER_MINUTE = int(
    os.getenv("MCP_TOKEN_RATE_PER_MINUTE", "30")
)
MCP_TOKEN_BURST = int(os.getenv("MCP_TOKEN_BURST", "10"))
MCP_TOKEN_MAX_CONCURRENT_REQUESTS = int(
    os.getenv("MCP_TOKEN_MAX_CONCURRENT_REQUESTS", "2")
)
MCP_USER_MAX_CONCURRENT_REQUESTS = int(
    os.getenv("MCP_USER_MAX_CONCURRENT_REQUESTS", "4")
)
MCP_TOKEN_USAGE_LOG_ENABLED = os.getenv(
    "MCP_TOKEN_USAGE_LOG_ENABLED",
    "true",
).lower() in {"1", "true", "yes", "on"}
MCP_TOKEN_USAGE_IP_MODE = os.getenv(
    "MCP_TOKEN_USAGE_IP_MODE",
    "prefix",
).strip().lower()
if MCP_TOKEN_USAGE_IP_MODE not in {"none", "prefix", "full"}:
    raise RuntimeError("MCP_TOKEN_USAGE_IP_MODE must be none, prefix, or full.")
MCP_TOKEN_USAGE_MAX_CAPTURE_BYTES = max(
    0,
    min(
        int(os.getenv("MCP_TOKEN_USAGE_MAX_CAPTURE_BYTES", "131072")),
        1_048_576,
    ),
)
SCHWAB_USER_UNITS_PER_MINUTE = int(
    os.getenv("SCHWAB_USER_UNITS_PER_MINUTE", "6")
)
SCHWAB_USER_REQUEST_BURST = int(os.getenv("SCHWAB_USER_REQUEST_BURST", "3"))
SCHWAB_USER_DAILY_UNITS = int(os.getenv("SCHWAB_USER_DAILY_UNITS", "100"))
SCHWAB_HISTORY_CALLS_PER_MINUTE = int(
    os.getenv("SCHWAB_HISTORY_CALLS_PER_MINUTE", "2")
)
SCHWAB_HISTORY_BURST = int(os.getenv("SCHWAB_HISTORY_BURST", "2"))
SCHWAB_HISTORY_DAILY_CALLS = int(os.getenv("SCHWAB_HISTORY_DAILY_CALLS", "20"))
SCHWAB_GLOBAL_REQUESTS_PER_MINUTE = int(
    os.getenv("SCHWAB_GLOBAL_REQUESTS_PER_MINUTE", "60")
)
SCHWAB_GLOBAL_BURST = int(os.getenv("SCHWAB_GLOBAL_BURST", "10"))
SCHWAB_MAX_CONCURRENT_REQUESTS = int(
    os.getenv("SCHWAB_MAX_CONCURRENT_REQUESTS", "5")
)
SCHWAB_MAX_QUOTE_SYMBOLS = int(os.getenv("SCHWAB_MAX_QUOTE_SYMBOLS", "200"))

SCORING_MODEL_VERSION = os.getenv("MCP_SCORING_MODEL_VERSION", "1.1").strip()
if SCORING_MODEL_VERSION != "1.1":
    raise RuntimeError(
        "This server build implements scoring model 1.1; set "
        "MCP_SCORING_MODEL_VERSION=1.1."
    )
SCORING_BENCHMARK = _cleaned_scoring_benchmark = os.getenv(
    "MCP_SCORING_BENCHMARK",
    "VOO",
).strip().upper()
if not SYMBOL_RE.match(_cleaned_scoring_benchmark):
    raise RuntimeError("MCP_SCORING_BENCHMARK is not a valid symbol.")
SCORING_MIN_PEER_COUNT = max(
    2,
    int(os.getenv("MCP_SCORING_MIN_PEER_COUNT", "5")),
)
SCORING_MAX_CANDIDATES = max(
    1,
    min(int(os.getenv("MCP_SCORING_MAX_CANDIDATES", "10")), 50),
)
SCORING_DEFAULT_TARGET_WEIGHT_PERCENT = float(
    os.getenv("MCP_SCORING_DEFAULT_TARGET_WEIGHT_PERCENT", "2")
)
SCORING_PERSIST_SNAPSHOTS = os.getenv(
    "MCP_SCORING_PERSIST_SNAPSHOTS",
    "true",
).lower() in {"1", "true", "yes", "on"}

_SCHWAB_TOKEN_REFRESH_LOCK = threading.Lock()
_SCHWAB_RESPONSE_CACHE_LOCK = threading.Lock()
_SCHWAB_RESPONSE_CACHE: dict[str, tuple[float, Any]] = {}

READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
OPEN_WORLD_READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
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


@dataclass(frozen=True)
class AuthIdentity:
    authentication_subject: str
    user_id: str | None
    api_token_id: str | None
    token_key: str


@dataclass
class RequestUsageMetrics:
    schwab_units: int = 0
    schwab_upstream_requests: int = 0
    schwab_cache_hits: int = 0
    rate_limit_scope: str | None = None


_REQUEST_USAGE_METRICS: ContextVar[RequestUsageMetrics | None] = ContextVar(
    "investment_mcp_request_usage",
    default=None,
)


class _KeyedConcurrencyLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}

    def try_acquire(self, key: str) -> bool:
        with self._lock:
            active = self._active.get(key, 0)
            if active >= self.limit:
                return False
            self._active[key] = active + 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            active = self._active.get(key, 0)
            if active <= 1:
                self._active.pop(key, None)
            else:
                self._active[key] = active - 1

    def active(self, key: str) -> int:
        with self._lock:
            return self._active.get(key, 0)


_TOKEN_CONCURRENCY_LIMITER = _KeyedConcurrencyLimiter(
    MCP_TOKEN_MAX_CONCURRENT_REQUESTS
)
_USER_CONCURRENCY_LIMITER = _KeyedConcurrencyLimiter(
    MCP_USER_MAX_CONCURRENT_REQUESTS
)


class BearerAuthASGI:
    def __init__(
        self,
        app: Any,
        token_resolver: Callable[[str], AuthIdentity | str | None],
        protected_path: str,
        *,
        token_concurrency_limiter: _KeyedConcurrencyLimiter | None = None,
        user_concurrency_limiter: _KeyedConcurrencyLimiter | None = None,
    ) -> None:
        self.app = app
        self.token_resolver = token_resolver
        self.protected_path = protected_path.rstrip("/") or "/"
        self.token_concurrency_limiter = (
            token_concurrency_limiter or _TOKEN_CONCURRENCY_LIMITER
        )
        self.user_concurrency_limiter = (
            user_concurrency_limiter or _USER_CONCURRENCY_LIMITER
        )

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
                resolved_identity = await asyncio.to_thread(
                    self.token_resolver,
                    token,
                )
            except Exception:
                LOGGER.exception("Bearer token validation failed unexpectedly.")
                await self._service_unavailable(send)
                return

            if not resolved_identity:
                await self._unauthorized(send)
                return

            if isinstance(resolved_identity, AuthIdentity):
                identity = resolved_identity
            else:
                authentication_subject = str(resolved_identity).strip()
                if not authentication_subject:
                    await self._unauthorized(send)
                    return
                identity = AuthIdentity(
                    authentication_subject=authentication_subject,
                    user_id=None,
                    api_token_id=None,
                    token_key=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                )

            scope = dict(scope)
            state = dict(scope.get("state") or {})
            state["authentication_subject"] = identity.authentication_subject
            if identity.user_id:
                state["user_id"] = identity.user_id
            if identity.api_token_id:
                state["api_token_id"] = identity.api_token_id
            # This is an API-token UUID for database tokens and a SHA-256
            # digest for temporary legacy tokens. The plaintext bearer token
            # is never copied into request state or logs.
            state["token_rate_key"] = identity.token_key
            scope["state"] = state

            # Streamable HTTP uses POST for JSON-RPC requests. Long-lived GET
            # streams authenticate normally but do not occupy concurrency slots.
            if str(scope.get("method") or "").upper() == "POST":
                user_key = identity.user_id or identity.authentication_subject
                if not self.token_concurrency_limiter.try_acquire(
                    identity.token_key
                ):
                    LOGGER.warning(
                        "Token concurrency limit exceeded: api_token_id=%s "
                        "user_id=%s limit=%s",
                        identity.api_token_id or "legacy",
                        identity.user_id or "legacy",
                        self.token_concurrency_limiter.limit,
                    )
                    await self._too_many_requests(
                        send,
                        scope_name="token_concurrency",
                        limit=self.token_concurrency_limiter.limit,
                    )
                    return

                if not self.user_concurrency_limiter.try_acquire(user_key):
                    self.token_concurrency_limiter.release(identity.token_key)
                    LOGGER.warning(
                        "User concurrency limit exceeded: user_id=%s limit=%s",
                        identity.user_id or identity.authentication_subject,
                        self.user_concurrency_limiter.limit,
                    )
                    await self._too_many_requests(
                        send,
                        scope_name="user_concurrency",
                        limit=self.user_concurrency_limiter.limit,
                    )
                    return

                await self._run_observed_request(
                    scope,
                    receive,
                    send,
                    identity=identity,
                    user_key=user_key,
                )
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

    async def _run_observed_request(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        *,
        identity: AuthIdentity,
        user_key: str,
    ) -> None:
        started_at = datetime.now(timezone.utc)
        started_monotonic = monotonic()
        captured_body = bytearray()
        captured_response = bytearray()
        request_bytes = 0
        response_bytes = 0
        response_status = 500
        error_type: str | None = None
        metrics = RequestUsageMetrics()
        metrics_token = _REQUEST_USAGE_METRICS.set(metrics)

        async def observed_receive() -> dict[str, Any]:
            nonlocal request_bytes
            message = await receive()
            if message.get("type") == "http.request":
                body = bytes(message.get("body") or b"")
                request_bytes += len(body)
                remaining = max(
                    0,
                    MCP_TOKEN_USAGE_MAX_CAPTURE_BYTES - len(captured_body),
                )
                if remaining:
                    captured_body.extend(body[:remaining])
            return message

        async def observed_send(message: dict[str, Any]) -> None:
            nonlocal response_bytes, response_status
            if message.get("type") == "http.response.start":
                response_status = int(message.get("status") or 500)
            elif message.get("type") == "http.response.body":
                body = bytes(message.get("body") or b"")
                response_bytes += len(body)
                remaining = max(
                    0,
                    MCP_TOKEN_USAGE_MAX_CAPTURE_BYTES - len(captured_response),
                )
                if remaining:
                    captured_response.extend(body[:remaining])
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            error_type = "CancelledError"
            raise
        except Exception as exc:
            error_type = type(exc).__name__[:100]
            raise
        finally:
            completed_at = datetime.now(timezone.utc)
            duration_ms = max(
                0,
                int((monotonic() - started_monotonic) * 1000),
            )
            self.user_concurrency_limiter.release(user_key)
            self.token_concurrency_limiter.release(identity.token_key)
            _REQUEST_USAGE_METRICS.reset(metrics_token)

            if (
                MCP_TOKEN_USAGE_LOG_ENABLED
                and identity.api_token_id
                and identity.user_id
            ):
                try:
                    await asyncio.to_thread(
                        _record_api_token_usage,
                        identity=identity,
                        scope=scope,
                        captured_body=bytes(captured_body),
                        captured_response=bytes(captured_response),
                        request_bytes=request_bytes,
                        response_bytes=response_bytes,
                        response_status=response_status,
                        started_at=started_at,
                        completed_at=completed_at,
                        duration_ms=duration_ms,
                        error_type=error_type,
                        metrics=metrics,
                    )
                except Exception:
                    # Telemetry must never change the result of an MCP request.
                    LOGGER.exception(
                        "Failed to record API token usage: api_token_id=%s "
                        "user_id=%s",
                        identity.api_token_id,
                        identity.user_id,
                    )

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

    async def _too_many_requests(
        self,
        send: Any,
        *,
        scope_name: str,
        limit: int,
    ) -> None:
        payload = json.dumps(
            {
                "error": "concurrency_limit_exceeded",
                "message": _limit_message(scope_name, 1),
                "scope": scope_name,
                "retry_after_seconds": 1,
                "limit": limit,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", b"1"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


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


def _resolve_database_token(token: str) -> AuthIdentity | None:
    """Resolve a bearer token through the least-privilege SQL procedure."""
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    row = _fetch_one(
        "EXEC invest.AuthenticateApiToken @TokenHash = ?;",
        (token_hash,),
    )
    if not row:
        return None
    subject = str(row.get("AuthenticationSubject") or "").strip()
    if not subject:
        return None
    api_token_id = str(row.get("ApiTokenId") or "").strip() or None
    user_id = str(row.get("UserId") or "").strip() or None
    return AuthIdentity(
        authentication_subject=subject,
        user_id=user_id,
        api_token_id=api_token_id,
        token_key=api_token_id or token_hash.hex(),
    )


def _build_token_resolver() -> Callable[[str], AuthIdentity | None]:
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

    def resolve(token: str) -> AuthIdentity | None:
        if mode in {"database", "hybrid"}:
            try:
                identity = _resolve_database_token(token)
            except pyodbc.Error:
                if mode == "database":
                    raise
                LOGGER.exception(
                    "Database token validation failed; trying temporary legacy fallback."
                )
            else:
                if identity:
                    return identity

        if mode in {"legacy", "hybrid"}:
            subject = _resolve_legacy_token(token, legacy_tokens)
            if subject:
                return AuthIdentity(
                    authentication_subject=subject,
                    user_id=None,
                    api_token_id=None,
                    token_key=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                )
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


def _scope_header(scope: dict[str, Any], name: str) -> str:
    wanted = name.lower().encode("latin1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return value.decode("latin1", errors="replace").strip()
    return ""


def _limited_text(value: Any, length: int) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned[:length] if cleaned else None


def _parse_mcp_request_metadata(body: bytes) -> tuple[str | None, str | None]:
    if not body or len(body) >= MCP_TOKEN_USAGE_MAX_CAPTURE_BYTES:
        return None, None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return "batch" if isinstance(payload, list) else None, None
    rpc_method = _limited_text(payload.get("method"), 100)
    params = payload.get("params")
    tool_name = None
    if rpc_method == "tools/call" and isinstance(params, dict):
        tool_name = _limited_text(params.get("name"), 200)
    return rpc_method, tool_name


def _parse_mcp_response_error(body: bytes) -> str | None:
    if not body or len(body) >= MCP_TOKEN_USAGE_MAX_CAPTURE_BYTES:
        return None
    try:
        decoded = body.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if decoded.startswith("data:"):
        decoded = "\n".join(
            line[5:].lstrip()
            for line in decoded.splitlines()
            if line.startswith("data:")
        )
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("error"), dict):
        return "JsonRpcError"
    result = payload.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        return "ToolError"
    return None


def _client_network_metadata(
    scope: dict[str, Any],
) -> tuple[str | None, str | None]:
    raw_ip = _scope_header(scope, "cf-connecting-ip")
    if not raw_ip:
        client = scope.get("client")
        if isinstance(client, (tuple, list)) and client:
            raw_ip = str(client[0])
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError:
        return None, None

    prefix_length = 24 if address.version == 4 else 64
    network = str(
        ipaddress.ip_network(f"{address}/{prefix_length}", strict=False)
    )
    stored_ip = str(address) if MCP_TOKEN_USAGE_IP_MODE == "full" else None
    stored_network = network if MCP_TOKEN_USAGE_IP_MODE != "none" else None
    return stored_ip, stored_network


def _record_api_token_usage(
    *,
    identity: AuthIdentity,
    scope: dict[str, Any],
    captured_body: bytes,
    captured_response: bytes,
    request_bytes: int,
    response_bytes: int,
    response_status: int,
    started_at: datetime,
    completed_at: datetime,
    duration_ms: int,
    error_type: str | None,
    metrics: RequestUsageMetrics,
) -> None:
    if not identity.api_token_id or not identity.user_id:
        return

    rpc_method, tool_name = _parse_mcp_request_metadata(captured_body)
    mcp_error_type = _parse_mcp_response_error(captured_response)
    client_ip, client_network = _client_network_metadata(scope)
    client_country = _limited_text(_scope_header(scope, "cf-ipcountry"), 8)
    user_agent = _limited_text(_scope_header(scope, "user-agent"), 512)
    cf_ray_id = _limited_text(_scope_header(scope, "cf-ray"), 100)
    session_id = _scope_header(scope, "mcp-session-id")
    session_hash = (
        hashlib.sha256(session_id.encode("utf-8")).digest()
        if session_id
        else None
    )
    fingerprint_source = "|".join(
        value
        for value in (client_ip or client_network, client_country, user_agent)
        if value
    )
    fingerprint_hash = (
        hashlib.sha256(fingerprint_source.encode("utf-8")).digest()
        if fingerprint_source
        else None
    )

    was_rate_limited = bool(metrics.rate_limit_scope or response_status == 429)
    if was_rate_limited:
        outcome = "RateLimited"
    elif error_type == "CancelledError":
        outcome = "Cancelled"
        if response_status == 500:
            response_status = 499
    elif error_type or response_status >= 500:
        outcome = "ServerError"
    elif mcp_error_type:
        outcome = "ToolError"
    elif response_status >= 400:
        outcome = "ClientError"
    else:
        outcome = "Success"

    _fetch_all(
        """
        EXEC invest.RecordApiTokenUsage
            @ApiTokenId = ?,
            @UserId = ?,
            @RequestStartedAt = ?,
            @RequestCompletedAt = ?,
            @DurationMs = ?,
            @HttpMethod = ?,
            @RequestPath = ?,
            @HostName = ?,
            @RpcMethod = ?,
            @ToolName = ?,
            @HttpStatus = ?,
            @Outcome = ?,
            @ErrorType = ?,
            @WasRateLimited = ?,
            @RateLimitScope = ?,
            @RequestBytes = ?,
            @ResponseBytes = ?,
            @SchwabUnits = ?,
            @SchwabUpstreamRequests = ?,
            @SchwabCacheHits = ?,
            @ClientIpAddress = ?,
            @ClientNetwork = ?,
            @ClientCountry = ?,
            @ClientFingerprintHash = ?,
            @UserAgent = ?,
            @CfRayId = ?,
            @McpSessionIdHash = ?;
        """,
        (
            identity.api_token_id,
            identity.user_id,
            started_at,
            completed_at,
            duration_ms,
            _limited_text(scope.get("method"), 10) or "POST",
            _limited_text(scope.get("path"), 512) or "/mcp",
            _limited_text(_scope_header(scope, "host"), 255),
            rpc_method,
            tool_name,
            max(100, min(int(response_status), 599)),
            outcome,
            _limited_text(error_type or mcp_error_type, 100),
            was_rate_limited,
            _limited_text(metrics.rate_limit_scope, 100),
            max(0, int(request_bytes)),
            max(0, int(response_bytes)),
            max(0, int(metrics.schwab_units)),
            max(0, int(metrics.schwab_upstream_requests)),
            max(0, int(metrics.schwab_cache_hits)),
            client_ip,
            client_network,
            client_country,
            fingerprint_hash,
            user_agent,
            cf_ray_id,
            session_hash,
        ),
    )


class SchwabApiError(RuntimeError):
    """A safe, token-free description of a Schwab API failure."""


def _limit_message(scope: str, retry_after_seconds: int) -> str:
    """Return a safe explanation suitable for an MCP client to show a user."""
    descriptions = {
        "mcp_token_minute": (
            "This API token has reached its per-minute request limit."
        ),
        "mcp_user_minute": (
            "Your account has reached its combined per-minute request limit."
        ),
        "token_concurrency": (
            "This API token has too many requests running at the same time."
        ),
        "user_concurrency": (
            "Your account has too many requests running at the same time."
        ),
        "schwab_user_units_minute": (
            "Your Schwab market-data unit limit has been reached."
        ),
        "schwab_user_burst": (
            "Your Schwab market-data burst limit has been reached."
        ),
        "schwab_user_daily": (
            "Your daily Schwab market-data allowance has been reached."
        ),
        "schwab_history_user_minute": (
            "Your Schwab price-history request limit has been reached."
        ),
        "schwab_history_user_daily": (
            "Your daily Schwab price-history allowance has been reached."
        ),
        "schwab_global_minute": (
            "The shared Schwab market-data service is temporarily busy."
        ),
        "schwab_global_concurrency": (
            "The shared Schwab market-data service is temporarily busy."
        ),
    }
    retry_after = max(1, int(retry_after_seconds))
    seconds_label = "second" if retry_after == 1 else "seconds"
    description = descriptions.get(
        scope,
        "The requested operation has reached a usage limit.",
    )
    return f"{description} Retry in {retry_after} {seconds_label}."


class RateLimitExceeded(RuntimeError):
    """Structured, token-free MCP error returned when a quota is exhausted."""

    def __init__(
        self,
        scope: str,
        retry_after_seconds: int,
        *,
        limit: int,
        unit: str,
    ) -> None:
        self.scope = scope
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        self.limit = limit
        self.unit = unit
        details = {
            "error": "rate_limit_exceeded",
            "message": _limit_message(scope, self.retry_after_seconds),
            "scope": scope,
            "retry_after_seconds": self.retry_after_seconds,
            "limit": limit,
            "unit": unit,
        }
        super().__init__(json.dumps(details, separators=(",", ":")))


class _TokenBucketLimiter:
    def __init__(self, rate_per_minute: int, capacity: int) -> None:
        self.rate_per_minute = max(1, int(rate_per_minute))
        self.capacity = max(1, int(capacity))
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def consume(self, key: str, amount: int = 1) -> int | None:
        requested = max(1, int(amount))
        if requested > self.capacity:
            return 60
        now = monotonic()
        refill_per_second = self.rate_per_minute / 60.0
        with self._lock:
            tokens, last_refill = self._buckets.get(
                key,
                (float(self.capacity), now),
            )
            tokens = min(
                float(self.capacity),
                tokens + max(0.0, now - last_refill) * refill_per_second,
            )
            if tokens >= requested:
                self._buckets[key] = (tokens - requested, now)
                return None
            self._buckets[key] = (tokens, now)
            missing = requested - tokens
            return max(1, int((missing / refill_per_second) + 0.999))

    def refund(self, key: str, amount: int = 1) -> None:
        now = monotonic()
        with self._lock:
            tokens, last_refill = self._buckets.get(key, (0.0, now))
            refill = max(0.0, now - last_refill) * self.rate_per_minute / 60.0
            self._buckets[key] = (
                min(float(self.capacity), tokens + refill + max(1, int(amount))),
                now,
            )

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


class _UtcDailyCounter:
    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._usage: dict[tuple[str, date], int] = {}

    def consume(self, key: str, amount: int = 1) -> int | None:
        requested = max(1, int(amount))
        now = datetime.now(timezone.utc)
        today = now.date()
        counter_key = (key, today)
        with self._lock:
            used = self._usage.get(counter_key, 0)
            if used + requested <= self.limit:
                self._usage[counter_key] = used + requested
                if len(self._usage) > 10_000:
                    self._usage = {
                        item_key: value
                        for item_key, value in self._usage.items()
                        if item_key[1] >= today - timedelta(days=1)
                    }
                return None
        tomorrow = datetime.combine(
            today + timedelta(days=1),
            datetime_time.min,
            tzinfo=timezone.utc,
        )
        return max(1, int((tomorrow - now).total_seconds() + 0.999))

    def refund(self, key: str, amount: int = 1) -> None:
        counter_key = (key, datetime.now(timezone.utc).date())
        with self._lock:
            used = self._usage.get(counter_key, 0)
            self._usage[counter_key] = max(0, used - max(1, int(amount)))

    def clear(self) -> None:
        with self._lock:
            self._usage.clear()


_MCP_TOKEN_LIMITER = _TokenBucketLimiter(
    MCP_TOKEN_RATE_PER_MINUTE,
    MCP_TOKEN_BURST,
)
_MCP_USER_LIMITER = _TokenBucketLimiter(MCP_USER_RATE_PER_MINUTE, MCP_USER_BURST)
_SCHWAB_USER_UNIT_LIMITER = _TokenBucketLimiter(
    SCHWAB_USER_UNITS_PER_MINUTE,
    SCHWAB_USER_UNITS_PER_MINUTE,
)
_SCHWAB_USER_REQUEST_LIMITER = _TokenBucketLimiter(
    SCHWAB_USER_UNITS_PER_MINUTE,
    SCHWAB_USER_REQUEST_BURST,
)
_SCHWAB_USER_DAILY_COUNTER = _UtcDailyCounter(SCHWAB_USER_DAILY_UNITS)
_SCHWAB_HISTORY_LIMITER = _TokenBucketLimiter(
    SCHWAB_HISTORY_CALLS_PER_MINUTE,
    SCHWAB_HISTORY_BURST,
)
_SCHWAB_HISTORY_DAILY_COUNTER = _UtcDailyCounter(SCHWAB_HISTORY_DAILY_CALLS)
_SCHWAB_GLOBAL_LIMITER = _TokenBucketLimiter(
    SCHWAB_GLOBAL_REQUESTS_PER_MINUTE,
    SCHWAB_GLOBAL_BURST,
)
_SCHWAB_CONCURRENCY = threading.BoundedSemaphore(
    max(1, SCHWAB_MAX_CONCURRENT_REQUESTS)
)


def _raise_rate_limit(
    scope: str,
    retry_after_seconds: int,
    *,
    limit: int,
    unit: str,
    user_id: str | None = None,
) -> None:
    request_metrics = _REQUEST_USAGE_METRICS.get()
    if request_metrics is not None:
        request_metrics.rate_limit_scope = scope
    LOGGER.warning(
        "Rate limit exceeded: scope=%s user_id=%s retry_after_seconds=%s "
        "limit=%s unit=%s",
        scope,
        user_id or "shared",
        retry_after_seconds,
        limit,
        unit,
    )
    raise RateLimitExceeded(
        scope,
        retry_after_seconds,
        limit=limit,
        unit=unit,
    )


def _enforce_general_mcp_limit(
    user_id: str,
    token_key: str | None = None,
) -> None:
    if token_key:
        retry_after = _MCP_TOKEN_LIMITER.consume(token_key)
        if retry_after is not None:
            _raise_rate_limit(
                "mcp_token_minute",
                retry_after,
                limit=MCP_TOKEN_RATE_PER_MINUTE,
                unit="calls_per_minute",
                user_id=user_id,
            )

    retry_after = _MCP_USER_LIMITER.consume(user_id)
    if retry_after is not None:
        if token_key:
            _MCP_TOKEN_LIMITER.refund(token_key)
        _raise_rate_limit(
            "mcp_user_minute",
            retry_after,
            limit=MCP_USER_RATE_PER_MINUTE,
            unit="calls_per_minute",
            user_id=user_id,
        )


def _reserve_schwab_user_capacity(
    user_id: str,
    units: int,
    *,
    is_history: bool,
) -> None:
    reservations: list[tuple[Any, str, int]] = []

    def reserve(
        limiter: _TokenBucketLimiter | _UtcDailyCounter,
        key: str,
        amount: int,
        scope: str,
        limit: int,
        unit: str,
    ) -> None:
        retry_after = limiter.consume(key, amount)
        if retry_after is not None:
            for reserved_limiter, reserved_key, reserved_amount in reversed(
                reservations
            ):
                reserved_limiter.refund(reserved_key, reserved_amount)
            _raise_rate_limit(
                scope,
                retry_after,
                limit=limit,
                unit=unit,
                user_id=user_id,
            )
        reservations.append((limiter, key, amount))

    reserve(
        _SCHWAB_USER_UNIT_LIMITER,
        user_id,
        units,
        "schwab_user_units_minute",
        SCHWAB_USER_UNITS_PER_MINUTE,
        "units_per_minute",
    )
    reserve(
        _SCHWAB_USER_REQUEST_LIMITER,
        user_id,
        1,
        "schwab_user_burst",
        SCHWAB_USER_REQUEST_BURST,
        "requests_burst",
    )
    reserve(
        _SCHWAB_USER_DAILY_COUNTER,
        user_id,
        units,
        "schwab_user_daily",
        SCHWAB_USER_DAILY_UNITS,
        "units_per_utc_day",
    )
    if is_history:
        reserve(
            _SCHWAB_HISTORY_LIMITER,
            user_id,
            1,
            "schwab_history_user_minute",
            SCHWAB_HISTORY_CALLS_PER_MINUTE,
            "calls_per_minute",
        )
        reserve(
            _SCHWAB_HISTORY_DAILY_COUNTER,
            user_id,
            1,
            "schwab_history_user_daily",
            SCHWAB_HISTORY_DAILY_CALLS,
            "calls_per_utc_day",
        )


@contextmanager
def _schwab_global_request_slot():
    retry_after = _SCHWAB_GLOBAL_LIMITER.consume("global")
    if retry_after is not None:
        _raise_rate_limit(
            "schwab_global_minute",
            retry_after,
            limit=SCHWAB_GLOBAL_REQUESTS_PER_MINUTE,
            unit="requests_per_minute",
        )

    acquired = _SCHWAB_CONCURRENCY.acquire(timeout=1)
    if not acquired:
        _SCHWAB_GLOBAL_LIMITER.refund("global")
        _raise_rate_limit(
            "schwab_global_concurrency",
            1,
            limit=SCHWAB_MAX_CONCURRENT_REQUESTS,
            unit="concurrent_requests",
        )
    try:
        yield
    finally:
        _SCHWAB_CONCURRENCY.release()


def _as_utc_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SchwabApiError(
                "The Schwab token update timestamp in SQL Server is invalid."
            ) from exc
    else:
        raise SchwabApiError(
            "The Schwab token update timestamp has an unsupported type."
        )

    # dbo.TDAconfig.AccessTokenUpdateTime is a legacy datetime column. The
    # runtime procedures write UTC, so a timezone-free value is interpreted as
    # UTC rather than as the Windows service account's local time.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _schwab_access_token_is_fresh(
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    access_token = str(config.get("AccessToken") or "").strip()
    updated_at = _as_utc_datetime(config.get("AccessTokenUpdateTime"))
    if not access_token or updated_at is None:
        return False

    if SCHWAB_ACCESS_TOKEN_TTL_SECONDS <= 0:
        raise SchwabApiError("SCHWAB_ACCESS_TOKEN_TTL_SECONDS must be positive.")
    refresh_after = max(
        0,
        SCHWAB_ACCESS_TOKEN_TTL_SECONDS
        - max(0, SCHWAB_ACCESS_TOKEN_REFRESH_BUFFER_SECONDS),
    )
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = (current.astimezone(timezone.utc) - updated_at).total_seconds()
    return age_seconds < refresh_after


def _get_schwab_oauth_config() -> dict[str, Any]:
    if SCHWAB_TDACONFIG_ID <= 0:
        raise SchwabApiError("SCHWAB_TDACONFIG_ID must be a positive integer.")
    config = _fetch_one(
        "EXEC invest.GetSchwabOAuthConfig @TDAconfigId = ?;",
        (SCHWAB_TDACONFIG_ID,),
    )
    if not config:
        raise SchwabApiError(
            f"Schwab OAuth configuration {SCHWAB_TDACONFIG_ID} was not found."
        )
    return config


def _required_schwab_config_value(config: dict[str, Any], name: str) -> str:
    value = str(config.get(name) or "").strip()
    if not value:
        raise SchwabApiError(f"Schwab OAuth configuration is missing {name}.")
    return value


def _validated_schwab_token_url() -> str:
    # dbo.TDAconfig.URLtoGetCode is the legacy interactive authorization URL
    # used to obtain the initial code/refresh token. It is intentionally not
    # used for background access-token refreshes.
    token_url = SCHWAB_TOKEN_URL
    parsed = urlparse(token_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SchwabApiError("The Schwab token URL has an invalid port.") from exc
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != SCHWAB_ALLOWED_API_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != "/v1/oauth/token"
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise SchwabApiError(
            "The Schwab token URL must be exactly the HTTPS Schwab OAuth "
            "token endpoint."
        )
    return token_url


def _request_new_schwab_access_token(
    config: dict[str, Any],
) -> tuple[str, int | None]:
    client_id = _required_schwab_config_value(config, "client_id")
    client_secret = _required_schwab_config_value(config, "client_secret")
    refresh_token = _required_schwab_config_value(config, "RefreshToken")
    token_url = _validated_schwab_token_url()

    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")
    request = Request(
        token_url,
        data=urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "investment-mcp/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=SCHWAB_HTTP_TIMEOUT_SECONDS) as response:
            payload_bytes = response.read(1_048_577)
    except HTTPError as exc:
        raise SchwabApiError(
            f"Schwab OAuth refresh failed with HTTP status {exc.code}."
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise SchwabApiError("Schwab OAuth refresh could not reach Schwab.") from exc

    if len(payload_bytes) > 1_048_576:
        raise SchwabApiError("Schwab OAuth returned an unexpectedly large response.")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchwabApiError("Schwab OAuth returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise SchwabApiError("Schwab OAuth returned an unexpected response.")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise SchwabApiError("Schwab OAuth response did not contain an access token.")

    expires_in: int | None = None
    if payload.get("expires_in") is not None:
        try:
            expires_in = int(payload["expires_in"])
        except (TypeError, ValueError) as exc:
            raise SchwabApiError(
                "Schwab OAuth returned an invalid token lifetime."
            ) from exc
        if expires_in <= 0:
            raise SchwabApiError("Schwab OAuth returned an expired access token.")

    return access_token, expires_in


def _save_schwab_access_token(access_token: str) -> None:
    result = _fetch_one(
        (
            "EXEC invest.UpdateSchwabAccessToken "
            "@TDAconfigId = ?, @AccessToken = ?;"
        ),
        (SCHWAB_TDACONFIG_ID, access_token),
    )
    if not result:
        raise SchwabApiError("SQL Server did not save the Schwab access token.")


def _get_schwab_access_token(*, force_refresh: bool = False) -> str:
    """Return a usable token, refreshing it shortly before expiry when needed."""
    with _SCHWAB_TOKEN_REFRESH_LOCK:
        config = _get_schwab_oauth_config()
        if not force_refresh and _schwab_access_token_is_fresh(config):
            return _required_schwab_config_value(config, "AccessToken")

        access_token, expires_in = _request_new_schwab_access_token(config)
        if expires_in is not None and expires_in < SCHWAB_ACCESS_TOKEN_TTL_SECONDS:
            LOGGER.warning(
                "Schwab reported a shorter access-token lifetime (%s seconds) "
                "than SCHWAB_ACCESS_TOKEN_TTL_SECONDS (%s seconds).",
                expires_in,
                SCHWAB_ACCESS_TOKEN_TTL_SECONDS,
            )
        _save_schwab_access_token(access_token)
        return access_token


def _schwab_market_data_get(
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Call one Schwab market-data endpoint and retry once after a 401."""
    cleaned_path = path.strip()
    path_parts = cleaned_path.replace("\\", "/").split("/")
    if (
        not cleaned_path
        or "://" in cleaned_path
        or "?" in cleaned_path
        or "#" in cleaned_path
        or "\\" in cleaned_path
        or any(part in {".", ".."} for part in path_parts)
    ):
        raise SchwabApiError("Invalid Schwab market-data path.")
    normalized_path = "/" + cleaned_path.lstrip("/")
    url = f"{SCHWAB_MARKET_DATA_BASE_URL}{normalized_path}"
    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"

    for attempt in range(2):
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": (
                    "Bearer "
                    + _get_schwab_access_token(force_refresh=attempt == 1)
                ),
                "User-Agent": "investment-mcp/1.0",
            },
            method="GET",
        )
        try:
            with _schwab_global_request_slot():
                request_metrics = _REQUEST_USAGE_METRICS.get()
                if request_metrics is not None:
                    request_metrics.schwab_upstream_requests += 1
                with urlopen(
                    request,
                    timeout=SCHWAB_HTTP_TIMEOUT_SECONDS,
                ) as response:
                    payload_bytes = response.read(10_485_761)
        except HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                continue
            raise SchwabApiError(
                f"Schwab market data request failed with HTTP status {exc.code}."
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise SchwabApiError(
                "Schwab market data request could not reach Schwab."
            ) from exc

        if len(payload_bytes) > 10_485_760:
            raise SchwabApiError(
                "Schwab market data returned an unexpectedly large response."
            )
        try:
            return json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchwabApiError("Schwab market data returned invalid JSON.") from exc

    raise SchwabApiError("Schwab market data authorization failed.")


def _schwab_cached_market_data_get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    ttl_seconds: int,
    user_id: str,
    units: int = 1,
    is_history: bool = False,
) -> Any:
    cache_key = json.dumps(
        {"path": path, "params": params or {}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    now = monotonic()
    with _SCHWAB_RESPONSE_CACHE_LOCK:
        cached = _SCHWAB_RESPONSE_CACHE.get(cache_key)
        if cached and cached[0] > now:
            request_metrics = _REQUEST_USAGE_METRICS.get()
            if request_metrics is not None:
                request_metrics.schwab_cache_hits += 1
            return deepcopy(cached[1])

    _reserve_schwab_user_capacity(user_id, units, is_history=is_history)
    request_metrics = _REQUEST_USAGE_METRICS.get()
    if request_metrics is not None:
        request_metrics.schwab_units += max(1, int(units))
    payload = _schwab_market_data_get(path, params)
    with _SCHWAB_RESPONSE_CACHE_LOCK:
        if len(_SCHWAB_RESPONSE_CACHE) >= 1000:
            expired_keys = [
                key
                for key, (expires_at, _) in _SCHWAB_RESPONSE_CACHE.items()
                if expires_at <= now
            ]
            for key in expired_keys:
                _SCHWAB_RESPONSE_CACHE.pop(key, None)
            if len(_SCHWAB_RESPONSE_CACHE) >= 1000:
                oldest_key = min(
                    _SCHWAB_RESPONSE_CACHE,
                    key=lambda key: _SCHWAB_RESPONSE_CACHE[key][0],
                )
                _SCHWAB_RESPONSE_CACHE.pop(oldest_key, None)
        _SCHWAB_RESPONSE_CACHE[cache_key] = (
            monotonic() + max(0, ttl_seconds),
            deepcopy(payload),
        )
    return payload


def _epoch_milliseconds_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(
            float(value) / 1000,
            tz=timezone.utc,
        ).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _normalize_schwab_quote(
    requested_symbol: str,
    raw_quote: dict[str, Any],
) -> dict[str, Any]:
    quote = raw_quote.get("quote") or {}
    reference = raw_quote.get("reference") or {}
    fundamental = raw_quote.get("fundamental") or {}
    if not isinstance(quote, dict):
        quote = {}
    if not isinstance(reference, dict):
        reference = {}
    if not isinstance(fundamental, dict):
        fundamental = {}
    symbol = str(raw_quote.get("symbol") or requested_symbol).upper()
    return {
        "Symbol": symbol,
        "Name": reference.get("description"),
        "AssetType": raw_quote.get("assetMainType"),
        "AssetSubType": raw_quote.get("assetSubType"),
        "Exchange": reference.get("exchangeName") or reference.get("exchange"),
        "Realtime": raw_quote.get("realtime"),
        "LastPrice": quote.get("lastPrice"),
        "MarkPrice": quote.get("mark"),
        "BidPrice": quote.get("bidPrice"),
        "AskPrice": quote.get("askPrice"),
        "OpenPrice": quote.get("openPrice"),
        "HighPrice": quote.get("highPrice"),
        "LowPrice": quote.get("lowPrice"),
        "ClosePrice": quote.get("closePrice"),
        "NetChange": quote.get("netChange"),
        "NetPercentChange": quote.get("netPercentChange"),
        "Volume": quote.get("totalVolume"),
        "QuoteTime": _epoch_milliseconds_to_iso(quote.get("quoteTime")),
        "PE": fundamental.get("peRatio"),
        "EPS": fundamental.get("eps"),
        "DividendAmount": fundamental.get("divAmount"),
        "DividendYield": fundamental.get("divYield"),
        "Source": "Schwab",
    }


def _schwab_quote_rows(
    payload: Any,
    requested_symbols: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise SchwabApiError("Schwab quotes returned an unexpected response.")
    rows: list[dict[str, Any]] = []
    for symbol in requested_symbols:
        raw_quote = payload.get(symbol) or payload.get(symbol.upper())
        if isinstance(raw_quote, dict):
            rows.append(_normalize_schwab_quote(symbol, raw_quote))
    return rows


def _normalize_schwab_instrument(raw: dict[str, Any]) -> dict[str, Any]:
    fundamental = raw.get("fundamental") or {}
    if not isinstance(fundamental, dict):
        fundamental = {}
    return {
        "Symbol": raw.get("symbol"),
        "CUSIP": raw.get("cusip"),
        "Name": raw.get("description"),
        "Exchange": raw.get("exchange"),
        "AssetType": raw.get("assetType"),
        "PE": fundamental.get("peRatio"),
        "PEG": fundamental.get("pegRatio"),
        "EPS": fundamental.get("eps"),
        "DividendAmount": fundamental.get("divAmount"),
        "DividendYield": fundamental.get("divYield"),
        "SharesOutstanding": fundamental.get("sharesOutstanding"),
        "ReturnOnEquity": fundamental.get("returnOnEquity"),
        "RevenuePerShareTTM": fundamental.get("revenuePerShareTTM"),
        "NextDividendExDate": fundamental.get("nextDivExDate"),
        "Source": "Schwab",
    }


def _schwab_instrument_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise SchwabApiError("Schwab instruments returned an unexpected response.")
    instruments = payload.get("instruments") or []
    if not isinstance(instruments, list):
        raise SchwabApiError("Schwab instruments returned an unexpected response.")
    return [
        _normalize_schwab_instrument(item)
        for item in instruments
        if isinstance(item, dict)
    ]


def _schwab_history_rows(
    payload: Any,
    symbol: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise SchwabApiError("Schwab price history returned an unexpected response.")
    candles = payload.get("candles") or []
    if not isinstance(candles, list):
        raise SchwabApiError("Schwab price history returned an unexpected response.")
    rows = []
    for candle in candles[-limit:]:
        if not isinstance(candle, dict):
            continue
        timestamp = _epoch_milliseconds_to_iso(candle.get("datetime"))
        rows.append(
            {
                "Symbol": str(payload.get("symbol") or symbol).upper(),
                "Date": timestamp[:10] if timestamp else None,
                "OpenValue": candle.get("open"),
                "HighValue": candle.get("high"),
                "LowValue": candle.get("low"),
                "LastValue": candle.get("close"),
                "Volume": candle.get("volume"),
                "TradeTypeId": None,
                "Created": None,
                "Updated": None,
                "Source": "Schwab",
            }
        )
    return rows


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
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    if request is not None:
        state = request.scope.get("state", {})
        authenticated_user_id = str(state.get("user_id", "")).strip()
        if authenticated_user_id:
            token_rate_key = str(state.get("token_rate_key", "")).strip() or None
            _enforce_general_mcp_limit(authenticated_user_id, token_rate_key)
            return authenticated_user_id

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
    user_id = str(row["UserId"])
    _enforce_general_mcp_limit(user_id)
    return user_id


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


def _load_scoring_reference_rows() -> list[dict[str, Any]]:
    """Load the global dbo.Series-backed universe with current research fields."""
    catalog_rows = _fetch_all(
        """
        SELECT
            SeriesId,
            Symbol,
            Name,
            Type,
            PE,
            Volatility,
            Yield,
            EPS,
            DivAmount,
            Exchange,
            AssetType,
            AssetSubType,
            CandidateClass,
            Archetype,
            ClassificationMethod,
            ClassificationConfidence,
            ClassificationRuleVersion,
            NeedsReview,
            InclusionReason,
            ClassificationUpdatedAt
        FROM invest.McpScoringReferenceInstruments
        ORDER BY Symbol;
        """
    )
    research_by_symbol = {
        str(row.get("Symbol") or "").upper(): row
        for row in _load_research_rows(limit=MAX_ROWS)
    }
    merged_rows: list[dict[str, Any]] = []
    for catalog_row in catalog_rows:
        merged = dict(catalog_row)
        research = research_by_symbol.get(
            str(catalog_row.get("Symbol") or "").upper()
        )
        if research:
            merged.update(
                {key: value for key, value in research.items() if value is not None}
            )
        merged_rows.append(merged)
    return merged_rows


def _load_latest_fund_holdings() -> dict[str, dict[str, Any]]:
    """Load the latest provider-neutral fund holdings used for overlap scoring."""
    rows = _fetch_all(
        """
        SELECT
            FundSymbol,
            HoldingKey,
            HoldingSymbol,
            WeightPercent,
            AsOfDate,
            SourceName,
            ReportedCoveragePercent
        FROM invest.McpLatestFundHoldings
        ORDER BY FundSymbol, WeightPercent DESC, HoldingKey;
        """
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        fund_symbol = str(row.get("FundSymbol") or "").upper()
        holding_key = str(row.get("HoldingKey") or "").upper()
        weight_percent = _number(row.get("WeightPercent"))
        if not fund_symbol or not holding_key or weight_percent is None:
            continue
        entry = result.setdefault(
            fund_symbol,
            {
                "AsOfDate": row.get("AsOfDate"),
                "SourceName": row.get("SourceName"),
                "CoveragePercent": _number(row.get("ReportedCoveragePercent")),
                "Holdings": {},
            },
        )
        entry["Holdings"][holding_key] = max(0.0, weight_percent) / 100.0
    return result


def _load_scoring_history(
    symbol: str,
    user_id: str,
    as_of: date,
) -> list[dict[str, Any]]:
    start_date = as_of - timedelta(days=365 * 4)
    local_rows = _fetch_all(
        f"""
        SELECT TOP ({MAX_ROWS})
            instrument.Symbol,
            history.Date,
            history.OpenValue,
            history.HighValue,
            history.LowValue,
            history.LastValue,
            history.Volume,
            history.Updated
        FROM dbo.SeriesData AS history
        INNER JOIN invest.McpInstruments AS instrument
            ON instrument.SeriesId = history.SeriesId
        WHERE UPPER(instrument.Symbol) = ?
          AND history.Date >= ?
          AND history.Date <= ?
        ORDER BY history.Date;
        """,
        (symbol, start_date, as_of),
    )
    if local_rows:
        return [{**row, "Source": "SQL"} for row in local_rows]

    start_timestamp = int(
        datetime.combine(start_date, datetime_time.min, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    end_timestamp = int(
        datetime.combine(as_of, datetime_time.max, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    payload = _schwab_cached_market_data_get(
        "/pricehistory",
        {
            "symbol": symbol,
            "periodType": "year",
            "frequencyType": "daily",
            "frequency": 1,
            "startDate": start_timestamp,
            "endDate": end_timestamp,
            "needExtendedHoursData": "false",
            "needPreviousClose": "true",
        },
        ttl_seconds=900,
        user_id=user_id,
        units=5,
        is_history=True,
    )
    return _schwab_history_rows(payload, symbol, MAX_ROWS)


def _prepare_scoring_candidate(
    symbol: str,
    user_id: str,
    reference_rows: list[dict[str, Any]],
    score_as_of: date,
    benchmark_history: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    local = next(
        (
            dict(row)
            for row in reference_rows
            if str(row.get("Symbol") or "").upper() == symbol
        ),
        None,
    )
    if local is None:
        payload = _schwab_cached_market_data_get(
            "/quotes",
            {
                "symbols": symbol,
                "fields": "quote,reference,fundamental",
                "indicative": "false",
            },
            ttl_seconds=15,
            user_id=user_id,
            units=1,
        )
        instrument_rows = _schwab_quote_rows(payload, [symbol])
        local = dict(instrument_rows[0]) if instrument_rows else None
        if local is None:
            raise ValueError("Instrument not found.")
        local.update(classify_scoring_instrument(local))

    history = (
        benchmark_history
        if symbol == SCORING_BENCHMARK
        else _load_scoring_history(symbol, user_id, score_as_of)
    )
    if not history:
        raise ValueError("No price history is available for this instrument.")
    price_features = compute_price_features(
        history,
        benchmark_history,
        as_of=score_as_of,
    )
    candidate = dict(local)
    candidate.update(price_features)
    candidate["Symbol"] = symbol
    candidate["HistorySource"] = history[0].get("Source") if history else None
    return candidate, price_features, history


def _owned_scoring_positions(
    user_id: str,
    account_id: str,
    reference_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    account = _fetch_all(
        """
        SELECT AccountId
        FROM invest.Accounts
        WHERE AccountId = ?
          AND OwnerUserId = ?
          AND IsActive = 1;
        """,
        (account_id, user_id),
    )
    if not account:
        raise ValueError("Account not found.")
    positions = _fetch_all(
        """
        SELECT
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
            ) AS Quantity
        FROM invest.Transactions AS t
        INNER JOIN invest.Accounts AS a
            ON a.AccountId = t.AccountId
           AND a.OwnerUserId = t.UserId
        WHERE t.UserId = ?
          AND t.AccountId = ?
          AND t.IsDeleted = 0
          AND t.Symbol IS NOT NULL
          AND a.IsActive = 1
        GROUP BY t.Symbol
        HAVING SUM(
            CASE
                WHEN UPPER(t.TransactionType) IN
                     ('BUY', 'PURCHASE', 'OPENING_POSITION')
                    THEN COALESCE(t.Quantity, 0)
                WHEN UPPER(t.TransactionType) IN ('SELL', 'SALE')
                    THEN -COALESCE(t.Quantity, 0)
                ELSE 0
            END
        ) <> 0;
        """,
        (user_id, account_id),
    )
    price_by_symbol = {
        str(row.get("Symbol") or "").upper(): _number(row.get("LastValue"))
        for row in reference_rows
    }
    for position in positions:
        symbol = str(position.get("Symbol") or "").upper()
        quantity = abs(_number(position.get("Quantity")) or 0.0)
        price = price_by_symbol.get(symbol)
        position["MarketValue"] = quantity * price if price is not None else None
    return positions


def _load_position_histories(
    positions: list[dict[str, Any]],
    as_of: date,
) -> dict[str, list[dict[str, Any]]]:
    """Bulk-load local histories for portfolio correlation without Schwab calls."""
    symbols = _clean_symbols(
        [str(position.get("Symbol") or "") for position in positions]
    )
    if not symbols:
        return {}
    start_date = as_of - timedelta(days=365 * 4)
    placeholders = ",".join("?" for _ in symbols)
    rows = _fetch_all(
        f"""
        SELECT
            instrument.Symbol,
            history.Date,
            history.LastValue
        FROM dbo.SeriesData AS history
        INNER JOIN invest.McpInstruments AS instrument
            ON instrument.SeriesId = history.SeriesId
        WHERE UPPER(instrument.Symbol) IN ({placeholders})
          AND history.Date >= ?
          AND history.Date <= ?
        ORDER BY instrument.Symbol, history.Date;
        """,
        (*symbols, start_date, as_of),
    )
    histories: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("Symbol") or "").upper()
        if symbol:
            histories.setdefault(symbol, []).append(row)
    return histories


def _persist_scoring_snapshot(
    candidate: dict[str, Any],
    price_features: dict[str, Any],
    score: dict[str, Any],
) -> str | None:
    if not SCORING_PERSIST_SNAPSHOTS:
        return None
    try:
        feature_rows = _fetch_all(
            """
            EXEC invest.UpsertInstrumentFeatureSnapshot
                @SeriesId = ?,
                @Symbol = ?,
                @CandidateClass = ?,
                @Archetype = ?,
                @FeatureAsOf = ?,
                @PriceFeaturesJson = ?,
                @FundamentalFeaturesJson = ?,
                @ExposureFeaturesJson = ?,
                @DataSourcesJson = ?,
                @DataCompletenessScore = ?,
                @ModelVersion = ?;
            """,
            (
                candidate.get("SeriesId"),
                score["Symbol"],
                score["CandidateClass"],
                score["Archetype"],
                score["ScoreAsOf"],
                json.dumps(price_features, default=_serialize, separators=(",", ":")),
                json.dumps(candidate, default=_serialize, separators=(",", ":")),
                json.dumps(
                    {
                        "CandidateClass": score["CandidateClass"],
                        "Archetype": score["Archetype"],
                    },
                    separators=(",", ":"),
                ),
                json.dumps(
                    {
                        "History": candidate.get("HistorySource"),
                        "Fundamentals": candidate.get("Source", "SQL"),
                    },
                    separators=(",", ":"),
                ),
                score["DataCompletenessScore"],
                score["ModelVersion"],
            ),
        )
        feature_snapshot_id = (
            feature_rows[0].get("FeatureSnapshotId") if feature_rows else None
        )
        score_rows = _fetch_all(
            """
            EXEC invest.UpsertInstrumentScoreSnapshot
                @FeatureSnapshotId = ?,
                @Symbol = ?,
                @CandidateClass = ?,
                @Archetype = ?,
                @QualityScore = ?,
                @ValuationScore = ?,
                @GrowthScore = ?,
                @TrendScore = ?,
                @RiskScore = ?,
                @LiquidityCostScore = ?,
                @InvestmentQualityScore = ?,
                @TechnicalOpportunityScore = ?,
                @ReferenceSimilarityScore = ?,
                @StandaloneCandidateScore = ?,
                @DataCompletenessScore = ?,
                @PeerGroupLevel = ?,
                @PeerCount = ?,
                @ClosestPeersJson = ?,
                @StrengthsJson = ?,
                @ConcernsJson = ?,
                @MissingFeaturesJson = ?,
                @ScoreAsOf = ?,
                @ModelVersion = ?;
            """,
            (
                feature_snapshot_id,
                score["Symbol"],
                score["CandidateClass"],
                score["Archetype"],
                score["QualityScore"],
                score["ValuationScore"],
                score["GrowthScore"],
                score["TrendScore"],
                score["RiskScore"],
                score["LiquidityCostScore"],
                score["InvestmentQualityScore"],
                score["TechnicalOpportunityScore"],
                score["ReferenceSimilarityScore"],
                score["StandaloneCandidateScore"],
                score["DataCompletenessScore"],
                score["PeerGroupLevel"],
                score["PeerCount"],
                json.dumps(score["ClosestReferenceSymbols"], separators=(",", ":")),
                json.dumps(score["Strengths"], separators=(",", ":")),
                json.dumps(score["Concerns"], separators=(",", ":")),
                json.dumps(score["MissingFeatures"], separators=(",", ":")),
                score["ScoreAsOf"],
                score["ModelVersion"],
            ),
        )
        return (
            str(score_rows[0]["InstrumentScoreSnapshotId"])
            if score_rows and score_rows[0].get("InstrumentScoreSnapshotId")
            else None
        )
    except Exception:
        LOGGER.exception("Unable to persist investment scoring snapshot.")
        return None


def _persist_portfolio_candidate_score(
    user_id: str,
    account_id: str,
    instrument_score_snapshot_id: str | None,
    score: dict[str, Any],
) -> None:
    if not SCORING_PERSIST_SNAPSHOTS or not instrument_score_snapshot_id:
        return
    try:
        _fetch_all(
            """
            EXEC invest.UpsertPortfolioCandidateScoreSnapshot
                @UserId = ?,
                @AccountId = ?,
                @InstrumentScoreSnapshotId = ?,
                @TargetWeightPercent = ?,
                @PortfolioFitScore = ?,
                @CompositeCandidateScore = ?,
                @ReviewTier = ?,
                @PortfolioImpactJson = ?,
                @ScoreAsOf = ?,
                @ModelVersion = ?;
            """,
            (
                user_id,
                account_id,
                instrument_score_snapshot_id,
                score["PortfolioImpact"]["TargetWeightPercent"],
                score["PortfolioFitScore"],
                score["CompositeCandidateScore"],
                score["ReviewTier"],
                json.dumps(
                    score["PortfolioImpact"],
                    default=_serialize,
                    separators=(",", ":"),
                ),
                score["ScoreAsOf"],
                score["ModelVersion"],
            ),
        )
    except Exception:
        LOGGER.exception("Unable to persist portfolio candidate score snapshot.")


def _score_symbol_for_user(
    user_id: str,
    symbol: str,
    reference_rows: list[dict[str, Any]],
    benchmark_history: list[dict[str, Any]],
    *,
    account_id: str | None,
    target_weight_percent: float,
    score_as_of: date,
    owned_positions: list[dict[str, Any]] | None = None,
    fund_holdings: dict[str, dict[str, Any]] | None = None,
    position_histories: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    candidate, price_features, candidate_history = _prepare_scoring_candidate(
        symbol,
        user_id,
        reference_rows,
        score_as_of,
        benchmark_history,
    )
    score = score_candidate(
        candidate,
        reference_rows,
        model_version=SCORING_MODEL_VERSION,
        score_as_of=score_as_of,
        minimum_peer_count=SCORING_MIN_PEER_COUNT,
    )
    instrument_score_snapshot_id = _persist_scoring_snapshot(
        candidate,
        price_features,
        score,
    )
    if account_id is None:
        return score

    positions = (
        owned_positions
        if owned_positions is not None
        else _owned_scoring_positions(user_id, account_id, reference_rows)
    )
    loaded_fund_holdings = (
        fund_holdings if fund_holdings is not None else _load_latest_fund_holdings()
    )
    loaded_position_histories = (
        position_histories
        if position_histories is not None
        else _load_position_histories(positions, score_as_of)
    )
    portfolio_fit = compute_portfolio_fit(
        score,
        positions,
        reference_rows,
        target_weight_percent=target_weight_percent,
        fund_holdings=loaded_fund_holdings,
        candidate_history=candidate_history,
        position_histories=loaded_position_histories,
    )
    similarity = score["ReferenceSimilarityScore"]
    similarity_for_formula = similarity if similarity is not None else 50.0
    composite = round(
        score["InvestmentQualityScore"] * 0.35
        + score["ValuationScore"] * 0.20
        + score["TrendScore"] * 0.15
        + portfolio_fit["PortfolioFitScore"] * 0.20
        + similarity_for_formula * 0.10,
        2,
    )
    if score["RiskScore"] < 25:
        composite = min(composite, 69.0)
    if score["LiquidityCostScore"] < 20:
        composite = min(composite, 64.0)
    if portfolio_fit["PortfolioFitDataStatus"] != "Sufficient":
        composite = min(composite, 64.0)
    score["AccountId"] = account_id
    score["PortfolioFitScore"] = portfolio_fit["PortfolioFitScore"]
    score["PortfolioImpact"] = portfolio_fit
    score["CompositeCandidateScore"] = composite
    score["ReviewTier"] = scoring_review_tier(composite)
    score["Concerns"] = score["Concerns"] + portfolio_fit["Limitations"]
    _persist_portfolio_candidate_score(
        user_id,
        account_id,
        instrument_score_snapshot_id,
        score,
    )
    return score


@mcp.tool(annotations=OPEN_WORLD_READ_ONLY_TOOL)
def search_symbols(
    ctx: Context,
    query: str = "",
    asset_type: str | None = None,
    active_only: bool = True,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search local instruments, then Schwab for symbols not in the database."""
    user_id = _current_user_id(ctx)
    limit = _clamp_limit(limit, default=25)
    cleaned_query = query.strip()
    if len(cleaned_query) > 100 or any(ord(char) < 32 for char in cleaned_query):
        raise ValueError("query must be 100 printable characters or fewer.")
    filters = []
    params: list[Any] = []

    if cleaned_query:
        pattern = f"%{cleaned_query}%"
        filters.append("(Symbol LIKE ? OR Name LIKE ?)")
        params.extend([pattern, pattern])
    if asset_type:
        filters.append("AssetType = ?")
        params.append(asset_type)
    # invest.McpInstruments already exposes active instruments only. Keep the
    # parameter for backward-compatible tool schemas.

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    local_rows = _fetch_all(
        f"""
        SELECT TOP ({limit})
            SeriesId,
            Symbol,
            Name,
            Type,
            PE,
            Volatility,
            Yield,
            EPS,
            DivAmount,
            Exchange,
            AssetType,
            AssetSubType
        FROM invest.McpInstruments
        {where}
        ORDER BY Symbol;
        """,
        tuple(params),
    )

    results = [{**row, "Source": "SQL"} for row in local_rows]
    if not cleaned_query or len(results) >= limit:
        return results[:limit]

    try:
        payload = _schwab_cached_market_data_get(
            "/instruments",
            {"symbol": cleaned_query, "projection": "symbol-search"},
            ttl_seconds=3600,
            user_id=user_id,
            units=1,
        )
        external_rows = _schwab_instrument_rows(payload)
    except SchwabApiError:
        if results:
            LOGGER.warning("Schwab instrument search failed; returning SQL results.")
            return results[:limit]
        raise

    existing = {str(row.get("Symbol") or "").upper() for row in results}
    for row in external_rows:
        symbol = str(row.get("Symbol") or "").upper()
        if not symbol or symbol in existing:
            continue
        if asset_type and str(row.get("AssetType") or "").lower() != asset_type.lower():
            continue
        results.append(row)
        existing.add(symbol)
        if len(results) >= limit:
            break
    return results


@mcp.tool(annotations=OPEN_WORLD_READ_ONLY_TOOL)
def get_symbol_profile(ctx: Context, symbol: str) -> dict[str, Any] | None:
    """Return Schwab profile/fundamentals, merged with local data when present."""
    user_id = _current_user_id(ctx)
    cleaned = _clean_symbol(symbol)
    rows = _fetch_all(
        """
        SELECT TOP (1)
            SeriesId,
            Name,
            Symbol,
            Type,
            PE,
            Volatility,
            Yield,
            EPS,
            DivAmount,
            Exchange,
            AssetType,
            AssetSubType
        FROM invest.McpInstruments
        WHERE UPPER(Symbol) = ?;
        """,
        (cleaned,),
    )
    local = rows[0] if rows else None
    try:
        payload = _schwab_cached_market_data_get(
            "/instruments",
            {"symbol": cleaned, "projection": "fundamental"},
            ttl_seconds=3600,
            user_id=user_id,
            units=1,
        )
        external_rows = _schwab_instrument_rows(payload)
    except SchwabApiError:
        if local:
            return {**local, "Source": "SQL"}
        raise

    external = next(
        (
            row
            for row in external_rows
            if str(row.get("Symbol") or "").upper() == cleaned
        ),
        external_rows[0] if external_rows else None,
    )
    if not external:
        return {**local, "Source": "SQL"} if local else None
    if not local:
        return external
    merged = dict(local)
    merged.update({key: value for key, value in external.items() if value is not None})
    merged["Source"] = "Schwab+SQL"
    return merged


@mcp.tool(annotations=OPEN_WORLD_READ_ONLY_TOOL)
def get_latest_prices(ctx: Context, symbols: list[str]) -> list[dict[str, Any]]:
    """Return current Schwab quotes for any symbols, with SQL fallback."""
    user_id = _current_user_id(ctx)
    cleaned = _clean_symbols(symbols)
    if len(cleaned) > SCHWAB_MAX_QUOTE_SYMBOLS:
        raise ValueError(
            f"A quote request may contain at most {SCHWAB_MAX_QUOTE_SYMBOLS} "
            "unique symbols."
        )
    quote_by_symbol: dict[str, dict[str, Any]] = {}
    schwab_error: SchwabApiError | None = None
    try:
        sorted_symbols = sorted(cleaned)
        for start in range(0, len(sorted_symbols), 200):
            batch = sorted_symbols[start : start + 200]
            payload = _schwab_cached_market_data_get(
                "/quotes",
                {
                    "symbols": ",".join(batch),
                    "fields": "quote,reference,fundamental",
                    "indicative": "false",
                },
                ttl_seconds=15,
                user_id=user_id,
                units=max(1, (len(batch) + 49) // 50),
            )
            for row in _schwab_quote_rows(payload, batch):
                quote_by_symbol[str(row["Symbol"]).upper()] = row
    except SchwabApiError as exc:
        schwab_error = exc
        LOGGER.warning("Schwab quotes failed; trying SQL fallback.")

    missing = [symbol for symbol in cleaned if symbol not in quote_by_symbol]
    if missing:
        placeholders = ", ".join("?" for _ in missing)
        local_rows = _fetch_all(
            f"""
            SELECT
                i.Symbol,
                i.Name,
                latest.LastValue AS LastPrice,
                latest.PriceDate,
                latest.Updated,
                i.AssetType,
                i.AssetSubType,
                i.Exchange
            FROM invest.McpInstruments i
            OUTER APPLY
            (
                SELECT TOP (1)
                    sd.LastValue,
                    sd.Date AS PriceDate,
                    sd.Updated
                FROM dbo.SeriesData sd
                WHERE sd.SeriesId = i.SeriesId
                ORDER BY sd.Date DESC
            ) latest
            WHERE UPPER(i.Symbol) IN ({placeholders});
            """,
            tuple(missing),
        )
        for row in local_rows:
            normalized = {
                "Symbol": row.get("Symbol"),
                "Name": row.get("Name"),
                "AssetType": row.get("AssetType"),
                "AssetSubType": row.get("AssetSubType"),
                "Exchange": row.get("Exchange"),
                "Realtime": False,
                "LastPrice": row.get("LastPrice"),
                "PriceDate": row.get("PriceDate"),
                "Updated": row.get("Updated"),
                "Source": "SQL",
            }
            symbol_key = str(row.get("Symbol") or "").upper()
            if symbol_key:
                quote_by_symbol[symbol_key] = normalized

    if schwab_error and not quote_by_symbol:
        raise schwab_error
    return [quote_by_symbol[symbol] for symbol in cleaned if symbol in quote_by_symbol]


@mcp.tool(annotations=OPEN_WORLD_READ_ONLY_TOOL)
def get_price_history(
    ctx: Context,
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Return local daily history, using Schwab when the symbol is not loaded."""
    user_id = _current_user_id(ctx)
    cleaned = _clean_symbol(symbol)
    end_value = _parse_date(end_date, "end_date") or date.today()
    start_value = _parse_date(start_date, "start_date") or (
        end_value - timedelta(days=365 * 6)
    )
    if start_value > end_value:
        raise ValueError("start_date must be before or equal to end_date.")

    limit = _clamp_limit(limit, default=2000)
    local_rows = _fetch_all(
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
        JOIN invest.McpInstruments s
            ON s.SeriesId = sd.SeriesId
        WHERE UPPER(s.Symbol) = ?
          AND sd.Date >= ?
          AND sd.Date <= ?
        ORDER BY sd.Date;
        """,
        (cleaned, start_value, end_value),
    )
    if local_rows:
        return [{**row, "Source": "SQL"} for row in local_rows]

    start_timestamp = int(
        datetime.combine(
            start_value,
            datetime_time.min,
            tzinfo=timezone.utc,
        ).timestamp()
        * 1000
    )
    end_timestamp = int(
        datetime.combine(
            end_value,
            datetime_time.max,
            tzinfo=timezone.utc,
        ).timestamp()
        * 1000
    )
    payload = _schwab_cached_market_data_get(
        "/pricehistory",
        {
            "symbol": cleaned,
            "periodType": "year",
            "frequencyType": "daily",
            "frequency": 1,
            "startDate": start_timestamp,
            "endDate": end_timestamp,
            "needExtendedHoursData": "false",
            "needPreviousClose": "true",
        },
        ttl_seconds=900,
        user_id=user_id,
        units=5,
        is_history=True,
    )
    return _schwab_history_rows(payload, cleaned, limit)


@mcp.tool(annotations=OPEN_WORLD_READ_ONLY_TOOL)
def get_market_hours(
    ctx: Context,
    markets: list[str] | None = None,
    market_date: str | None = None,
) -> dict[str, Any]:
    """Return Schwab market hours for investment markets (options excluded)."""
    user_id = _current_user_id(ctx)
    requested_markets = [
        str(value).strip().lower() for value in (markets or ["equity", "bond"])
    ]
    allowed_markets = {"equity", "bond", "future", "forex"}
    if not requested_markets or not set(requested_markets) <= allowed_markets:
        raise ValueError("markets may contain equity, bond, future, and/or forex.")
    requested_date = _parse_date(market_date, "market_date") or date.today()
    payload = _schwab_cached_market_data_get(
        "/markets",
        {
            "markets": ",".join(dict.fromkeys(requested_markets)),
            "date": requested_date.isoformat(),
        },
        ttl_seconds=300,
        user_id=user_id,
        units=1,
    )
    return {
        "Source": "Schwab",
        "MarketDate": requested_date.isoformat(),
        "Markets": payload,
    }


@mcp.tool(annotations=OPEN_WORLD_READ_ONLY_TOOL)
def get_market_movers(
    ctx: Context,
    index_symbol: str = "$SPX",
    sort: str = "PERCENT_CHANGE_UP",
    frequency: int = 10,
    limit: int = 10,
) -> dict[str, Any]:
    """Return Schwab movers for a supported equity market or index."""
    user_id = _current_user_id(ctx)
    normalized_index = index_symbol.strip().upper()
    allowed_indexes = {
        "$DJI",
        "$COMPX",
        "$SPX",
        "NYSE",
        "NASDAQ",
        "OTCBB",
        "INDEX_ALL",
        "EQUITY_ALL",
    }
    if normalized_index not in allowed_indexes:
        raise ValueError(
            "index_symbol must be $DJI, $COMPX, $SPX, NYSE, NASDAQ, "
            "OTCBB, INDEX_ALL, or EQUITY_ALL."
        )
    normalized_sort = sort.strip().upper()
    allowed_sorts = {
        "VOLUME",
        "TRADES",
        "PERCENT_CHANGE_UP",
        "PERCENT_CHANGE_DOWN",
    }
    if normalized_sort not in allowed_sorts:
        raise ValueError(
            "sort must be VOLUME, TRADES, PERCENT_CHANGE_UP, or "
            "PERCENT_CHANGE_DOWN."
        )
    try:
        normalized_frequency = int(frequency)
    except (TypeError, ValueError) as exc:
        raise ValueError("frequency must be 0, 1, 5, 10, 30, or 60.") from exc
    if normalized_frequency not in {0, 1, 5, 10, 30, 60}:
        raise ValueError("frequency must be 0, 1, 5, 10, 30, or 60.")
    normalized_limit = _clamp_limit(limit, default=10)
    payload = _schwab_cached_market_data_get(
        f"/movers/{normalized_index}",
        {"sort": normalized_sort, "frequency": normalized_frequency},
        ttl_seconds=60,
        user_id=user_id,
        units=1,
    )
    screeners = payload.get("screeners", []) if isinstance(payload, dict) else []
    if not isinstance(screeners, list):
        raise SchwabApiError("Schwab movers returned an unexpected response.")
    movers = []
    for raw in screeners[:normalized_limit]:
        if not isinstance(raw, dict):
            continue
        movers.append(
            {
                "Symbol": raw.get("symbol"),
                "Name": raw.get("description"),
                "LastPrice": raw.get("lastPrice"),
                "NetChange": raw.get("netChange"),
                "NetPercentChange": raw.get("netPercentChange"),
                "Volume": raw.get("totalVolume"),
                "Trades": raw.get("trades"),
                "MarketShare": raw.get("marketShare"),
                "Source": "Schwab",
            }
        )
    return {
        "Source": "Schwab",
        "Index": normalized_index,
        "Sort": normalized_sort,
        "Frequency": normalized_frequency,
        "Movers": movers,
    }


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_research_snapshot(
    ctx: Context,
    symbols: list[str] | None = None,
    asset_type: str | None = None,
    watched_only: bool = False,
    traded_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return research/ranking rows from the configured research stored procedure."""
    _current_user_id(ctx)
    cleaned = _clean_symbols(symbols) if symbols else None
    return _load_research_rows(
        symbols=cleaned,
        asset_type=asset_type,
        watched_only=watched_only,
        traded_only=traded_only,
        limit=limit,
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def compare_symbols(ctx: Context, symbols: list[str]) -> list[dict[str, Any]]:
    """Compare selected symbols using the research stored procedure output."""
    _current_user_id(ctx)
    cleaned = _clean_symbols(symbols)
    return _load_research_rows(symbols=cleaned, limit=len(cleaned))


@mcp.tool(annotations=READ_ONLY_TOOL)
def screen_instruments(
    ctx: Context,
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
    _current_user_id(ctx)
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
def get_watched_symbols(ctx: Context, limit: int = 200) -> list[dict[str, Any]]:
    """Return instruments included in the MCP watch list."""
    _current_user_id(ctx)
    limit = _clamp_limit(limit, default=200)
    return _load_research_rows(watched_only=True, limit=limit)


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_traded_symbols(ctx: Context, limit: int = 200) -> list[dict[str, Any]]:
    """Return instruments included in the MCP traded-symbol list."""
    _current_user_id(ctx)
    limit = _clamp_limit(limit, default=200)
    return _load_research_rows(traded_only=True, limit=limit)


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_data_freshness(ctx: Context) -> dict[str, Any]:
    """Summarize instrument counts and latest available data dates."""
    _current_user_id(ctx)
    summary = _fetch_all(
        """
        SELECT
            COUNT(*) AS InstrumentCount,
            MIN(first_price.FirstDataDate) AS EarliestDataDate,
            MAX(latest_price.MaxDataDate) AS LatestDataDate,
            MAX(latest_price.Updated) AS LatestSeriesUpdate
        FROM invest.McpInstruments i
        OUTER APPLY
        (
            SELECT TOP (1)
                sd.Date AS FirstDataDate
            FROM dbo.SeriesData sd
            WHERE sd.SeriesId = i.SeriesId
            ORDER BY sd.Date
        ) first_price
        OUTER APPLY
        (
            SELECT TOP (1)
                sd.Date AS MaxDataDate,
                COALESCE(sd.Updated, sd.Created) AS Updated
            FROM dbo.SeriesData sd
            WHERE sd.SeriesId = i.SeriesId
            ORDER BY sd.Date DESC
        ) latest_price;
        """
    )
    date_buckets = _fetch_all(
        """
        WITH LatestInstrumentDates AS
        (
            SELECT
                i.SeriesId,
                latest_price.MaxDataDate
            FROM invest.McpInstruments i
            OUTER APPLY
            (
                SELECT TOP (1)
                    sd.Date AS MaxDataDate
                FROM dbo.SeriesData sd
                WHERE sd.SeriesId = i.SeriesId
                ORDER BY sd.Date DESC
            ) latest_price
        )
        SELECT TOP (10)
            CAST(MaxDataDate AS date) AS DataDate,
            COUNT(*) AS InstrumentCount
        FROM LatestInstrumentDates
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
def get_market_indicators(ctx: Context, limit: int = 100) -> list[dict[str, Any]]:
    """Return the highest-ranked current market research indicators."""
    _current_user_id(ctx)
    rows = _load_research_rows(limit=_clamp_limit(limit))
    return sorted(
        rows,
        key=lambda row: (_number(_get(row, "Rank", "Score")) is not None,
                         _number(_get(row, "Rank", "Score")) or 0),
        reverse=True,
    )[: _clamp_limit(limit)]


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_reference_universe(
    ctx: Context,
    candidate_class: str | None = None,
    archetype: str | None = None,
    include_needs_review: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return the global dbo.Series-backed investment reference universe."""
    _current_user_id(ctx)
    rows = _load_scoring_reference_rows()
    if candidate_class:
        expected_class = candidate_class.strip().casefold()
        rows = [
            row
            for row in rows
            if str(row.get("CandidateClass") or "").casefold() == expected_class
        ]
    if archetype:
        expected_archetype = archetype.strip().casefold()
        rows = [
            row
            for row in rows
            if str(row.get("Archetype") or "").casefold() == expected_archetype
        ]
    if not include_needs_review:
        rows = [row for row in rows if not _truthy(row.get("NeedsReview"))]
    fields = (
        "SeriesId",
        "Symbol",
        "Name",
        "Type",
        "AssetType",
        "AssetSubType",
        "CandidateClass",
        "Archetype",
        "ClassificationMethod",
        "ClassificationConfidence",
        "ClassificationRuleVersion",
        "NeedsReview",
        "InclusionReason",
    )
    return [
        {field: row.get(field) for field in fields}
        for row in rows[: _clamp_limit(limit, default=200)]
    ]


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_fund_holdings(
    symbol: str,
    ctx: Context,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the latest normalized holdings snapshot used for overlap scoring."""
    _current_user_id(ctx)
    cleaned_symbol = _clean_symbol(symbol)
    normalized_limit = _clamp_limit(limit, default=100)
    return _fetch_all(
        f"""
        SELECT TOP ({normalized_limit})
            FundSymbol,
            HoldingKey,
            HoldingSymbol,
            HoldingName,
            WeightPercent,
            AsOfDate,
            SourceName,
            SourceUrl,
            ReportedCoveragePercent
        FROM invest.McpLatestFundHoldings
        WHERE FundSymbol = ?
        ORDER BY WeightPercent DESC, HoldingKey;
        """,
        (cleaned_symbol,),
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_scoring_model(
    ctx: Context,
    model_version: str | None = None,
) -> dict[str, Any] | None:
    """Return one immutable scoring-model definition and its explicit weights."""
    _current_user_id(ctx)
    requested_version = (model_version or SCORING_MODEL_VERSION).strip()
    if not requested_version or len(requested_version) > 50:
        raise ValueError("model_version must contain 1 to 50 characters.")
    rows = _fetch_all(
        """
        SELECT
            ModelVersion,
            Description,
            BenchmarkSymbol,
            WeightsJson,
            NormalizationJson,
            ClassificationRuleVersion,
            EffectiveAt,
            IsActive,
            CreatedAt
        FROM invest.ScoringModelVersions
        WHERE ModelVersion = ?;
        """,
        (requested_version,),
    )
    if not rows:
        return None
    result = dict(rows[0])
    for field in ("WeightsJson", "NormalizationJson"):
        raw = result.pop(field, None)
        result[field.removesuffix("Json")] = json.loads(raw) if raw else {}
    return result


@mcp.tool(annotations=OPEN_WORLD_READ_ONLY_TOOL)
def score_instrument(
    symbol: str,
    ctx: Context,
    account_id: str | None = None,
    target_weight_percent: float = SCORING_DEFAULT_TARGET_WEIGHT_PERCENT,
) -> dict[str, Any]:
    """Score any instrument against the global universe and optional owned account."""
    user_id = _current_user_id(ctx)
    cleaned_symbol = _clean_symbol(symbol)
    normalized_account_id = (
        _canonical_uuid(account_id, "account_id") if account_id else None
    )
    try:
        normalized_target_weight = float(target_weight_percent)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_weight_percent must be numeric.") from exc
    if not 0 < normalized_target_weight <= 25:
        raise ValueError("target_weight_percent must be greater than 0 and at most 25.")

    score_as_of = date.today()
    reference_rows = _load_scoring_reference_rows()
    benchmark_history = _load_scoring_history(
        SCORING_BENCHMARK,
        user_id,
        score_as_of,
    )
    return _score_symbol_for_user(
        user_id,
        cleaned_symbol,
        reference_rows,
        benchmark_history,
        account_id=normalized_account_id,
        target_weight_percent=normalized_target_weight,
        score_as_of=score_as_of,
    )


@mcp.tool(annotations=OPEN_WORLD_READ_ONLY_TOOL)
def rank_candidates(
    symbols: list[str],
    ctx: Context,
    account_id: str | None = None,
    target_weight_percent: float = SCORING_DEFAULT_TARGET_WEIGHT_PERCENT,
) -> dict[str, Any]:
    """Score and rank a bounded list of candidates for review, never for trading."""
    user_id = _current_user_id(ctx)
    cleaned_symbols = _clean_symbols(symbols)
    if len(cleaned_symbols) > SCORING_MAX_CANDIDATES:
        raise ValueError(
            f"At most {SCORING_MAX_CANDIDATES} candidates may be ranked per call."
        )
    normalized_account_id = (
        _canonical_uuid(account_id, "account_id") if account_id else None
    )
    try:
        normalized_target_weight = float(target_weight_percent)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_weight_percent must be numeric.") from exc
    if not 0 < normalized_target_weight <= 25:
        raise ValueError("target_weight_percent must be greater than 0 and at most 25.")

    score_as_of = date.today()
    reference_rows = _load_scoring_reference_rows()
    benchmark_history = _load_scoring_history(
        SCORING_BENCHMARK,
        user_id,
        score_as_of,
    )
    owned_positions = (
        _owned_scoring_positions(user_id, normalized_account_id, reference_rows)
        if normalized_account_id
        else None
    )
    fund_holdings = _load_latest_fund_holdings() if normalized_account_id else None
    position_histories = (
        _load_position_histories(owned_positions or [], score_as_of)
        if normalized_account_id
        else None
    )
    scored: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for cleaned_symbol in cleaned_symbols:
        try:
            scored.append(
                _score_symbol_for_user(
                    user_id,
                    cleaned_symbol,
                    reference_rows,
                    benchmark_history,
                    account_id=normalized_account_id,
                    target_weight_percent=normalized_target_weight,
                    score_as_of=score_as_of,
                    owned_positions=owned_positions,
                    fund_holdings=fund_holdings,
                    position_histories=position_histories,
                )
            )
        except (ValueError, SchwabApiError, RateLimitExceeded) as exc:
            errors.append({"Symbol": cleaned_symbol, "Error": str(exc)})
    ranking_field = (
        "CompositeCandidateScore" if normalized_account_id else "StandaloneCandidateScore"
    )
    scored.sort(
        key=lambda row: _number(row.get(ranking_field)) or -1.0,
        reverse=True,
    )
    return {
        "RankingField": ranking_field,
        "ScoreAsOf": score_as_of.isoformat(),
        "ModelVersion": SCORING_MODEL_VERSION,
        "Candidates": scored,
        "Errors": errors,
    }


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_score_history(
    symbol: str,
    ctx: Context,
    account_id: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Return stored global or caller-owned portfolio-aware score snapshots."""
    user_id = _current_user_id(ctx)
    cleaned_symbol = _clean_symbol(symbol)
    normalized_limit = _clamp_limit(limit, default=30)
    if account_id:
        normalized_account_id = _canonical_uuid(account_id, "account_id")
        account = _fetch_all(
            """
            SELECT AccountId
            FROM invest.Accounts
            WHERE AccountId = ? AND OwnerUserId = ? AND IsActive = 1;
            """,
            (normalized_account_id, user_id),
        )
        if not account:
            raise ValueError("Account not found.")
        rows = _fetch_all(
            f"""
            SELECT TOP ({normalized_limit})
                portfolio.AccountId,
                instrument.Symbol,
                portfolio.TargetWeightPercent,
                portfolio.PortfolioFitScore,
                portfolio.CompositeCandidateScore,
                portfolio.ReviewTier,
                portfolio.PortfolioImpactJson,
                portfolio.ScoreAsOf,
                portfolio.ModelVersion,
                portfolio.CreatedAt
            FROM invest.PortfolioCandidateScoreSnapshots AS portfolio
            INNER JOIN invest.InstrumentScoreSnapshots AS instrument
                ON instrument.InstrumentScoreSnapshotId =
                   portfolio.InstrumentScoreSnapshotId
            WHERE portfolio.UserId = ?
              AND portfolio.AccountId = ?
              AND instrument.Symbol = ?
            ORDER BY portfolio.ScoreAsOf DESC, portfolio.CreatedAt DESC;
            """,
            (user_id, normalized_account_id, cleaned_symbol),
        )
        for row in rows:
            raw = row.pop("PortfolioImpactJson", None)
            row["PortfolioImpact"] = json.loads(raw) if raw else {}
        return rows

    rows = _fetch_all(
        f"""
        SELECT TOP ({normalized_limit})
            Symbol,
            CandidateClass,
            Archetype,
            InvestmentQualityScore,
            ValuationScore,
            TrendScore,
            RiskScore,
            LiquidityCostScore,
            TechnicalOpportunityScore,
            ReferenceSimilarityScore,
            StandaloneCandidateScore,
            DataCompletenessScore,
            PeerGroupLevel,
            PeerCount,
            ClosestPeersJson,
            StrengthsJson,
            ConcernsJson,
            MissingFeaturesJson,
            ScoreAsOf,
            ModelVersion,
            CreatedAt,
            UpdatedAt
        FROM invest.InstrumentScoreSnapshots
        WHERE Symbol = ?
        ORDER BY ScoreAsOf DESC, UpdatedAt DESC;
        """,
        (cleaned_symbol,),
    )
    json_fields = {
        "ClosestPeersJson": "ClosestReferenceSymbols",
        "StrengthsJson": "Strengths",
        "ConcernsJson": "Concerns",
        "MissingFeaturesJson": "MissingFeatures",
    }
    for row in rows:
        for source_field, output_field in json_fields.items():
            raw = row.pop(source_field, None)
            row[output_field] = json.loads(raw) if raw else []
    return rows


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


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_shared_portfolio(
    account_id: str,
    ctx: Context,
) -> dict[str, Any]:
    """Return cash and positions for one account shared with VIEW permission."""
    user_id = _current_user_id(ctx)
    normalized_account_id = _canonical_uuid(account_id, "account_id")
    grant_filter = """
          AND pg.RevokedAt IS NULL
          AND (pg.ExpiresAt IS NULL OR pg.ExpiresAt > SYSDATETIMEOFFSET())
          AND EXISTS
          (
              SELECT 1
              FROM OPENJSON(pg.PermissionsJson)
              WHERE [value] = N'VIEW'
          )
    """

    accounts = _fetch_all(
        f"""
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
            pg.ExpiresAt,
            a.RowVersion
        FROM invest.PortfolioGrants pg
        JOIN invest.Accounts a
          ON a.AccountId = pg.AccountId
         AND a.OwnerUserId = pg.OwnerUserId
        JOIN invest.Users owner
          ON owner.UserId = pg.OwnerUserId
         AND owner.IsActive = 1
        WHERE pg.AccountId = ?
          AND pg.RecipientUserId = ?
          AND a.IsActive = 1
          {grant_filter}
        """,
        (normalized_account_id, user_id),
    )
    if not accounts:
        # Do not reveal whether an inaccessible account exists.
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
        JOIN invest.PortfolioGrants pg
          ON pg.AccountId = cb.AccountId
         AND pg.OwnerUserId = cb.UserId
        JOIN invest.Accounts a
          ON a.AccountId = pg.AccountId
         AND a.OwnerUserId = pg.OwnerUserId
        WHERE pg.AccountId = ?
          AND pg.RecipientUserId = ?
          AND a.IsActive = 1
          {grant_filter}
        ORDER BY cb.Currency;
        """,
        (normalized_account_id, user_id),
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
        JOIN invest.PortfolioGrants pg
          ON pg.AccountId = t.AccountId
         AND pg.OwnerUserId = t.UserId
        JOIN invest.Accounts a
          ON a.AccountId = pg.AccountId
         AND a.OwnerUserId = pg.OwnerUserId
        WHERE pg.AccountId = ?
          AND pg.RecipientUserId = ?
          AND t.IsDeleted = 0
          AND t.Symbol IS NOT NULL
          AND a.IsActive = 1
          {grant_filter}
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
        ORDER BY t.Symbol;
        """,
        (normalized_account_id, user_id),
    )
    return {
        "accounts": accounts,
        "cash_balances": balances,
        "positions": positions,
    }


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

invest.McpInstruments:
  Least-privilege active-instrument catalog filtered to exclude internal TEMP
  and PORTF calculation series. It exposes identifiers, names, core
  fundamentals, exchange, and asset classification only. The MCP runtime has
  no direct SELECT permission on dbo.Series.

dbo.SeriesData:
  Daily history: SeriesDataId, SeriesId, Date, OpenValue, HighValue,
  LowValue, LastValue, Volume, Created, Updated, TradeTypeId.

Research procedure:
  The configured stored procedure returns performance, PE, yield,
  volatility, Sharpe ratio, annualized return, and drawdown fields.

invest.McpScoringReferenceInstruments and scoring snapshots:
  dbo.Series is the global curated reference universe. A local SQL trigger
  assigns transparent candidate classes and archetypes without changing the
  existing symbol-management application. Global feature/score snapshots are
  shared; portfolio-fit snapshots are restricted to the authenticated owner.

invest.McpLatestFundHoldings:
  Latest normalized provider holdings for each fund. Model 1.1 combines this
  global read-only dataset with local price history to measure constituent
  overlap and portfolio-return correlation. Missing holdings and insufficient
  correlation history impose explicit score caps rather than assuming a
  perfect portfolio fit.

Schwab market data:
  Read-only quotes, instrument profiles, daily price history, market hours,
  and equity/index movers. Option chains and brokerage order submission are
  intentionally excluded. Responses are cached briefly and never expose
  Schwab OAuth credentials.

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
