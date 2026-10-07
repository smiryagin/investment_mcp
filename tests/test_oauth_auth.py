from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt

import server


ISSUER = "https://staging.wiselinetrade.com/"
RESOURCE = "https://staging-investments-mcp.wiselinetrade.com/mcp"
JWKS_URI = f"{ISSUER}.well-known/jwks"
SUBJECT = "portal:11111111-2222-3333-4444-555555555555"


class OAuthJwtResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = server.OAuthJwtResolver(
            issuer=ISSUER,
            resource=RESOURCE,
            jwks_uri=JWKS_URI,
            algorithms=("RS256",),
            leeway_seconds=60,
            jwks_cache_seconds=3600,
        )

    def test_validates_jwt_and_current_trade_entitlement(self) -> None:
        claims = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "sub": SUBJECT,
            "scope": "investments.read investments.write",
            "client_id": "https://chatgpt.com/oauth/client.json",
            "iat": 1,
            "exp": 4_102_444_800,
            "jti": "oauth-token-id",
        }
        with patch.object(
            self.resolver.jwks_client,
            "get_signing_key_from_jwt",
            return_value=SimpleNamespace(key="public-key"),
        ), patch.object(server.jwt, "decode", return_value=claims) as decode, patch.object(
            server,
            "_fetch_one",
            return_value={"UserId": "trade-user-id"},
        ) as fetch_one:
            identity = self.resolver.resolve("header.payload.signature")

        self.assertIsNotNone(identity)
        self.assertEqual(identity.authentication_subject, SUBJECT)
        self.assertEqual(identity.user_id, "trade-user-id")
        self.assertEqual(identity.scopes, {"investments.read", "investments.write"})
        self.assertEqual(identity.client_id, "https://chatgpt.com/oauth/client.json")
        self.assertEqual(
            fetch_one.call_args.args,
            (
                "EXEC invest.AuthenticateOAuthSubject @AuthenticationSubject = ?;",
                (SUBJECT,),
            ),
        )
        self.assertEqual(decode.call_args.kwargs["issuer"], ISSUER)
        self.assertEqual(decode.call_args.kwargs["audience"], RESOURCE)
        self.assertEqual(decode.call_args.kwargs["algorithms"], ["RS256"])
        self.assertNotIn("nbf", decode.call_args.kwargs["options"]["require"])
        self.assertTrue(decode.call_args.kwargs["options"]["verify_nbf"])

    def test_rejects_invalid_jwt_without_querying_trade(self) -> None:
        with patch.object(
            self.resolver.jwks_client,
            "get_signing_key_from_jwt",
            return_value=SimpleNamespace(key="public-key"),
        ), patch.object(
            server.jwt,
            "decode",
            side_effect=jwt.InvalidTokenError("invalid"),
        ), patch.object(server, "_fetch_one") as fetch_one:
            identity = self.resolver.resolve("header.payload.signature")

        self.assertIsNone(identity)
        fetch_one.assert_not_called()

    def test_rejects_wrong_audience_and_expired_tokens_without_querying_trade(self) -> None:
        for error in (jwt.InvalidAudienceError("wrong audience"), jwt.ExpiredSignatureError("expired")):
            with self.subTest(error=type(error).__name__), patch.object(
                self.resolver.jwks_client,
                "get_signing_key_from_jwt",
                return_value=SimpleNamespace(key="public-key"),
            ), patch.object(server.jwt, "decode", side_effect=error), patch.object(
                server,
                "_fetch_one",
            ) as fetch_one:
                identity = self.resolver.resolve("header.payload.signature")

            self.assertIsNone(identity)
            fetch_one.assert_not_called()

    def test_rejects_token_without_scopes_before_querying_trade(self) -> None:
        claims = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "sub": SUBJECT,
            "scope": "",
            "client_id": "client",
            "iat": 1,
            "nbf": 1,
            "exp": 4_102_444_800,
            "jti": "oauth-token-id",
        }
        with patch.object(
            self.resolver.jwks_client,
            "get_signing_key_from_jwt",
            return_value=SimpleNamespace(key="public-key"),
        ), patch.object(server.jwt, "decode", return_value=claims), patch.object(
            server,
            "_fetch_one",
        ) as fetch_one:
            identity = self.resolver.resolve("header.payload.signature")

        self.assertIsNone(identity)
        fetch_one.assert_not_called()

    def test_rejects_valid_jwt_when_entitlement_is_inactive(self) -> None:
        claims = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "sub": SUBJECT,
            "scope": "investments.read",
            "client_id": "client",
            "iat": 1,
            "nbf": 1,
            "exp": 4_102_444_800,
            "jti": "oauth-token-id",
        }
        with patch.object(
            self.resolver.jwks_client,
            "get_signing_key_from_jwt",
            return_value=SimpleNamespace(key="public-key"),
        ), patch.object(server.jwt, "decode", return_value=claims), patch.object(
            server,
            "_fetch_one",
            return_value=None,
        ):
            identity = self.resolver.resolve("header.payload.signature")

        self.assertIsNone(identity)


class OAuthScopeTests(unittest.TestCase):
    def test_write_scope_is_enforced_for_oauth_identity(self) -> None:
        request = SimpleNamespace(
            scope={
                "state": {
                    "user_id": "trade-user-id",
                    "oauth_scopes": ["investments.read"],
                    "token_rate_key": "jti",
                }
            }
        )
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))

        with self.assertRaisesRegex(PermissionError, "investments.write"):
            server._current_user_id(context, "investments.write")

    def test_manual_identity_keeps_existing_read_and_write_access(self) -> None:
        request = SimpleNamespace(
            scope={"state": {"user_id": "trade-user-id", "token_rate_key": "manual"}}
        )
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))

        with patch.object(server, "_enforce_general_mcp_limit"):
            user_id = server._current_user_id(context, "investments.write")

        self.assertEqual(user_id, "trade-user-id")


class OAuthResourceServerTests(unittest.TestCase):
    def test_root_oauth_issuer_preserves_canonical_trailing_slash(self) -> None:
        with patch.dict(
            os.environ,
            {"MCP_OAUTH_ISSUER": "https://staging.wiselinetrade.com"},
            clear=False,
        ):
            issuer = server._required_oauth_issuer_url("MCP_OAUTH_ISSUER")

        self.assertEqual(issuer, ISSUER)

    def test_path_oauth_issuer_is_not_modified(self) -> None:
        with patch.dict(
            os.environ,
            {"MCP_OAUTH_ISSUER": "https://identity.example.com/tenant"},
            clear=False,
        ):
            issuer = server._required_oauth_issuer_url("MCP_OAUTH_ISSUER")

        self.assertEqual(issuer, "https://identity.example.com/tenant")

    def test_fastmcp_publishes_path_specific_protected_resource_metadata(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MCP_OAUTH_ENABLED": "true",
                "MCP_OAUTH_ISSUER": ISSUER,
                "MCP_OAUTH_RESOURCE": RESOURCE,
                "MCP_OAUTH_JWKS_URI": JWKS_URI,
                "MCP_TOKEN_AUTH_MODE": "database",
            },
            clear=False,
        ):
            oauth_server = server._create_mcp_server()
            app = oauth_server.streamable_http_app()

        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/mcp", paths)
        self.assertIn("/.well-known/oauth-protected-resource/mcp", paths)

    def test_tools_advertise_read_and_write_oauth_scopes(self) -> None:
        tools = server.mcp._tool_manager._tools
        original_meta = {name: tool.meta for name, tool in tools.items()}
        try:
            with patch.dict(os.environ, {"MCP_OAUTH_ENABLED": "true"}, clear=False):
                server._attach_oauth_tool_metadata()

            read_scheme = tools["get_my_accounts"].meta["securitySchemes"][0]
            write_scheme = tools["create_account"].meta["securitySchemes"][0]
            self.assertEqual(read_scheme["scopes"], ["investments.read"])
            self.assertEqual(
                write_scheme["scopes"],
                ["investments.read", "investments.write"],
            )
            self.assertTrue(tools["get_my_profile"].meta["openai/profile"])
        finally:
            for name, meta in original_meta.items():
                tools[name].meta = meta

    def test_oauth_trade_migration_is_least_privilege(self) -> None:
        sql_path = Path(__file__).parents[1] / "sql" / "019_add_oauth_subject_authentication.sql"
        sql = sql_path.read_text(encoding="utf-8")
        self.assertIn("CREATE OR ALTER PROCEDURE invest.AuthenticateOAuthSubject", sql)
        self.assertIn("GRANT EXECUTE ON OBJECT::invest.AuthenticateOAuthSubject", sql)
        self.assertNotIn("GRANT SELECT ON OBJECT::invest.PortalUserEntitlements", sql)


if __name__ == "__main__":
    unittest.main()
