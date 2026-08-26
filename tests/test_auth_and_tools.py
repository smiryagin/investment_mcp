from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import unittest
from datetime import datetime, timedelta, timezone
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

        asyncio.run(middleware(scope, receive, send))
        self.assertEqual(messages[0]["status"], 401)

    def test_database_token_is_hashed_before_lookup(self) -> None:
        token = "imcp_" + "a" * 64
        with patch.object(
            server,
            "_fetch_one",
            return_value={"AuthenticationSubject": "local:andreySr"},
        ) as fetch_one:
            subject = server._resolve_database_token(token)

        self.assertEqual(subject, "local:andreySr")
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
            self.assertEqual(resolver(token), "local:andreySr")

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


class ToolSchemaTests(unittest.TestCase):
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

            def execute(self, sql, params=()):
                self.statements.append(sql)
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
        ), patch.object(server, "_fetch_all") as fetch_all:
            rows = server.get_latest_prices(["voo"])

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
        with patch.object(server, "_fetch_all", return_value=[]), patch.object(
            server,
            "_schwab_cached_market_data_get",
            return_value=payload,
        ) as schwab_get:
            rows = server.search_symbols("SCHD")

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
        with patch.object(server, "_fetch_all", return_value=[]), patch.object(
            server,
            "_schwab_cached_market_data_get",
            return_value=payload,
        ) as schwab_get:
            rows = server.get_price_history(
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
        with patch.object(
            server,
            "_schwab_market_data_get",
            return_value={"value": [1]},
        ) as provider_get:
            first = server._schwab_cached_market_data_get(
                "/quotes",
                {"symbols": "VOO"},
                ttl_seconds=15,
            )
            first["value"].append(2)
            second = server._schwab_cached_market_data_get(
                "/quotes",
                {"symbols": "VOO"},
                ttl_seconds=15,
            )

        provider_get.assert_called_once()
        self.assertEqual(second, {"value": [1]})

    def test_market_hours_rejects_options(self) -> None:
        with self.assertRaisesRegex(ValueError, "equity"):
            server.get_market_hours(markets=["option"])

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
        ) as schwab_get:
            hours = server.get_market_hours(
                markets=["equity"],
                market_date="2026-08-26",
            )
            movers = server.get_market_movers(index_symbol="$SPX")

        self.assertTrue(hours["Markets"]["equity"]["isOpen"])
        self.assertEqual(movers["Movers"][0]["Symbol"], "ABC")
        self.assertEqual(
            [call_args.args[0] for call_args in schwab_get.call_args_list],
            ["/markets", "/movers/$SPX"],
        )


if __name__ == "__main__":
    unittest.main()
