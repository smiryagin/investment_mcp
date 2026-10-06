# Investment MCP OAuth resource-server design

Status: design spike. No runtime route or database has been changed.

The portal-side protocol decision is documented in the `investment_portal` repository's `docs/MCP_OAUTH_DESIGN.md`. This document defines the Investment MCP half of that contract.

## Responsibility

Investment MCP is an OAuth resource server, not an authorization server. It must:

- advertise OAuth protected-resource metadata;
- validate portal-issued access tokens;
- enforce scopes and current Trade entitlement;
- expose the authenticated profile to supported AI hosts;
- preserve current request quotas, concurrency limits, telemetry, and ownership checks;
- continue accepting existing database-backed `imcp_...` tokens during migration.

It must never issue OAuth tokens, render a login page, or connect to the WiseLinePortal database.

## SDK integration

The current requirement `mcp[cli]>=1.27,<2` resolves to the maintained 1.x SDK line. Before runtime implementation it should be raised to at least the latest supported 1.x release and pinned through a lock/deployment artifact. The supported 1.x FastMCP API already provides:

- `TokenVerifier` for resource-server token validation;
- `AuthSettings` for issuer, resource URL, required scopes, and resource validation;
- RFC 9728 protected-resource metadata;
- an OAuth-aware `WWW-Authenticate` response;
- authenticated request context.

The implementation should use these supported APIs instead of continuing to implement the OAuth challenge/discovery layer in the custom `BearerAuthASGI` middleware. WiseLine's existing quota, concurrency, usage logging, and Trade identity behavior remains a separate middleware/service around the authenticated identity.

## Public metadata

For resource `https://investments-mcp.torusystems.com/mcp`, the server publishes:

`GET https://investments-mcp.torusystems.com/.well-known/oauth-protected-resource/mcp`

with the equivalent of:

```json
{
  "resource": "https://investments-mcp.torusystems.com/mcp",
  "authorization_servers": ["https://wiselinetrade.com"],
  "scopes_supported": ["investments.read", "investments.write"],
  "bearer_methods_supported": ["header"]
}
```

An unauthenticated or invalid request to `/mcp` returns `401` with a `WWW-Authenticate: Bearer` challenge containing a `resource_metadata` link to that document.

Staging must use a different resource URL and the staging portal issuer. Do not advertise the staging issuer for the production MCP URL.

## Composite authentication

During transition, bearer credentials have two independent validation paths:

1. An opaque `imcp_...` token is hashed and resolved through `invest.AuthenticateApiToken`, exactly as today.
2. A compact JWT is validated using the portal issuer's discovery document/JWKS.

JWT validation must require:

- an approved signing algorithm (initially `RS256`);
- a known `kid` from the cached JWKS, with safe refresh on key rollover;
- exact `iss`;
- exact MCP `aud`;
- valid `nbf`, `iat`, and `exp` with a small clock-skew allowance;
- required scope for the invoked tool;
- a nonempty `sub` in the form `portal:<guid>`.

Unknown token formats fail closed. If the authorization server or JWKS is temporarily unavailable, already cached valid keys may be used until their configured cache limit; an unknown signing key must not fall back to the opaque-token database path.

## Trade identity and entitlement

The JWT `sub` matches the existing Trade `invest.Users.AuthenticationSubject` value created by `invest.Portal_EnsureUser`:

```text
portal:<lowercase PortalUserId>
```

After cryptographic validation, the MCP server resolves the subject through a least-privilege Trade stored procedure that also checks:

- `invest.Users.IsActive = 1`;
- a matching `invest.PortalUserEntitlements` row;
- `IsEntitled = 1`;
- `EntitledThrough` is null or in the future.

The procedure returns the `TradeUserId` used by all existing ownership checks. This database check occurs for every authenticated request (or through a very short, revocation-aware cache) so cancellation and trial expiration are not delayed until JWT expiration.

The new stored procedure is a later, separately reviewed Trade migration. It is not part of this spike.

## Tool scopes

Every tool will declare its OAuth policy in MCP tool metadata:

- all current read-only tools: `investments.read`;
- all current write and destructive-record tools: `investments.write` (and normally `investments.read`);
- a new stable `get_my_profile` tool: `investments.read`, read-only, marked with `_meta["openai/profile"] = true`.

`trading.execute` is reserved and must not be advertised until a tool can submit a real broker order.

The server enforces scope in code; metadata is not authorization. Missing scope produces a structured authorization failure and the required `_meta["mcp/www_authenticate"]` challenge so hosts can request incremental consent.

## Profile tool

`get_my_profile` returns only stable account-identification information needed to distinguish connected accounts, for example:

```json
{
  "id": "<TradeUserId>",
  "name": "Andrey",
  "email": "masked-or-omitted",
  "subscription_status": "trialing"
}
```

The final schema will follow the OpenAI profile-tool contract. Do not return portfolio positions, tokens, provider customer IDs, payment details, or full subscription history.

## Configuration planned for implementation

```text
MCP_OAUTH_ENABLED=true
MCP_OAUTH_ISSUER=https://wiselinetrade.com
MCP_OAUTH_RESOURCE=https://investments-mcp.torusystems.com/mcp
MCP_OAUTH_JWKS_URI=https://wiselinetrade.com/.well-known/jwks
MCP_OAUTH_ALLOWED_ALGORITHMS=RS256
MCP_OAUTH_CLOCK_SKEW_SECONDS=60
MCP_OAUTH_JWKS_CACHE_SECONDS=3600
MCP_OAUTH_REQUIRED_READ_SCOPE=investments.read
MCP_OAUTH_REQUIRED_WRITE_SCOPE=investments.write
```

`MCP_TOKEN_AUTH_MODE=database` remains the legacy-token control during transition. OAuth and opaque-token acceptance should be controlled independently so OAuth can be disabled without breaking existing users.

No private signing key or portal database credential belongs on the MCP host.

## Failure behavior

| Condition | Result |
| --- | --- |
| no bearer token | `401` + protected-resource metadata challenge |
| malformed/unknown token | `401 invalid_token` |
| bad signature/issuer/audience/expiry | `401 invalid_token` |
| valid token, missing tool scope | `403 insufficient_scope` + scope challenge |
| valid token, inactive entitlement | `403` with a subscription/actionable message |
| temporary Trade authentication failure | `503`, never anonymous fallback |
| quota/concurrency exhausted | existing `429` behavior |

No response or log may include the bearer token, authorization code, refresh token, or raw JWT claims beyond the approved audit fields.

## Verification plan

1. Unit-test metadata and challenge documents.
2. Generate a test RSA key pair and test valid, expired, wrong-issuer, wrong-audience, wrong-scope, and unknown-`kid` JWTs.
3. Test key rollover with old and new JWKS entries.
4. Test current entitlement, expired trial, cancellation, and disabled Trade user.
5. Run all existing API-token, rate-limit, concurrency, telemetry, and ownership tests unchanged.
6. Run the included `scripts/oauth_metadata_probe.py` against staging.
7. Test end to end from Codex, ChatGPT, and Claude before changing the portal's default instructions.

## Safe migration order

1. Deploy MCP support with OAuth disabled; legacy database tokens remain active.
2. Publish staging portal OAuth endpoints and staging MCP metadata.
3. Enable hybrid acceptance on staging.
4. Complete real client connections and revocation tests.
5. Enable production OAuth while retaining manual tokens.
6. Make OAuth the portal default only after client-compatibility acceptance passes.
