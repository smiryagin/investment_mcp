from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
