from __future__ import annotations

import asyncio
import hashlib
import os
import re
import struct
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import call, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

import server


class BearerAuthenticationTests(unittest.TestCase):
    def test_loads_legacy_token_with_default_subject(self) -> None:
        token = "a-long-test-token"
        with patch.dict(
            os.environ,
            {
                "MCP_BEARER_TOKEN": token,
                "MCP_DEFAULT_AUTH_SUBJECT": "auth0|andrey",
                "MCP_TOKEN_SUBJECTS_JSON": "",
            },
            clear=False,
        ):
            mapping = server._load_token_subjects()

        self.assertEqual(
            mapping[hashlib.sha256(token.encode("utf-8")).hexdigest()],
            "auth0|andrey",
        )

    def test_valid_token_adds_subject_to_request_state(self) -> None:
        token = "user-specific-test-token"
        observed_scope: dict[str, object] = {}

        async def app(scope, receive, send):
            observed_scope.update(scope)

        middleware = server.BearerAuthASGI(
            app,
            lambda supplied_token: (
                "auth0|son" if supplied_token == token else None
            ),
            "/mcp",
        )
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", f"Bearer {token}".encode("latin1"))],
        }

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            raise AssertionError(f"Unexpected response: {message}")

        asyncio.run(middleware(scope, receive, send))
        self.assertEqual(
            observed_scope["state"]["authentication_subject"],
            "auth0|son",
        )
        self.assertEqual(
            observed_scope["state"]["token_rate_key"],
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
        )

    def test_invalid_token_returns_401(self) -> None:
        messages: list[dict[str, object]] = []

        async def app(scope, receive, send):
            raise AssertionError("Protected app must not receive an invalid token")

        middleware = server.BearerAuthASGI(app, lambda token: None, "/mcp")
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer invalid")],
        }

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            messages.append(message)

        with patch.dict(
            os.environ,
            {
                "MCP_OAUTH_ENABLED": "true",
                "MCP_OAUTH_RESOURCE": (
                    "https://staging-investments-mcp.wiselinetrade.com/mcp"
                ),
            },
            clear=False,
        ):
            asyncio.run(middleware(scope, receive, send))
        self.assertEqual(messages[0]["status"], 401)
        headers = dict(messages[0]["headers"])
        self.assertEqual(
            headers[b"www-authenticate"],
            (
                b'Bearer resource_metadata="https://staging-investments-mcp.'
                b'wiselinetrade.com/.well-known/oauth-protected-resource/mcp", '
                b'scope="investments.read investments.write"'
            ),
        )

    def test_database_token_is_hashed_before_lookup(self) -> None:
        token = "imcp_" + "a" * 64
        with patch.object(
            server,
            "_fetch_one",
            return_value={
                "ApiTokenId": "token-id",
                "UserId": "user-id",
                "AuthenticationSubject": "local:andreySr",
            },
        ) as fetch_one:
            identity = server._resolve_database_token(token)

        self.assertEqual(identity.authentication_subject, "local:andreySr")
        self.assertEqual(identity.api_token_id, "token-id")
        self.assertEqual(identity.user_id, "user-id")
        self.assertEqual(identity.token_key, "token-id")
        self.assertEqual(
            fetch_one.call_args.args[1],
            (hashlib.sha256(token.encode("utf-8")).digest(),),
        )

    def test_hybrid_mode_falls_back_to_legacy_token(self) -> None:
        token = "temporary-legacy-token"
        with patch.dict(
            os.environ,
            {
                "MCP_TOKEN_AUTH_MODE": "hybrid",
                "MCP_BEARER_TOKEN": token,
                "MCP_DEFAULT_AUTH_SUBJECT": "local:andreySr",
                "MCP_TOKEN_SUBJECTS_JSON": "",
            },
            clear=False,
        ), patch.object(server, "_resolve_database_token", return_value=None):
            resolver = server._build_token_resolver()
            identity = resolver(token)
            self.assertEqual(identity.authentication_subject, "local:andreySr")
            self.assertIsNone(identity.api_token_id)
            self.assertIsNone(identity.user_id)

    def test_database_mode_does_not_accept_legacy_token(self) -> None:
        token = "temporary-legacy-token"
        with patch.dict(
            os.environ,
            {
                "MCP_TOKEN_AUTH_MODE": "database",
                "MCP_BEARER_TOKEN": token,
                "MCP_DEFAULT_AUTH_SUBJECT": "local:andreySr",
                "MCP_TOKEN_SUBJECTS_JSON": "",
            },
            clear=False,
        ), patch.object(server, "_resolve_database_token", return_value=None):
            resolver = server._build_token_resolver()
            self.assertIsNone(resolver(token))

    def test_token_concurrency_limit_returns_429_and_releases_slot(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            finish = asyncio.Event()
            token_limiter = server._KeyedConcurrencyLimiter(1)
            user_limiter = server._KeyedConcurrencyLimiter(2)

            async def app(scope, receive, send):
                started.set()
                await finish.wait()

            identity = server.AuthIdentity(
                authentication_subject="local:user",
                user_id="user-id",
                api_token_id="token-id",
                token_key="token-id",
            )
            middleware = server.BearerAuthASGI(
                app,
                lambda token: identity,
                "/mcp",
                token_concurrency_limiter=token_limiter,
                user_concurrency_limiter=user_limiter,
            )
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": [(b"authorization", b"Bearer token")],
            }

            async def receive():
                return {"type": "http.request"}

            async def discard_send(message):
                return None

            first = asyncio.create_task(
                middleware(scope, receive, discard_send)
            )
            await started.wait()
            messages: list[dict[str, object]] = []

            async def capture_send(message):
                messages.append(message)

            await middleware(scope, receive, capture_send)
            self.assertEqual(messages[0]["status"], 429)
            self.assertIn(b"token_concurrency", messages[1]["body"])
            self.assertIn(
                b"This API token has too many requests running",
                messages[1]["body"],
            )
            self.assertEqual(token_limiter.active("token-id"), 1)

            finish.set()
            await first
            self.assertEqual(token_limiter.active("token-id"), 0)
            self.assertEqual(user_limiter.active("user-id"), 0)

        with patch.object(server, "MCP_TOKEN_USAGE_LOG_ENABLED", False):
            asyncio.run(scenario())

    def test_user_concurrency_aggregates_different_tokens(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            finish = asyncio.Event()
            token_limiter = server._KeyedConcurrencyLimiter(1)
            user_limiter = server._KeyedConcurrencyLimiter(1)

            async def app(scope, receive, send):
                started.set()
                await finish.wait()

            identities = {
                "one": server.AuthIdentity(
                    "local:user", "user-id", "token-one", "token-one"
                ),
                "two": server.AuthIdentity(
                    "local:user", "user-id", "token-two", "token-two"
                ),
            }
            middleware = server.BearerAuthASGI(
                app,
                lambda token: identities.get(token),
                "/mcp",
                token_concurrency_limiter=token_limiter,
                user_concurrency_limiter=user_limiter,
            )

            def request_scope(token: str) -> dict[str, object]:
                return {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp",
                    "headers": [
                        (b"authorization", f"Bearer {token}".encode("latin1"))
                    ],
                }

            async def receive():
                return {"type": "http.request"}

            async def discard_send(message):
                return None

            first = asyncio.create_task(
                middleware(request_scope("one"), receive, discard_send)
            )
            await started.wait()
            messages: list[dict[str, object]] = []

            async def capture_send(message):
                messages.append(message)

            await middleware(request_scope("two"), receive, capture_send)
            self.assertEqual(messages[0]["status"], 429)
            self.assertIn(b"user_concurrency", messages[1]["body"])
            self.assertIn(
                b"Your account has too many requests running",
                messages[1]["body"],
            )
            self.assertEqual(token_limiter.active("token-two"), 0)

            finish.set()
            await first
            self.assertEqual(user_limiter.active("user-id"), 0)

        with patch.object(server, "MCP_TOKEN_USAGE_LOG_ENABLED", False):
            asyncio.run(scenario())

    def test_authenticated_post_records_request_and_response_metadata(self) -> None:
        request_body = b'{"method":"tools/call","params":{"name":"test_tool"}}'

        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200})
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"result":{"isError":false}}',
                }
            )

        identity = server.AuthIdentity(
            "local:user",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            "token-key",
        )
        middleware = server.BearerAuthASGI(
            app,
            lambda token: identity,
            "/mcp",
            token_concurrency_limiter=server._KeyedConcurrencyLimiter(2),
            user_concurrency_limiter=server._KeyedConcurrencyLimiter(4),
        )
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"authorization", b"Bearer token")],
        }

        async def receive():
            return {
                "type": "http.request",
                "body": request_body,
                "more_body": False,
            }

        async def send(message):
            return None

        with patch.object(
            server,
            "_record_api_token_usage",
        ) as record_usage, patch.object(
            server,
            "MCP_TOKEN_USAGE_LOG_ENABLED",
            True,
        ):
            asyncio.run(middleware(scope, receive, send))

        record_usage.assert_called_once()
        kwargs = record_usage.call_args.kwargs
        self.assertEqual(kwargs["captured_body"], request_body)
        self.assertEqual(kwargs["response_status"], 200)
        self.assertGreater(kwargs["response_bytes"], 0)


class ToolSchemaTests(unittest.TestCase):
    def test_context_is_not_exposed_in_public_tool_schemas(self) -> None:
        registered = server.mcp._tool_manager._tools
        for name in {
            "search_symbols",
            "get_symbol_profile",
            "get_latest_prices",
            "get_price_history",
            "get_market_hours",
            "get_market_movers",
            "get_research_snapshot",
            "compare_symbols",
            "screen_instruments",
        }:
            properties = registered[name].parameters.get("properties", {})
            self.assertNotIn("ctx", properties, name)

    def test_private_tool_schemas_do_not_accept_user_id(self) -> None:
        private_tools = {
            "create_account",
            "get_my_accounts",
            "get_my_portfolio",
            "import_opening_positions",
            "get_my_open_orders",
            "record_trade_execution",
            "create_limit_order_record",
            "update_limit_order_record",
            "cancel_limit_order_record",
            "update_cash_balance",
            "get_my_strategy",
            "update_my_strategy",
            "share_portfolio",
            "revoke_portfolio_access",
            "list_portfolio_access",
            "get_shared_portfolios",
            "get_shared_portfolio",
        }
        registered = server.mcp._tool_manager._tools
        self.assertTrue(private_tools <= set(registered))
        for name in private_tools:
            properties = registered[name].parameters.get("properties", {})
            self.assertNotIn("user_id", properties, name)
            self.assertNotIn("UserId", properties, name)

    def test_write_annotations(self) -> None:
        registered = server.mcp._tool_manager._tools
        self.assertFalse(registered["create_account"].annotations.readOnlyHint)
        self.assertTrue(registered["create_account"].annotations.idempotentHint)
        self.assertFalse(
            registered["import_opening_positions"].annotations.readOnlyHint
        )
        self.assertTrue(
            registered["import_opening_positions"].annotations.idempotentHint
        )
        self.assertFalse(registered["create_limit_order_record"].annotations.readOnlyHint)
        self.assertTrue(registered["create_limit_order_record"].annotations.idempotentHint)
        self.assertTrue(registered["cancel_limit_order_record"].annotations.destructiveHint)
        self.assertTrue(registered["get_shared_portfolio"].annotations.readOnlyHint)

    def test_order_tools_expose_duration_and_expiration(self) -> None:
        registered = server.mcp._tool_manager._tools
        create_properties = registered["create_limit_order_record"].parameters[
            "properties"
        ]
        update_properties = registered["update_limit_order_record"].parameters[
            "properties"
        ]
        for properties in (create_properties, update_properties):
            self.assertIn("duration", properties)
            self.assertIn("expires_on", properties)

    def test_create_account_derives_owner_from_caller(self) -> None:
        properties = server.mcp._tool_manager._tools["create_account"].parameters[
            "properties"
        ]
        self.assertIn("account_name", properties)
        self.assertIn("base_currency", properties)
        self.assertNotIn("owner_user_id", properties)

    def test_opening_import_has_explicit_position_schema(self) -> None:
        parameters = server.mcp._tool_manager._tools[
            "import_opening_positions"
        ].parameters
        properties = parameters["properties"]
        self.assertIn("account_id", properties)
        self.assertIn("positions", properties)
        position_schema = properties["positions"]["items"]
        if "$ref" in position_schema:
            definition_name = position_schema["$ref"].rsplit("/", 1)[-1]
            position_schema = parameters["$defs"][definition_name]
        self.assertEqual(
            set(position_schema["required"]),
            {"symbol", "quantity", "total_cost_basis"},
        )


class ConnectedProfileTests(unittest.TestCase):
    def test_profile_uses_display_name_as_account_nickname(self) -> None:
        with patch.object(
            server,
            "_current_user_id",
            return_value="00000000-0000-0000-0000-000000000001",
        ), patch.object(
            server,
            "_fetch_one",
            return_value={
                "UserId": "00000000-0000-0000-0000-000000000001",
                "DisplayName": "Andrey",
            },
        ):
            profile = server.get_my_profile(None)

        self.assertEqual(profile.name, "Andrey")
        self.assertEqual(profile.nickname, "Andrey")


class PortfolioContextTests(unittest.TestCase):
    def test_portfolio_context_includes_applicable_strategy_rules(self) -> None:
        account_id = "00000000-0000-0000-0000-000000000010"
        strategy = {
            "StrategyRuleId": "00000000-0000-0000-0000-000000000020",
            "AccountId": account_id,
            "RuleName": "Long-term growth",
            "RuleType": "allocation",
            "RuleJson": '{"targetEquityPercent":70}',
            "Scope": "portfolio",
            "IsEnabled": True,
        }
        with patch.object(
            server,
            "_current_user_id",
            return_value="00000000-0000-0000-0000-000000000001",
        ), patch.object(
            server,
            "_fetch_all",
            side_effect=[
                [{"AccountId": account_id, "AccountName": "Retirement"}],
                [],
                [{"AccountId": account_id, "Symbol": "VOO"}],
                [strategy],
            ],
        ) as fetch_all:
            result = server.get_my_portfolio(None, account_id)

        self.assertEqual(result["strategy_rules"], [strategy])
        self.assertEqual(fetch_all.call_count, 4)
        strategy_sql, strategy_params = fetch_all.call_args_list[3].args
        self.assertIn("FROM invest.StrategyRules", strategy_sql)
        self.assertIn("sr.AccountId = ? OR sr.AccountId IS NULL", strategy_sql)
        self.assertEqual(
            strategy_params,
            ("00000000-0000-0000-0000-000000000001", account_id),
        )

    def test_all_portfolios_include_global_and_portfolio_strategy_rules(self) -> None:
        with patch.object(
            server,
            "_current_user_id",
            return_value="user-1",
        ), patch.object(
            server,
            "_fetch_all",
            side_effect=[[{"AccountId": "account-1"}], [], [], []],
        ) as fetch_all:
            result = server.get_my_portfolio(None)

        self.assertEqual(result["strategy_rules"], [])
        strategy_sql, strategy_params = fetch_all.call_args_list[3].args
        self.assertNotIn("sr.AccountId = ?", strategy_sql)
        self.assertEqual(strategy_params, ("user-1",))


class SharedPortfolioTests(unittest.TestCase):
    def test_shared_portfolio_scopes_every_query_to_active_view_grant(self) -> None:
        account_id = "00000000-0000-0000-0000-000000000010"
        recipient_id = "00000000-0000-0000-0000-000000000020"
        account = {"AccountId": account_id, "OwnerDisplayName": "Child"}
        balance = {"AccountId": account_id, "TotalAmount": 125.0}
        position = {"AccountId": account_id, "Symbol": "VOO", "Quantity": 2.0}

        with patch.object(
            server,
            "_current_user_id",
            return_value=recipient_id,
        ), patch.object(
            server,
            "_fetch_all",
            side_effect=[[account], [balance], [position]],
        ) as fetch_all:
            result = server.get_shared_portfolio(account_id, object())

        self.assertEqual(result["accounts"], [account])
        self.assertEqual(result["cash_balances"], [balance])
        self.assertEqual(result["positions"], [position])
        self.assertEqual(fetch_all.call_count, 3)
        for query_call in fetch_all.call_args_list:
            sql, params = query_call.args
            self.assertIn("invest.PortfolioGrants", sql)
            self.assertIn("pg.RecipientUserId = ?", sql)
            self.assertIn("pg.RevokedAt IS NULL", sql)
            self.assertIn("pg.ExpiresAt", sql)
            self.assertIn("OPENJSON(pg.PermissionsJson)", sql)
            self.assertIn("[value] = N'VIEW'", sql)
            self.assertEqual(params, (account_id, recipient_id))

    def test_inaccessible_shared_account_returns_not_found(self) -> None:
        with patch.object(
            server,
            "_current_user_id",
            return_value="00000000-0000-0000-0000-000000000020",
        ), patch.object(server, "_fetch_all", return_value=[]) as fetch_all:
            with self.assertRaisesRegex(ValueError, "Account not found"):
                server.get_shared_portfolio(
                    "00000000-0000-0000-0000-000000000010",
                    object(),
                )

        fetch_all.assert_called_once()


class OrderDurationTests(unittest.TestCase):
    def test_normalizes_common_duration_names(self) -> None:
        self.assertEqual(server._normalize_order_duration("GTC"), "GTC")
        self.assertEqual(
            server._normalize_order_duration("good till date"),
            "GTD",
        )

    def test_rejects_unknown_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "duration must be"):
            server._normalize_order_duration("sixty days")

    def test_parses_expiration_date(self) -> None:
        self.assertEqual(
            server._parse_order_expiration("2026-10-02").isoformat(),
            "2026-10-02",
        )


class OpeningPositionWriteTests(unittest.TestCase):
    def test_import_does_not_write_cash_balances(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.statements: list[str] = []
                self.calls: list[tuple[str, tuple[object, ...]]] = []

            def execute(self, sql, params=()):
                self.statements.append(sql)
                self.calls.append((sql, tuple(params)))
                return self

        cursor = FakeCursor()

        def run_write(user_id, tool_name, key, payload, operation):
            self.assertEqual(tool_name, "import_opening_positions")
            return operation(cursor)

        transaction = {
            "TransactionId": "00000000-0000-0000-0000-000000000001",
            "Symbol": "VOO",
        }
        with patch.object(server, "_current_user_id", return_value="caller-id"), patch.object(
            server,
            "_require_owned_account",
            return_value={"BaseCurrency": "USD"},
        ), patch.object(
            server,
            "_rows_from_cursor",
            side_effect=[[], [transaction]],
        ), patch.object(server, "_write_audit"), patch.object(
            server,
            "_run_idempotent_write",
            side_effect=run_write,
        ):
            result = server.import_opening_positions(
                account_id="00000000-0000-0000-0000-000000000010",
                positions=[
                    server.OpeningPositionInput(
                        symbol="VOO",
                        quantity=10,
                        total_cost_basis=5000,
                    )
                ],
                as_of="2026-08-10T15:00:00-04:00",
                idempotency_key="00000000-0000-4000-8000-000000000011",
                ctx=object(),
            )

        sql = "\n".join(cursor.statements)
        self.assertIn("OPENING_POSITION", sql)
        self.assertNotIn("CashBalances", sql)
        insert_sql, insert_params = next(
            call_args
            for call_args in cursor.calls
            if "INSERT INTO invest.Transactions" in call_args[0]
        )
        self.assertIn("CAST(? AS datetimeoffset(7))", insert_sql)
        self.assertEqual(
            insert_params[-2],
            "2026-08-10T19:00:00.000000+00:00",
        )
        self.assertFalse(result["cash_updated"])

    def test_duplicate_symbols_are_rejected_before_write(self) -> None:
        with patch.object(server, "_current_user_id", return_value="caller-id"):
            with self.assertRaisesRegex(ValueError, "duplicate symbol VOO"):
                server.import_opening_positions(
                    account_id="00000000-0000-0000-0000-000000000010",
                    positions=[
                        server.OpeningPositionInput(
                            symbol="voo",
                            quantity=1,
                            total_cost_basis=100,
                        ),
                        server.OpeningPositionInput(
                            symbol="VOO",
                            quantity=2,
                            total_cost_basis=200,
                        ),
                    ],
                    idempotency_key="00000000-0000-4000-8000-000000000011",
                    ctx=object(),
                )


class ResearchProcedureTests(unittest.TestCase):
    def test_server_side_flags_do_not_require_returned_flag_columns(self) -> None:
        row = {"Symbol": "VOO", "AssetType": "ETF", "Rank": 1}
        with patch.object(
            server,
            "RESEARCH_PROCEDURE_HAS_FILTERS",
            True,
        ), patch.object(server, "_fetch_all", return_value=[row]) as fetch_all:
            result = server._load_research_rows(
                watched_only=True,
                traded_only=True,
                limit=10,
            )

        self.assertEqual(result, [row])
        sql = fetch_all.call_args.args[0]
        self.assertIn("@WatchedOnly = ?", sql)
        self.assertIn("@TradedOnly = ?", sql)


class SqlServerTypeTests(unittest.TestCase):
    def test_normalizes_client_datetimeoffset_to_utc_sql_string(self) -> None:
        value = server._parse_datetimeoffset(
            "2026-10-01T09:30:00-04:00",
            "occurred_at",
        )

        self.assertEqual(
            value.isoformat(timespec="microseconds"),
            "2026-10-01T13:30:00.000000+00:00",
        )

    def test_rejects_client_timestamp_without_offset(self) -> None:
        with self.assertRaisesRegex(ValueError, "must include a UTC offset or Z"):
            server._parse_datetimeoffset(
                "2026-10-01T09:30:00",
                "occurred_at",
            )

    def test_decodes_datetimeoffset_odbc_value(self) -> None:
        raw = struct.pack(
            "<6hI2h",
            2026,
            8,
            7,
            15,
            30,
            10,
            123_456_000,
            -4,
            0,
        )
        value = server._decode_datetimeoffset(raw)
        self.assertEqual(value.isoformat(), "2026-08-07T15:30:10.123456-04:00")


class DateTimeOffsetWriteTests(unittest.TestCase):
    class FakeCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def execute(self, sql, params=()):
            self.calls.append((sql, tuple(params)))
            return self

    @staticmethod
    def _run_write(cursor):
        def run_write(user_id, tool_name, key, payload, operation):
            return operation(cursor)

        return run_write

    @staticmethod
    def _find_call(cursor, fragment: str):
        return next(call_args for call_args in cursor.calls if fragment in call_args[0])

    def test_record_trade_execution_binds_utc_strings(self) -> None:
        cursor = self.FakeCursor()
        transaction = {
            "TransactionId": "00000000-0000-0000-0000-000000000001",
        }
        balance = {
            "AccountId": "00000000-0000-0000-0000-000000000010",
        }
        with patch.object(
            server,
            "_current_user_id",
            return_value="caller-id",
        ), patch.object(server, "_require_owned_account"), patch.object(
            server,
            "_rows_from_cursor",
            side_effect=[[transaction], [balance]],
        ), patch.object(server, "_write_audit"), patch.object(
            server,
            "_run_idempotent_write",
            side_effect=self._run_write(cursor),
        ):
            server.record_trade_execution(
                account_id="00000000-0000-0000-0000-000000000010",
                client_execution_id="jpm-2026-10-01",
                transaction_type="BUY",
                symbol="JPM",
                quantity=1,
                price=310,
                gross_amount=310,
                currency="USD",
                idempotency_key="00000000-0000-4000-8000-000000000011",
                occurred_at="2026-10-01T09:30:00-04:00",
                ctx=object(),
            )

        transaction_sql, transaction_params = self._find_call(
            cursor,
            "INSERT INTO invest.Transactions",
        )
        cash_sql, cash_params = self._find_call(
            cursor,
            "UPDATE invest.CashBalances",
        )
        self.assertIn("CAST(? AS datetimeoffset(7))", transaction_sql)
        self.assertIn("CAST(? AS datetimeoffset(7))", cash_sql)
        self.assertEqual(
            transaction_params[-1],
            "2026-10-01T13:30:00.000000+00:00",
        )
        self.assertEqual(
            cash_params[2],
            "2026-10-01T13:30:00.000000+00:00",
        )

    def test_update_cash_balance_casts_update_and_insert(self) -> None:
        cursor = self.FakeCursor()
        result = {
            "AccountId": "00000000-0000-0000-0000-000000000010",
            "Currency": "USD",
        }
        with patch.object(
            server,
            "_current_user_id",
            return_value="caller-id",
        ), patch.object(server, "_require_owned_account"), patch.object(
            server,
            "_rows_from_cursor",
            side_effect=[[], [result]],
        ), patch.object(server, "_write_audit"), patch.object(
            server,
            "_run_idempotent_write",
            side_effect=self._run_write(cursor),
        ):
            server.update_cash_balance(
                account_id="00000000-0000-0000-0000-000000000010",
                currency="USD",
                total_amount=100,
                available_amount=90,
                as_of="2026-10-01T09:30:00-04:00",
                idempotency_key="00000000-0000-4000-8000-000000000011",
                ctx=object(),
            )

        update_sql, update_params = self._find_call(
            cursor,
            "UPDATE invest.CashBalances",
        )
        insert_sql, insert_params = self._find_call(
            cursor,
            "INSERT INTO invest.CashBalances",
        )
        self.assertIn("CAST(? AS datetimeoffset(7))", update_sql)
        self.assertIn("CAST(? AS datetimeoffset(7))", insert_sql)
        self.assertEqual(
            update_params[2],
            "2026-10-01T13:30:00.000000+00:00",
        )
        self.assertEqual(
            insert_params[-1],
            "2026-10-01T13:30:00.000000+00:00",
        )

    def test_share_portfolio_casts_expiration_update_and_insert(self) -> None:
        cursor = self.FakeCursor()
        result = {
            "PortfolioGrantId": "00000000-0000-0000-0000-000000000030",
        }
        with patch.object(
            server,
            "_current_user_id",
            return_value="00000000-0000-0000-0000-000000000001",
        ), patch.object(server, "_require_owned_account"), patch.object(
            server,
            "_rows_from_cursor",
            side_effect=[[{"Exists": 1}], [], [result]],
        ), patch.object(server, "_write_audit"), patch.object(
            server,
            "_run_idempotent_write",
            side_effect=self._run_write(cursor),
        ):
            server.share_portfolio(
                account_id="00000000-0000-0000-0000-000000000010",
                recipient_user_id="00000000-0000-0000-0000-000000000002",
                permissions=["VIEW"],
                idempotency_key="00000000-0000-4000-8000-000000000011",
                expires_at="2026-10-01T09:30:00-04:00",
                ctx=object(),
            )

        update_sql, update_params = self._find_call(
            cursor,
            "UPDATE invest.PortfolioGrants",
        )
        insert_sql, insert_params = self._find_call(
            cursor,
            "INSERT INTO invest.PortfolioGrants",
        )
        self.assertIn("CAST(? AS datetimeoffset(7))", update_sql)
        self.assertIn("CAST(? AS datetimeoffset(7))", insert_sql)
        self.assertEqual(
            update_params[1],
            "2026-10-01T13:30:00.000000+00:00",
        )
        self.assertEqual(
            insert_params[-1],
            "2026-10-01T13:30:00.000000+00:00",
        )


@unittest.skipUnless(
    os.getenv("MCP_TEST_SQLSERVER_CONN"),
    "MCP_TEST_SQLSERVER_CONN is required for SQL Server integration tests.",
)
class SqlServerDateTimeOffsetIntegrationTests(unittest.TestCase):
    def test_offset_timestamp_round_trips_as_the_same_utc_instant(self) -> None:
        expected = server._parse_datetimeoffset(
            "2026-10-01T09:30:00-04:00",
            "occurred_at",
        )
        sql_value = expected.isoformat(timespec="microseconds")
        connection = server.pyodbc.connect(
            os.environ["MCP_TEST_SQLSERVER_CONN"],
            autocommit=True,
        )
        try:
            connection.add_output_converter(-155, server._decode_datetimeoffset)
            row = connection.cursor().execute(
                """
                DECLARE @Values TABLE (OccurredAt datetimeoffset(7));
                INSERT INTO @Values (OccurredAt)
                VALUES (CAST(? AS datetimeoffset(7)));
                SELECT OccurredAt FROM @Values;
                """,
                (sql_value,),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(
            row[0].astimezone(timezone.utc),
            datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc),
        )


class SchwabOAuthTests(unittest.TestCase):
    def test_access_token_is_refreshed_before_thirty_minute_expiration(self) -> None:
        now = datetime(2026, 8, 26, 16, 0, tzinfo=timezone.utc)
        config = {
            "AccessToken": "existing-token",
            "AccessTokenUpdateTime": (now - timedelta(minutes=24)).isoformat(),
        }
        with patch.object(
            server,
            "SCHWAB_ACCESS_TOKEN_TTL_SECONDS",
            1800,
        ), patch.object(
            server,
            "SCHWAB_ACCESS_TOKEN_REFRESH_BUFFER_SECONDS",
            300,
        ):
            self.assertTrue(server._schwab_access_token_is_fresh(config, now=now))

            config["AccessTokenUpdateTime"] = (
                now - timedelta(minutes=25)
            ).isoformat()
            self.assertFalse(
                server._schwab_access_token_is_fresh(config, now=now)
            )

    def test_fresh_access_token_does_not_call_schwab(self) -> None:
        config = {
            "AccessToken": "existing-token",
            "AccessTokenUpdateTime": datetime.now(timezone.utc).isoformat(),
        }
        with patch.object(
            server,
            "_get_schwab_oauth_config",
            return_value=config,
        ), patch.object(
            server,
            "_request_new_schwab_access_token",
        ) as request_token, patch.object(
            server,
            "_save_schwab_access_token",
        ) as save_token:
            token = server._get_schwab_access_token()

        self.assertEqual(token, "existing-token")
        request_token.assert_not_called()
        save_token.assert_not_called()

    def test_stale_access_token_is_refreshed_and_saved(self) -> None:
        config = {
            "AccessToken": "expired-token",
            "AccessTokenUpdateTime": None,
        }
        with patch.object(
            server,
            "_get_schwab_oauth_config",
            return_value=config,
        ), patch.object(
            server,
            "_request_new_schwab_access_token",
            return_value=("new-token", 1800),
        ) as request_token, patch.object(
            server,
            "_save_schwab_access_token",
        ) as save_token:
            token = server._get_schwab_access_token()

        self.assertEqual(token, "new-token")
        request_token.assert_called_once_with(config)
        save_token.assert_called_once_with("new-token")

    def test_refresh_request_uses_basic_auth_and_refresh_grant(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, limit):
                self.limit = limit
                return b'{"access_token":"new-token","expires_in":1800}'

        config = {
            "client_id": "test-client",
            "client_secret": "test-secret",
            "RefreshToken": "test-refresh-token",
            "URLtoGetCode": (
                "https://auth.tdameritrade.com/auth?response_type=code"
            ),
        }
        response = FakeResponse()
        with patch.object(
            server,
            "SCHWAB_TOKEN_URL",
            "https://api.schwabapi.com/v1/oauth/token",
        ), patch.object(server, "urlopen", return_value=response) as open_url:
            token, expires_in = server._request_new_schwab_access_token(config)

        self.assertEqual((token, expires_in), ("new-token", 1800))
        request = open_url.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.schwabapi.com/v1/oauth/token",
        )
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertEqual(
            parse_qs(request.data.decode("utf-8")),
            {
                "grant_type": ["refresh_token"],
                "refresh_token": ["test-refresh-token"],
            },
        )

    def test_non_schwab_token_url_is_rejected(self) -> None:
        with patch.object(
            server,
            "SCHWAB_TOKEN_URL",
            "https://example.com/v1/oauth/token",
        ), self.assertRaisesRegex(server.SchwabApiError, "token endpoint"):
            server._validated_schwab_token_url()

    def test_market_data_retries_once_with_forced_refresh_after_401(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, limit):
                return b'{"symbol":"VOO"}'

        unauthorized = HTTPError(
            "https://api.schwabapi.com/marketdata/v1/quotes",
            401,
            "Unauthorized",
            None,
            None,
        )
        with patch.object(
            server,
            "_get_schwab_access_token",
            side_effect=["old-token", "new-token"],
        ) as get_token, patch.object(
            server,
            "urlopen",
            side_effect=[unauthorized, FakeResponse()],
        ):
            result = server._schwab_market_data_get(
                "/quotes",
                {"symbols": "VOO"},
            )

        self.assertEqual(result, {"symbol": "VOO"})
        self.assertEqual(
            get_token.call_args_list,
            [call(force_refresh=False), call(force_refresh=True)],
        )


class SchwabMarketDataToolTests(unittest.TestCase):
    def test_investment_tools_are_registered_without_options_tool(self) -> None:
        registered = server.mcp._tool_manager._tools
        self.assertIn("get_market_hours", registered)
        self.assertIn("get_market_movers", registered)
        self.assertNotIn("get_option_chain", registered)
        for name in {
            "search_symbols",
            "get_symbol_profile",
            "get_latest_prices",
            "get_price_history",
            "get_market_hours",
            "get_market_movers",
        }:
            self.assertTrue(registered[name].annotations.readOnlyHint, name)
            self.assertTrue(registered[name].annotations.openWorldHint, name)

    def test_live_quotes_are_normalized_without_sql_fallback(self) -> None:
        payload = {
            "VOO": {
                "symbol": "VOO",
                "assetMainType": "EQUITY",
                "assetSubType": "ETF",
                "realtime": True,
                "reference": {
                    "description": "Vanguard S&P 500 ETF",
                    "exchangeName": "NYSE Arca",
                },
                "quote": {
                    "lastPrice": 712.34,
                    "mark": 712.30,
                    "netPercentChange": 0.5,
                    "quoteTime": 1787774400000,
                },
            }
        }
        with patch.object(
            server,
            "_schwab_cached_market_data_get",
            return_value=payload,
        ), patch.object(server, "_current_user_id", return_value="user-1"), patch.object(
            server, "_fetch_all"
        ) as fetch_all:
            rows = server.get_latest_prices(None, ["voo"])

        fetch_all.assert_not_called()
        self.assertEqual(rows[0]["Symbol"], "VOO")
        self.assertEqual(rows[0]["LastPrice"], 712.34)
        self.assertEqual(rows[0]["Source"], "Schwab")

    def test_symbol_search_uses_schwab_when_local_database_has_no_match(self) -> None:
        payload = {
            "instruments": [
                {
                    "symbol": "SCHD",
                    "description": "Schwab US Dividend Equity ETF",
                    "assetType": "EQUITY",
                    "exchange": "NYSE Arca",
                }
            ]
        }
        with patch.object(server, "_current_user_id", return_value="user-1"), patch.object(
            server, "_fetch_all", return_value=[]
        ), patch.object(
            server,
            "_schwab_cached_market_data_get",
            return_value=payload,
        ) as schwab_get:
            rows = server.search_symbols(None, "SCHD")

        self.assertEqual(rows[0]["Symbol"], "SCHD")
        self.assertEqual(rows[0]["Source"], "Schwab")
        self.assertEqual(schwab_get.call_args.args[0], "/instruments")

    def test_unknown_symbol_history_uses_schwab(self) -> None:
        payload = {
            "symbol": "NEW",
            "candles": [
                {
                    "datetime": 1787702400000,
                    "open": 10,
                    "high": 12,
                    "low": 9,
                    "close": 11,
                    "volume": 1000,
                }
            ],
        }
        with patch.object(server, "_current_user_id", return_value="user-1"), patch.object(
            server, "_fetch_all", return_value=[]
        ), patch.object(
            server,
            "_schwab_cached_market_data_get",
            return_value=payload,
        ) as schwab_get:
            rows = server.get_price_history(
                None,
                "NEW",
                start_date="2026-08-25",
                end_date="2026-08-26",
            )

        self.assertEqual(rows[0]["Symbol"], "NEW")
        self.assertEqual(rows[0]["LastValue"], 11)
        self.assertEqual(rows[0]["Source"], "Schwab")
        self.assertEqual(schwab_get.call_args.args[0], "/pricehistory")

    def test_cache_returns_copies_and_avoids_duplicate_provider_calls(self) -> None:
        server._SCHWAB_RESPONSE_CACHE.clear()
        metrics = server.RequestUsageMetrics()
        metrics_token = server._REQUEST_USAGE_METRICS.set(metrics)
        try:
            with patch.object(
                server,
                "_schwab_market_data_get",
                return_value={"value": [1]},
            ) as provider_get, patch.object(
                server,
                "_reserve_schwab_user_capacity",
            ) as reserve_capacity:
                first = server._schwab_cached_market_data_get(
                    "/quotes",
                    {"symbols": "VOO"},
                    ttl_seconds=15,
                    user_id="user-1",
                )
                first["value"].append(2)
                second = server._schwab_cached_market_data_get(
                    "/quotes",
                    {"symbols": "VOO"},
                    ttl_seconds=15,
                    user_id="user-1",
                )
        finally:
            server._REQUEST_USAGE_METRICS.reset(metrics_token)

        provider_get.assert_called_once()
        reserve_capacity.assert_called_once_with(
            "user-1",
            1,
            is_history=False,
        )
        self.assertEqual(metrics.schwab_units, 1)
        self.assertEqual(metrics.schwab_cache_hits, 1)
        self.assertEqual(second, {"value": [1]})

    def test_quotes_reject_more_than_two_hundred_unique_symbols(self) -> None:
        symbols = [f"S{index}" for index in range(201)]
        with patch.object(
            server,
            "_current_user_id",
            return_value="user-1",
        ), self.assertRaisesRegex(ValueError, "at most 200"):
            server.get_latest_prices(None, symbols)

    def test_quote_units_scale_by_fifty_symbols(self) -> None:
        symbols = [f"S{index}" for index in range(51)]
        with patch.object(
            server,
            "_current_user_id",
            return_value="user-1",
        ), patch.object(
            server,
            "_schwab_cached_market_data_get",
            return_value={},
        ) as schwab_get, patch.object(server, "_fetch_all", return_value=[]):
            server.get_latest_prices(None, symbols)

        self.assertEqual(schwab_get.call_args.kwargs["units"], 2)


class RateLimitTests(unittest.TestCase):
    def test_token_bucket_returns_retry_after_when_burst_is_empty(self) -> None:
        limiter = server._TokenBucketLimiter(rate_per_minute=60, capacity=2)
        self.assertIsNone(limiter.consume("user"))
        self.assertIsNone(limiter.consume("user"))
        self.assertGreaterEqual(limiter.consume("user"), 1)

    def test_rate_limit_error_is_structured_and_safe(self) -> None:
        error = server.RateLimitExceeded(
            "schwab_user_daily",
            120,
            limit=100,
            unit="units_per_utc_day",
        )
        self.assertIn('"error":"rate_limit_exceeded"', str(error))
        self.assertIn(
            '"message":"Your daily Schwab market-data allowance has been reached. '
            'Retry in 120 seconds."',
            str(error),
        )
        self.assertIn('"retry_after_seconds":120', str(error))
        self.assertNotIn("token", str(error).lower())

    def test_limit_message_has_safe_fallback_and_singular_retry(self) -> None:
        message = server._limit_message("future_limit_scope", 1)

        self.assertEqual(
            message,
            "The requested operation has reached a usage limit. "
            "Retry in 1 second.",
        )

    def test_general_limit_rejects_repeated_calls_from_one_token(self) -> None:
        token_limiter = server._TokenBucketLimiter(1, 1)
        user_limiter = server._TokenBucketLimiter(60, 5)

        with patch.object(
            server,
            "_MCP_TOKEN_LIMITER",
            token_limiter,
        ), patch.object(
            server,
            "_MCP_USER_LIMITER",
            user_limiter,
        ), patch.object(
            server,
            "MCP_TOKEN_RATE_PER_MINUTE",
            1,
        ):
            server._enforce_general_mcp_limit("user-id", "token-one")
            with self.assertRaises(server.RateLimitExceeded) as raised:
                server._enforce_general_mcp_limit("user-id", "token-one")
            server._enforce_general_mcp_limit("user-id", "token-two")

        self.assertEqual(raised.exception.scope, "mcp_token_minute")

    def test_general_user_limit_aggregates_different_tokens(self) -> None:
        token_limiter = server._TokenBucketLimiter(60, 5)
        user_limiter = server._TokenBucketLimiter(1, 1)

        with patch.object(
            server,
            "_MCP_TOKEN_LIMITER",
            token_limiter,
        ), patch.object(
            server,
            "_MCP_USER_LIMITER",
            user_limiter,
        ), patch.object(
            server,
            "MCP_USER_RATE_PER_MINUTE",
            1,
        ):
            server._enforce_general_mcp_limit("user-id", "token-one")
            with self.assertRaises(server.RateLimitExceeded) as raised:
                server._enforce_general_mcp_limit("user-id", "token-two")

        self.assertEqual(raised.exception.scope, "mcp_user_minute")

    def test_current_user_id_enforces_general_limit_by_database_user(self) -> None:
        with patch.object(
            server,
            "_request_authentication_subject",
            return_value="local:andreySr",
        ), patch.object(
            server,
            "_fetch_one",
            return_value={"UserId": "user-guid"},
        ), patch.object(server, "_enforce_general_mcp_limit") as enforce:
            user_id = server._current_user_id(None)

        self.assertEqual(user_id, "user-guid")
        enforce.assert_called_once_with("user-guid")

    def test_current_user_id_enforces_token_and_user_limits(self) -> None:
        request = type(
            "Request",
            (),
            {
                "scope": {
                    "state": {
                        "user_id": "user-guid",
                        "token_rate_key": "token-guid",
                    }
                }
            },
        )()
        request_context = type(
            "RequestContext",
            (),
            {"request": request},
        )()
        ctx = type("Context", (), {"request_context": request_context})()

        with patch.object(server, "_enforce_general_mcp_limit") as enforce:
            user_id = server._current_user_id(ctx)

        self.assertEqual(user_id, "user-guid")
        enforce.assert_called_once_with("user-guid", "token-guid")


class TokenUsageTests(unittest.TestCase):
    def test_parses_tool_name_without_retaining_arguments(self) -> None:
        body = (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            b'"params":{"name":"get_latest_prices",'
            b'"arguments":{"secret":"must-not-be-logged"}}}'
        )
        self.assertEqual(
            server._parse_mcp_request_metadata(body),
            ("tools/call", "get_latest_prices"),
        )

    def test_client_ip_defaults_to_network_prefix(self) -> None:
        scope = {
            "headers": [(b"cf-connecting-ip", b"203.0.113.42")],
            "client": ("127.0.0.1", 12345),
        }
        with patch.object(server, "MCP_TOKEN_USAGE_IP_MODE", "prefix"):
            address, network = server._client_network_metadata(scope)

        self.assertIsNone(address)
        self.assertEqual(network, "203.0.113.0/24")

    def test_detects_mcp_tool_error_without_storing_response(self) -> None:
        self.assertEqual(
            server._parse_mcp_response_error(
                b'{"jsonrpc":"2.0","id":1,"result":{"isError":true}}'
            ),
            "ToolError",
        )

    def test_usage_insert_contains_metadata_but_not_request_arguments(self) -> None:
        now = datetime.now(timezone.utc)
        identity = server.AuthIdentity(
            authentication_subject="local:user",
            user_id="00000000-0000-0000-0000-000000000001",
            api_token_id="00000000-0000-0000-0000-000000000002",
            token_key="token-id",
        )
        scope = {
            "method": "POST",
            "path": "/mcp",
            "headers": [
                (b"host", b"mcp.wiselinetrade.com"),
                (b"cf-connecting-ip", b"203.0.113.42"),
                (b"cf-ipcountry", b"US"),
                (b"cf-ray", b"test-ray"),
                (b"user-agent", b"Codex/test"),
                (b"mcp-session-id", b"private-session-id"),
            ],
        }
        body = (
            b'{"method":"tools/call","params":{"name":"get_my_accounts",'
            b'"arguments":{"secret":"must-not-be-logged"}}}'
        )
        metrics = server.RequestUsageMetrics(
            schwab_units=2,
            schwab_upstream_requests=1,
            schwab_cache_hits=3,
        )

        with patch.object(server, "_fetch_all", return_value=[]) as fetch_all, patch.object(
            server,
            "MCP_TOKEN_USAGE_IP_MODE",
            "prefix",
        ):
            server._record_api_token_usage(
                identity=identity,
                scope=scope,
                captured_body=body,
                captured_response=b'{"result":{"isError":false}}',
                request_bytes=len(body),
                response_bytes=25,
                response_status=200,
                started_at=now,
                completed_at=now,
                duration_ms=12,
                error_type=None,
                metrics=metrics,
            )

        sql, params = fetch_all.call_args.args
        self.assertIn("invest.RecordApiTokenUsage", sql)
        self.assertEqual(params[8:12], ("tools/call", "get_my_accounts", 200, "Success"))
        self.assertEqual(params[20], None)
        self.assertEqual(params[21], "203.0.113.0/24")
        self.assertEqual(params[22], "US")
        self.assertEqual(params[25], "test-ray")
        self.assertIsInstance(params[26], bytes)
        self.assertNotIn("must-not-be-logged", repr(params))
        self.assertNotIn("private-session-id", repr(params))

    def test_market_hours_rejects_options(self) -> None:
        with patch.object(server, "_current_user_id", return_value="user-1"), self.assertRaisesRegex(
            ValueError, "equity"
        ):
            server.get_market_hours(None, markets=["option"])

    def test_market_hours_and_movers_call_only_read_only_endpoints(self) -> None:
        responses = [
            {"equity": {"isOpen": True}},
            {
                "screeners": [
                    {
                        "symbol": "ABC",
                        "description": "ABC Corporation",
                        "lastPrice": 100,
                        "netPercentChange": 5,
                    }
                ]
            },
        ]
        with patch.object(
            server,
            "_schwab_cached_market_data_get",
            side_effect=responses,
        ) as schwab_get, patch.object(
            server,
            "_current_user_id",
            return_value="user-1",
        ):
            hours = server.get_market_hours(
                None,
                markets=["equity"],
                market_date="2026-08-26",
            )
            movers = server.get_market_movers(None, index_symbol="$SPX")

        self.assertTrue(hours["Markets"]["equity"]["isOpen"])
        self.assertEqual(movers["Movers"][0]["Symbol"], "ABC")
        self.assertEqual(
            [call_args.args[0] for call_args in schwab_get.call_args_list],
            ["/markets", "/movers/$SPX"],
        )


class InstrumentViewSafetyTests(unittest.TestCase):
    def test_server_queries_do_not_read_series_table_directly(self) -> None:
        source = Path(server.__file__).read_text(encoding="utf-8-sig")
        self.assertIsNone(
            re.search(r"\b(?:FROM|JOIN)\s+dbo\.Series\b", source, re.IGNORECASE)
        )
        self.assertIn("FROM invest.McpInstruments", source)
        self.assertIn("JOIN invest.McpInstruments", source)

    def test_migration_filters_internal_series_and_restricts_table(self) -> None:
        migration = (
            Path(server.__file__).parent
            / "sql"
            / "009_create_mcp_instruments_view.sql"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("CREATE OR ALTER VIEW invest.McpInstruments", migration)
        self.assertIn("NOT IN ('TEMP', 'PORTF')", migration)
        view_select = migration.split("AS", 1)[1].split("FROM dbo.Series", 1)[0]
        for excluded_column in {
            "ISymbol",
            "Active",
            "IsWatched",
            "IsTraded",
            "TradePrice",
            "TradeDate",
            "MaxDataDate",
            "MinDataDate",
            "Updated",
        }:
            self.assertNotIn(excluded_column, view_select)
        self.assertIn(
            "DENY SELECT ON OBJECT::dbo.Series TO [mcp_connector]",
            migration,
        )
        self.assertIn(
            "GRANT SELECT ON OBJECT::invest.McpInstruments TO [mcp_connector]",
            migration,
        )

    def test_membership_tools_use_research_procedure_filters(self) -> None:
        with patch.object(
            server,
            "_load_research_rows",
            return_value=[],
        ) as load_research, patch.object(
            server,
            "_current_user_id",
            return_value="user-1",
        ):
            server.get_watched_symbols(None, limit=25)
            server.get_traded_symbols(None, limit=30)

        self.assertEqual(
            load_research.call_args_list,
            [
                call(watched_only=True, limit=25),
                call(traded_only=True, limit=30),
            ],
        )


class TokenUsageMigrationTests(unittest.TestCase):
    def test_usage_migration_is_least_privilege_and_omits_payloads(self) -> None:
        migration = (
            Path(server.__file__).parent
            / "sql"
            / "010_add_api_token_usage_log.sql"
        ).read_text(encoding="utf-8-sig")

        self.assertIn("CREATE TABLE invest.ApiTokenUsageLog", migration)
        self.assertIn("CREATE OR ALTER PROCEDURE invest.RecordApiTokenUsage", migration)
        self.assertIn("CREATE OR ALTER VIEW invest.ApiTokenUsageDaily", migration)
        self.assertIn(
            "GRANT EXECUTE ON OBJECT::invest.RecordApiTokenUsage",
            migration,
        )
        self.assertIn(
            "DENY SELECT, INSERT, UPDATE, DELETE",
            migration,
        )
        self.assertNotIn("RequestBody", migration)
        self.assertNotIn("ResponseBody", migration)
        self.assertNotIn("AuthorizationHeader", migration)


class ActiveTokenLimitMigrationTests(unittest.TestCase):
    def test_issue_token_limits_each_user_to_two_active_tokens(self) -> None:
        migration = (
            Path(server.__file__).parent
            / "sql"
            / "011_limit_active_api_tokens.sql"
        ).read_text(encoding="utf-8-sig")

        self.assertIn("CREATE OR ALTER PROCEDURE invest.IssueApiToken", migration)
        self.assertIn("WITH (UPDLOCK, HOLDLOCK)", migration)
        self.assertIn("RevokedAt IS NULL", migration)
        self.assertIn("ExpiresAt IS NULL OR ExpiresAt > @Now", migration)
        self.assertIn("IF @ActiveTokenCount >= 2", migration)

    def test_issue_token_rejects_case_insensitive_active_name_duplicates(self) -> None:
        migration = (
            Path(server.__file__).parent
            / "sql"
            / "018_reject_duplicate_active_token_names.sql"
        ).read_text(encoding="utf-8-sig")

        self.assertIn("CREATE OR ALTER PROCEDURE invest.IssueApiToken", migration)
        self.assertIn("WITH (UPDLOCK, HOLDLOCK)", migration)
        self.assertIn("SET @TokenName = LTRIM(RTRIM(@TokenName))", migration)
        self.assertIn("TokenName COLLATE Latin1_General_100_CI_AS", migration)
        self.assertIn("RevokedAt IS NULL", migration)
        self.assertIn("ExpiresAt IS NULL OR ExpiresAt > @Now", migration)
        self.assertIn("THROW 50015", migration)
        self.assertIn("IF @ActiveTokenCount >= 2", migration)


class PortalIntegrationMigrationTests(unittest.TestCase):
    def test_portal_contract_is_entitlement_aware_and_least_privilege(self) -> None:
        migration = (
            Path(server.__file__).parent
            / "sql"
            / "012_add_portal_integration.sql"
        ).read_text(encoding="utf-8-sig")

        self.assertIn("CREATE TABLE invest.PortalUserEntitlements", migration)
        self.assertIn("CREATE OR ALTER PROCEDURE invest.Portal_EnsureUser", migration)
        self.assertIn("CREATE OR ALTER PROCEDURE invest.Portal_SetEntitlement", migration)
        self.assertIn("CREATE OR ALTER PROCEDURE invest.Portal_GetPortfolios", migration)
        self.assertIn("CREATE OR ALTER PROCEDURE invest.Portal_GetPortfolio", migration)
        self.assertIn("CREATE OR ALTER PROCEDURE invest.Portal_CreateMcpToken", migration)
        self.assertIn("LEFT JOIN invest.PortalUserEntitlements", migration)
        self.assertIn("entitlement.IsEntitled = 1", migration)
        self.assertIn("CREATE ROLE [investment_portal_runtime]", migration)
        self.assertIn("TO [investment_portal_runtime]", migration)
        self.assertIn("ADD MEMBER [InvestmentPortal_Connector]", migration)
        self.assertIn(
            "DENY SELECT, INSERT, UPDATE, DELETE ON SCHEMA::invest",
            migration,
        )
        self.assertNotIn("GRANT SELECT ON OBJECT::invest.ApiTokens", migration)


class PortalStrategyMigrationTests(unittest.TestCase):
    def test_portal_strategy_contract_is_scoped_and_least_privilege(self) -> None:
        migration = (
            Path(server.__file__).parent
            / "sql"
            / "020_add_portal_strategy_context.sql"
        ).read_text(encoding="utf-8-sig")

        self.assertIn(
            "CREATE OR ALTER PROCEDURE invest.Portal_GetPortfolioStrategies",
            migration,
        )
        self.assertIn("r.UserId = @TradeUserId", migration)
        self.assertIn("r.AccountId = @PortfolioId OR r.AccountId IS NULL", migration)
        self.assertIn("a.OwnerUserId = @TradeUserId", migration)
        self.assertIn("r.RuleJson", migration)
        self.assertIn("TO [investment_portal_runtime]", migration)
        self.assertIn(
            "DENY EXECUTE ON OBJECT::invest.Portal_GetPortfolioStrategies",
            migration,
        )


if __name__ == "__main__":
    unittest.main()
