"""Validate the public metadata required for an OAuth-protected MCP server.

This is a deployment/readiness probe. It never sends credentials and only performs
GET requests to public well-known metadata endpoints.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


MAX_METADATA_BYTES = 1_048_576


@dataclass(frozen=True)
class ProbeResult:
    errors: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _https_url(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{field} must be a string.")
        return
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        errors.append(f"{field} must be an absolute HTTPS URL without a fragment.")


def build_protected_resource_metadata_url(resource_url: str) -> str:
    parsed = urlparse(resource_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        raise ValueError("resource_url must be an absolute HTTPS URL without a fragment")
    resource_path = parsed.path.lstrip("/")
    metadata_path = "/.well-known/oauth-protected-resource"
    if resource_path:
        metadata_path += f"/{resource_path}"
    return urlunparse((parsed.scheme, parsed.netloc, metadata_path, "", parsed.query, ""))


def build_authorization_server_metadata_url(issuer_url: str) -> str:
    parsed = urlparse(issuer_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment or parsed.query:
        raise ValueError("issuer_url must be an absolute HTTPS URL without query or fragment")
    issuer_path = parsed.path.rstrip("/")
    metadata_path = "/.well-known/oauth-authorization-server"
    if issuer_path:
        metadata_path += issuer_path
    return urlunparse((parsed.scheme, parsed.netloc, metadata_path, "", "", ""))


def validate_protected_resource_metadata(
    payload: dict[str, Any],
    *,
    expected_resource: str,
    expected_issuer: str,
    required_scopes: set[str],
) -> ProbeResult:
    errors: list[str] = []
    if payload.get("resource") != expected_resource:
        errors.append("protected-resource resource does not exactly match the expected MCP URL.")

    servers = payload.get("authorization_servers")
    if not isinstance(servers, list) or expected_issuer not in servers:
        errors.append("protected-resource authorization_servers does not contain the exact issuer.")

    scopes = payload.get("scopes_supported")
    if not isinstance(scopes, list) or not required_scopes.issubset(set(scopes)):
        errors.append("protected-resource scopes_supported is missing a required WiseLine scope.")

    methods = payload.get("bearer_methods_supported")
    if not isinstance(methods, list) or "header" not in methods:
        errors.append("protected-resource bearer_methods_supported must include header.")

    return ProbeResult(tuple(errors))


def validate_authorization_server_metadata(
    payload: dict[str, Any],
    *,
    expected_issuer: str,
    required_scopes: set[str],
    allow_preconfigured_client: bool = False,
) -> ProbeResult:
    errors: list[str] = []
    if payload.get("issuer") != expected_issuer:
        errors.append("authorization-server issuer does not exactly match the expected issuer.")

    for field in ("authorization_endpoint", "token_endpoint", "revocation_endpoint", "jwks_uri"):
        _https_url(payload.get(field), field, errors)

    response_types = payload.get("response_types_supported")
    if not isinstance(response_types, list) or "code" not in response_types:
        errors.append("response_types_supported must include code.")

    grant_types = payload.get("grant_types_supported")
    if not isinstance(grant_types, list) or not {
        "authorization_code",
        "refresh_token",
    }.issubset(set(grant_types)):
        errors.append("grant_types_supported must include authorization_code and refresh_token.")

    challenge_methods = payload.get("code_challenge_methods_supported")
    if not isinstance(challenge_methods, list) or "S256" not in challenge_methods:
        errors.append("code_challenge_methods_supported must include S256.")

    token_auth_methods = payload.get("token_endpoint_auth_methods_supported")
    if not isinstance(token_auth_methods, list) or "none" not in token_auth_methods:
        errors.append("token_endpoint_auth_methods_supported must include none for public clients.")

    if payload.get("authorization_response_iss_parameter_supported") is not True:
        errors.append("authorization_response_iss_parameter_supported must be true.")

    has_cimd = payload.get("client_id_metadata_document_supported") is True
    registration_endpoint = payload.get("registration_endpoint")
    if not has_cimd and not isinstance(registration_endpoint, str) and not allow_preconfigured_client:
        errors.append(
            "authorization server must advertise CIMD or a registration_endpoint, "
            "or the probe must be told that this host uses a preconfigured client."
        )
    if isinstance(registration_endpoint, str):
        _https_url(registration_endpoint, "registration_endpoint", errors)

    scopes = payload.get("scopes_supported")
    expected_scopes = required_scopes | {"offline_access"}
    if not isinstance(scopes, list) or not expected_scopes.issubset(set(scopes)):
        errors.append("authorization-server scopes_supported is missing a required scope.")

    return ProbeResult(tuple(errors))


def _fetch_json(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "WiseLineOAuthProbe/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - operator-provided HTTPS URL
        content_type = response.headers.get_content_type()
        if content_type not in {"application/json", "application/oauth-authz-req+jwt"}:
            raise ValueError(f"{url} returned unexpected content type {content_type!r}")
        body = response.read(MAX_METADATA_BYTES + 1)
        if len(body) > MAX_METADATA_BYTES:
            raise ValueError(f"{url} returned more than {MAX_METADATA_BYTES} bytes")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError(f"{url} did not return a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True, help="Exact public MCP resource URL")
    parser.add_argument("--issuer", required=True, help="Exact OAuth issuer URL")
    parser.add_argument("--resource-metadata-url")
    parser.add_argument("--authorization-metadata-url")
    parser.add_argument(
        "--preconfigured-client",
        action="store_true",
        help="Accept an authorization server where the AI host is registered out of band",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    resource_metadata_url = args.resource_metadata_url or build_protected_resource_metadata_url(args.resource)
    authorization_metadata_url = args.authorization_metadata_url or build_authorization_server_metadata_url(args.issuer)
    required_scopes = {"investments.read", "investments.write"}

    try:
        resource_metadata = _fetch_json(resource_metadata_url, args.timeout)
        authorization_metadata = _fetch_json(authorization_metadata_url, args.timeout)
    except Exception as exc:
        print(f"OAuth metadata probe failed: {exc}", file=sys.stderr)
        return 1

    errors = [
        *validate_protected_resource_metadata(
            resource_metadata,
            expected_resource=args.resource,
            expected_issuer=args.issuer,
            required_scopes=required_scopes,
            allow_preconfigured_client=args.preconfigured_client,
        ).errors,
        *validate_authorization_server_metadata(
            authorization_metadata,
            expected_issuer=args.issuer,
            required_scopes=required_scopes,
        ).errors,
    ]
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("OAuth metadata is compatible with the WiseLine MCP contract.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
