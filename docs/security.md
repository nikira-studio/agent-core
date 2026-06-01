# Security

Agent Core is built around one core principle: **agents never receive raw secrets.** They get references. The actual values travel through exactly one controlled path at runtime.

This document explains how that works, what other protections are in place, and what to do before exposing Agent Core beyond your local machine.

---

## How Secrets Are Stored

When you store a credential, the value is encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) before it's written to the database. The encryption key lives at `data/credential.key`, and a history of all keys (used after rotation) is in `data/credential.keyring`.

Normal API responses for credentials include metadata and the `AC_SECRET_*` reference name. The raw `value` field is never included.

Credentials are managed from the **Connectors** page or the `/api/credentials` API. Editing a credential only replaces the encrypted secret when a new value is supplied; metadata edits keep the current encrypted value.

**The only controlled ways a raw secret leaves Agent Core:**

1. A user with permission calls the reveal endpoint from an authenticated session.
2. The Credential Broker calls the internal resolution endpoint with a valid broker credential and an authorized agent identity.
3. A connector binding resolves the credential server-side so Agent Core can call the external service directly.

In the connector case, the raw secret stays inside the Agent Core process and is not returned to the agent.

Full backup exports are encrypted with a separate one-time backup key. The encrypted archive and the backup key together can restore your credentials, so treat both as sensitive data.

---

## How Identities Are Stored

| Identity | Storage method |
| --- | --- |
| User passwords | bcrypt hash |
| Agent API keys | SHA-256 hash only — shown once at creation and never stored |
| Broker credentials | SHA-256 hash in the database; plaintext in `data/broker.credential` |

If an API key or broker credential is lost, rotate it — there's no way to recover the original.

---

## Scope Model

Scopes are the permission boundary for everything in Agent Core: memory, credentials, activity records, and briefings. Every operation is checked against the caller's scopes.

| Scope | What it covers |
| --- | --- |
| `user:<id>` | A specific user's personal space — memory and credentials they own |
| `agent:<id>` | An agent's own private working space. This scope is intentionally private; other agent keys should not expect to read it unless you explicitly grant access. |
| `workspace:<id>` | A shared space for project-level collaboration across users or tools. The workspace record is still owned by one user for dashboard/admin visibility, but agents can be granted `workspace:<id>` access to collaborate across users. |
| `shared` | Intentionally global — any agent with the right grant can read from here |

Agents have separate `read_scopes` and `write_scopes`. An agent can read from `shared` without being able to write to it. Writing to `shared` requires an explicit grant in `write_scopes`, or the agent's ID must be in `AGENT_CORE_SHARED_SCOPE_AGENTS`.

A few other things worth knowing:

- **Inactive agents can't authenticate.** Rotating or deactivating an agent immediately revokes access.
- **Inactive workspaces stop authorizing access.** Deactivating a workspace makes `workspace:<id>` invalid for reads and writes.
- **Use workspace scopes for collaboration, not personal scopes.** The `agent:<id>` scope is private scratch space — don't use it as a handoff channel. For cross-agent work, use workspace scopes and keep activity and briefing records current. Activity is a mailroom-style handoff board, not an orchestrator: a human or agent can leave work there, but the receiving agent still has to explicitly check for and claim it. Other users do not automatically see a shared workspace row in their own dashboard; they see the shared capability through the agent scopes you grant.
- **Share workspaces with users, not with agent identities.** Workspace membership is the source of truth for collaboration. Once a user is added as a collaborator, they can grant their own agents `workspace:<id>` access. Revoking collaboration should immediately stop new runtime access even if an old agent scope is still present.

---

## The Credential Broker Flow

Here's what happens from the time you store a secret to when a tool actually uses it:

1. You store a secret in Agent Core.
2. Agent Core returns an `AC_SECRET_*` reference — not the secret.
3. The agent's tool configuration includes that reference.
4. At runtime, the local Credential Broker intercepts the reference.
5. The broker calls Agent Core's internal `/internal/credentials/resolve` endpoint.
6. Agent Core checks the broker credential and verifies the agent has read access to the credential's scope.
7. The broker injects the raw value into the child process environment.

The model's context window never contains your actual secret. It's injected at execution time, on your machine, by the broker.

> The `agent_id` for resolution must come from your integration config — not from model-generated text. The model should never be able to influence which agent identity resolves a secret.

---

## Connector Execution Flow

Connectors are the server-side path for external actions:

1. You create a stored credential.
2. You create a connector binding that points to that credential and has its own scope.
3. An agent calls `connectors_run` with a binding ID, action, and parameters.
4. Agent Core verifies the agent can read the binding scope.
5. Agent Core resolves the credential internally, calls the external service, logs the execution, and returns the result.

Credential scope controls access to the stored secret. Binding scope controls where the connector is available. In normal workspace use, set both to the same workspace.

---

## PII Protection on Shared Memory

Any write to the `shared` memory scope is automatically scanned for content that looks like:

- Email addresses
- Phone numbers
- API keys and tokens (including `ac_sk_`, `ghp_`, `sk-`, and similar patterns)
- Social Security Numbers

If a match is found, the write is rejected with `422 PII_DETECTED`. This prevents personal data and secrets from accidentally landing in the shared scope where any authorized agent can read them. Agent-private memory (`agent:<id>`) doesn't have this restriction.

The same protection applies to `/api/memory/import`. Imports are manual writes from curated text or markdown notes into the existing memory table, and shared-scope imports are scanned before records are created.

Search queries that look like credentials, or are too short/noisy (single stop words, punctuation-only strings), are also rejected before FTS execution.

---

## Rate Limits

Rate limits protect against runaway agents and misconfigured automation. Limits are in-memory token buckets that reset on process restart.

| Operation | Limit |
| --- | --- |
| Memory writes | 60 per minute per agent |
| Memory searches | 60 per minute per agent |
| Concurrent memory searches | 5 per agent |
| Credential creates | 10 per minute per user |
| Failed OTP attempts | 5 per 5 minutes per user |

Rate-limited responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers. `X-RateLimit-Reset` is a Unix timestamp.

---

## Rotating the Credential Encryption Key

If you suspect your encryption key has been exposed — or just as a routine security practice — you can rotate it. This generates a new key, re-encrypts all credential entries, and keeps the old key in the keyring so nothing breaks.

**From the dashboard:** Settings → Encryption Key → Rotate Encryption Key (requires admin access)

**Via the API:**

```bash
curl -X POST http://localhost:3500/api/credentials/rotate \
  -H "Authorization: Bearer <admin-session>" \
  -H "Content-Type: application/json" \
  -d '{}'
```

What happens during rotation:

1. The current primary key is backed up to `data/backups/credential.pre_rotation.{timestamp}.bak`
2. A new primary key is generated
3. The new key is prepended to the keyring, which stores all historical keys
4. All credential entries are re-encrypted with the new key
5. A `credential_key_rotated` audit event is written

After rotation, older entries that were encrypted with previous keys still decrypt correctly — the keyring tries keys in order, most recent first.

**Restoring a previous key** (e.g., after a bad rotation or from a backup):

```bash
curl -X POST http://localhost:3500/api/credentials/restore-key \
  -H "Authorization: Bearer <admin-session>" \
  -H "Content-Type: application/json" \
  -d '{"key_base64": "<base64-encoded-fernet-key>"}'
```

This replaces the current keyring with a single-key keyring containing the restored key. All credential entries must be decryptable by that key. A `credential_key_restored` audit event is written.

> Only restore a key if you know it can decrypt your existing credential entries. Restoring a key that can't decrypt your data will leave those credentials inaccessible.

---

## Webhooks

Inbound webhook commands are authenticated with a dedicated installation-wide inbound key in the `X-Agent-Core-Inbound-Key` header. They are not signed deliveries like outbound webhooks.

Outbound webhooks let external systems receive signed notifications when events occur in Agent Core.

**Admin-only.** Only admin users can create, edit, or delete webhook registrations. Agent API keys and non-admin sessions have no access to webhook configuration or secrets.

**Secrets are encrypted at rest.** Webhook secrets are encrypted using the same Fernet key used for credentials. They are never returned in API responses — they are write-only on create and update.

**Delivery is fire-and-log-only (v1).** If a receiver is down or slow, the failure is recorded in the delivery log and no retry is attempted. Keep receiver endpoints fast and reliable.

**SSRF protection.** Webhook URLs are validated against the same SSRF guard as other outbound URLs. Private network addresses (RFC 1918, loopback, link-local) are allowed by default for local and operator-managed deployments. If you want to block them, set `AGENT_CORE_BLOCK_INTERNAL_HOSTS=true` and use `AGENT_CORE_ALLOWED_INTERNAL_HOSTS` as an exception list.

**Signature verification.** Every delivery includes `X-Agent-Core-Signature: sha256=<hex>`. Receivers should verify this before acting on the payload:

```python
import hashlib, hmac

def verify(body: bytes, secret: str, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

**Manual pruning.** Audit and activity history can be manually pruned by an admin from the dashboard using a cutoff date. This is an explicit maintenance action, not an automatic retention job, and it only deletes the targeted historical rows.

---

## Deployment Checklist

Before making Agent Core accessible beyond localhost:

- [ ] Keep `data/`, `.env`, encrypted backup archives, backup keys, and `private/` out of version control
- [ ] Enroll TOTP on your admin account if you want login MFA
- [ ] Set `AGENT_CORE_COOKIE_SECURE=true` if Agent Core is served over HTTPS or behind a TLS proxy
- [ ] Set `AGENT_CORE_CORS_ORIGINS` to specific origins if browser clients need authenticated access
- [ ] Set `AGENT_CORE_ALLOWED_IPS` if Agent Core is reachable on a LAN or shared network
- [ ] Restrict filesystem permissions on `data/credential.key` and `data/broker.credential`
- [ ] Rotate the broker credential after any suspected exposure
- [ ] Rotate webhook secrets if any are suspected to be exposed
- [ ] Review the audit log after sensitive operations: credential reveals, backup/restore, key rotation, connector execution
