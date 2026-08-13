# Delegated Authorization Integration Contract

> Status: implemented and released on Agent Core `main`. This document is the stable integration contract; recursive delegation, ephemeral grant-only identities, and general budgets are not part of this version.

This contract is for any external coordinator or agent runtime. Agent Core remains the authority for identity, durable context, credentials, connector execution, delegation, and audit. The consumer remains responsible for task coordination and runtime behavior.

## Authentication and grant transport

Every worker request authenticates normally:

```http
Authorization: Bearer ac_sk_<recipient-agent-key>
```

A delegated request additionally sends the one-time-claimed credential only in:

```http
X-Agent-Core-Grant: ac_dg_<grant-id>.<opaque-secret>
```

The grant credential is rejected in JSON bodies and MCP arguments. It must not be stored in prompts, task metadata, activity records, logs, or connector parameters. A grant secret is returned exactly once by the recipient-authenticated REST claim endpoint.

## Permission model

Scopes are addresses, not blanket authority. A grant contains normalized permissions:

- scope permission: `resource_type`, `operation`, and exact `scope`; v1 supports memory and briefing operations;
- exact resource permission: `resource_type`, `operation`, and `resource_id`; v1 activities are exact-resource only;
- connector permission: exact `binding_id` and exact `action`.

Credential-reference access is not delegable in v1. Connector execution may resolve a binding credential internally without exposing its reference or value.

When the grant header is present, grant authority replaces the recipient agent's permanent authority. It is never unioned with permanent scopes. Principal, issuer, recipient, coordinator, expiry, revocation, current upstream authority, resource assignment, binding state, connector state, and action availability are revalidated on every request.

## Direct grant lifecycle

1. An authenticated user or an agent with `can_delegate=true` calls `POST /api/delegations`.
2. Agent Core checks that every requested permission is a subset of the issuer's current authority.
3. The recipient calls `POST /api/delegations/{grant_id}/claim` using its normal agent key and no grant header.
4. Agent Core returns `grant_secret` once and activates the grant.
5. The recipient sends its agent key plus `X-Agent-Core-Grant` for delegated operations.
6. An authorized issuer, recipient, or administrator may call `POST /api/delegations/{grant_id}/revoke`.

Safe lifecycle inspection is available through:

- `GET /api/delegations`
- `GET /api/delegations/{grant_id}`
- `GET /api/auth/effective-authority`

Grant TTL is capped at one hour. Claim windows are capped at five minutes. The implementation does not expose `max_uses`; therefore no consumer should send or depend on a usage limit.

## Request and approval lifecycle

An unprivileged coordinator can request authority without possessing it:

1. `POST /api/delegation-requests` records a pending request. For an agent requester, coordinator attribution is server-derived from the authenticated agent.
2. An eligible human or delegation-capable agent calls `POST /api/delegation-requests/{id}/approve`, optionally replacing any permission list with a strict subset.
3. Approval produces an `approved_unclaimed` direct grant. It never returns a secret.
4. The recipient uses the normal one-time claim flow.

Requests can be inspected with `GET /api/delegation-requests` and `GET /api/delegation-requests/{id}`. Denial uses `POST /api/delegation-requests/{id}/deny`. Decisions are one-time.

## MCP parity

The stateless `/mcp` transport rebuilds authority for every HTTP request. Send the same two authentication headers on every delegated MCP call. Supported lifecycle and inspection tools are:

- `effective_authority`
- `delegations_list`
- `delegation_request`
- `delegation_requests_list`
- `delegation_request_approve`
- `delegation_request_deny`
- `delegation_revoke`

Claim is intentionally REST-only because returning the secret as an MCP tool result would make it model-visible.

## Deterministic connector resolution

Use `POST /api/connector-bindings/resolve` or MCP `connectors_resolve` with `connector_type_id` and optional `logical_alias`, `scope`, and `action`.

Resolution first removes unauthorized candidates. It then selects an exact alias, the sole candidate, a unique preferred candidate, or a uniquely highest-priority candidate. Otherwise it returns `AMBIGUOUS_BINDING`; it never guesses across scopes. Under delegation, an action resolves only against the exact binding/action rows in the grant.

Bindings may define `endpoint_url_override` for native MCP deployments. Agent Core validates the URL and requires strict normalized equality of tool names, input schemas, and annotations with the connector type's stored capability contract. Incompatible endpoints require a separate connector type.

## Capability policy metadata

REST connector tool lists and MCP `connectors_actions_list` return `capability_policy` (advisory metadata such as risk, idempotency, approval requirement, expected latency, sensitivity, purpose, and tags) plus `authorization` with the effective required scope operation and its source.

Imported specifications, manifests, and MCP annotations may supply advisory metadata, but cannot make an unknown or non-HTTP-read action read-only. Unknown actions require write scope. Administrators may save a per-action operator policy through `PUT /api/connector-types/{id}/actions`, including `authorization_class: "read" | "write"`; this is the only supported path that can classify a non-obvious action as read-only. Changes are audited.

## Expected integration errors

- `DELEGATION_EXCEEDS_AUTHORITY`: approval or direct issuance exceeds current issuer authority.
- `GRANT_NOT_CLAIMABLE`: claim expired or was already used.
- `INVALID_GRANT`: malformed credential or recipient mismatch.
- `GRANT_INACTIVE`, `GRANT_EXPIRED`, `GRANT_INVALIDATED`: authoritative state or upstream authority blocks use.
- `GRANT_HEADER_REQUIRED`: a grant credential appeared in JSON or MCP arguments.
- `APPROVAL_EXPANDS_REQUEST`: approval attempted to add authority.
- `AMBIGUOUS_BINDING`: authorized resolution has no deterministic winner.
- `SCOPE_DENIED`: the resource operation or binding/action is not in effective authority.

Consumers should treat all unknown errors, resource types, operations, and actions as denied.

## Audit and recovery behavior

Delegated connector executions record principal, issuer, coordinator, executor, grant, correlation ID, and authorization mode. Delegated memory writes carry the same server-derived attribution in provenance. Denied delegated connector executions write a safe blocked audit event without credential material.

Backups clear grant hashes and force claimable or active grants to a revoked state. Replace restore cannot reactivate authority, merge restore omits grant/request tables, and hard purge removes or invalidates dependent authorization rows.
