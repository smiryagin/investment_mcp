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
            "get_my_accounts",
            "get_my_portfolio",
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
