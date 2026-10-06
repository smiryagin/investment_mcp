from __future__ import annotations

import unittest

from scripts.oauth_metadata_probe import (
    build_authorization_server_metadata_url,
    build_protected_resource_metadata_url,
    validate_authorization_server_metadata,
    validate_protected_resource_metadata,
)


RESOURCE = "https://investments-mcp.torusystems.com/mcp"
ISSUER = "https://wiselinetrade.com"
SCOPES = {"investments.read", "investments.write"}


class OAuthMetadataProbeTests(unittest.TestCase):
    def test_builds_path_specific_protected_resource_url(self) -> None:
        self.assertEqual(
            build_protected_resource_metadata_url(RESOURCE),
            "https://investments-mcp.torusystems.com/.well-known/oauth-protected-resource/mcp",
        )

    def test_builds_metadata_url_for_root_issuer(self) -> None:
        self.assertEqual(
            build_authorization_server_metadata_url(ISSUER),
            "https://wiselinetrade.com/.well-known/oauth-authorization-server",
        )

    def test_accepts_expected_metadata_contract(self) -> None:
        protected = validate_protected_resource_metadata(
            {
                "resource": RESOURCE,
                "authorization_servers": [ISSUER],
                "scopes_supported": sorted(SCOPES),
                "bearer_methods_supported": ["header"],
            },
            expected_resource=RESOURCE,
            expected_issuer=ISSUER,
            required_scopes=SCOPES,
        )
        authorization = validate_authorization_server_metadata(
            {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/connect/authorize",
                "token_endpoint": f"{ISSUER}/connect/token",
                "revocation_endpoint": f"{ISSUER}/connect/revoke",
                "jwks_uri": f"{ISSUER}/.well-known/jwks",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "authorization_response_iss_parameter_supported": True,
                "client_id_metadata_document_supported": True,
                "scopes_supported": [*sorted(SCOPES), "offline_access"],
            },
            expected_issuer=ISSUER,
            required_scopes=SCOPES,
        )

        self.assertTrue(protected.is_valid, protected.errors)
        self.assertTrue(authorization.is_valid, authorization.errors)

    def test_rejects_wrong_audience_and_missing_security_features(self) -> None:
        protected = validate_protected_resource_metadata(
            {
                "resource": "https://wrong.example/mcp",
                "authorization_servers": [ISSUER],
                "scopes_supported": ["investments.read"],
                "bearer_methods_supported": ["query"],
            },
            expected_resource=RESOURCE,
            expected_issuer=ISSUER,
            required_scopes=SCOPES,
        )
        authorization = validate_authorization_server_metadata(
            {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/connect/authorize",
                "token_endpoint": f"{ISSUER}/connect/token",
                "revocation_endpoint": f"{ISSUER}/connect/revoke",
                "jwks_uri": f"{ISSUER}/.well-known/jwks",
                "response_types_supported": ["token"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["plain"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                "authorization_response_iss_parameter_supported": False,
                "scopes_supported": ["investments.read"],
            },
            expected_issuer=ISSUER,
            required_scopes=SCOPES,
        )

        self.assertFalse(protected.is_valid)
        self.assertGreaterEqual(len(protected.errors), 3)
        self.assertFalse(authorization.is_valid)
        self.assertGreaterEqual(len(authorization.errors), 6)

    def test_accepts_preconfigured_client_without_claiming_cimd_or_dcr(self) -> None:
        authorization = validate_authorization_server_metadata(
            {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/connect/authorize",
                "token_endpoint": f"{ISSUER}/connect/token",
                "revocation_endpoint": f"{ISSUER}/connect/revoke",
                "jwks_uri": f"{ISSUER}/.well-known/jwks",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "authorization_response_iss_parameter_supported": True,
                "scopes_supported": [*sorted(SCOPES), "offline_access"],
            },
            expected_issuer=ISSUER,
            required_scopes=SCOPES,
            allow_preconfigured_client=True,
        )

        self.assertTrue(authorization.is_valid, authorization.errors)


if __name__ == "__main__":
    unittest.main()
