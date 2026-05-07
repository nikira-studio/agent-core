# Security

Agent Core is built around one rule: **agents may receive stable credential references, but they should never receive raw secrets through normal memory, vault listing, dashboard, or MCP paths.**

This document explains how that's enforced and what you need to do to keep your deployment secure.

---

## How Secrets Are Stored

Vault values are encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) before they're written to the database. The primary encryption key lives at `data/vault.key`. A history of all keys is stored in `data/vault.keyring` (JSON array), allowing decryption of older entries after key rotation.

Normal vault API responses include metadata, masked previews (like `ghp_****xyz`), and the `AC_SECRET_*` reference name. They never include the raw `value` field.

**The only ways a raw value leaves Agent Core:**
1. An admin uses the Reveal button in the dashboard (requires a valid OTP code).
2. The Credential Broker calls `/internal/vault/resolve` with a valid broker credential and a scope-authorized agent identity.

Full backups include both `agent-core.db` and `vault.key`. Anyone with both files can decrypt your vault. Treat backup ZIPs as sensitive.

---

## How Identities Are Stored

| Identity | How it's stored |
| --- | --- |
| User passwords | bcrypt hash |
| Agent API keys | SHA-256 hash only — plaintext is shown once and never stored |
| Broker credentials | SHA-256 hash in the database; plaintext written to `data/broker.credential` at startup |
| OTP backup codes | SHA-256 hash; returned once after enrollment confirmation or regeneration, then gone |

If an API key, backup code, or broker credential is lost, it must be rotated — it cannot be recovered.

---

## Scope Model

Scopes are the authorization boundary for memory, vault, activity, and briefings. Every read and write operation is checked against the caller's scopes.

| Scope | Who it belongs to |
| --- | --- |
| `user:<id>` | A specific user's personal space; normal agents only use their owner/default user's scope |
| `agent:<id>` | An agent's own private working space |
| `workspace:<id>` | A shared workspace for workspace/team collaboration |
| `shared` | Intentionally shared across all authorized agents |

Agents have separate `read_scopes` and `write_scopes`. An agent belongs to one owner/default user; use workspace/workspace scopes rather than granting one agent multiple users' personal scopes. An agent can read from `shared` without being able to write to it. Writing to `shared` requires explicit scope grant or the `AGENT_CORE_SHARED_SCOPE_AGENTS` environment variable.

**Inactive agents cannot authenticate.** An agent whose API key is rotated or whose record is deactivated immediately loses access.

**Inactive workspaces stop authorizing reads and writes.** If a workspace is deactivated, `workspace:<id>` is no longer a valid scope.

---

## The Credential Broker Flow

Here's how credentials flow from storage to execution without touching the model:

1. You store a secret in the Vault.
2. Agent Core returns an `AC_SECRET_*` reference name — not the secret.
3. The agent (or its tool configuration) passes that reference to the local Credential Broker.
4. The broker calls `/internal/vault/resolve` with the reference and a configured `agent_id`.
5. Agent Core verifies the broker credential and checks that the agent has read access to the vault entry's scope.
6. The broker injects the raw value into the child process environment or an approved HTTP header.

The `agent_id` for resolution must come from your integration configuration — never from model-generated text.

---

## PII Protection in Shared Memory

Writes to the `shared` memory scope are automatically scanned for content that looks like:

- Email addresses
- Phone numbers
- API keys and tokens (including `ac_sk_`, `ghp_`, `sk-`, and similar patterns)
- Social Security Numbers

If a match is found, the write is rejected with `422 PII_DETECTED`. This prevents secrets and personal data from accidentally ending up in shared memory where any agent can read them. Agent-private memory (`agent:<id>`) doesn't have this restriction.

Search queries that resemble credentials or are too noisy (single stop words, punctuation-only) are also rejected before FTS execution.

---

## Rate Limits

Rate limits protect against runaway agents or misconfigured automation. Limits are in-memory token buckets that reset on process restart.

| Operation | Limit |
| --- | --- |
| Memory writes | 60 per minute per agent |
| Memory searches | 60 per minute per agent |
| Concurrent memory searches | 5 per agent |
| Vault creates | 10 per minute per user |
| Failed OTP attempts | 5 per 5 minutes per user |

Rate-limited responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers. `Reset` is a Unix timestamp.

---

## Vault Key Rotation

If you suspect your vault key has been exposed, or as routine security practice, you can rotate the encryption key:

**From the dashboard:** Settings → Vault → Rotate Encryption Key (requires OTP confirmation)

**Via API:**
```bash
curl -X POST http://localhost:3500/api/vault/rotate \
  -H "Authorization: Bearer <admin-session>" \
  -H "Content-Type: application/json" \
  -d '{"otp_code": "123456"}'
```

What happens during rotation:
1. The current primary key is backed up to `data/backups/vault.pre_rotation.{timestamp}.bak`
2. A new primary key is generated
3. The new key is prepended to the keyring (`vault.keyring`), which stores all historical keys
4. All vault entries are re-encrypted with the new primary key
5. A `vault_key_rotated` audit event is written

**Decryption after rotation:** The keyring stores all historical keys. MultiFernet tries keys in keyring order (primary first), so older vault entries encrypted with previous keys still decrypt correctly.

**Restore a previous key** (e.g., from a backup):
```bash
curl -X POST http://localhost:3500/api/vault/restore-key \
  -H "Authorization: Bearer <admin-session>" \
  -H "Content-Type: application/json" \
  -d '{"key_base64": "<base64-encoded-fernet-key>", "otp_code": "123456"}'
```

Restoring a key replaces the current keyring with a single-key keyring containing the restored key. All vault entries must be decryptable by that key. A `vault_key_restored` audit event is written.

**Only rotate or restore when you have a working keyring.** If you have no valid key for your vault entries, restoring a key that cannot decrypt them will leave you unable to access those entries.

---

## Deployment Checklist

Before making Agent Core accessible beyond localhost:

- [ ] Keep `data/`, `.env`, backup ZIPs, and `private/` out of version control.
- [ ] Enroll OTP on your admin account before using backup export or restore.
- [ ] Set `AGENT_CORE_COOKIE_SECURE=true` when serving Agent Core over HTTPS or behind a TLS reverse proxy.
- [ ] Set `AGENT_CORE_CORS_ORIGINS` to specific origins if browser clients need authenticated access.
- [ ] Set `AGENT_CORE_ALLOWED_IPS` if the service is reachable on a LAN or network beyond your machine.
- [ ] Protect `data/vault.key` and `data/broker.credential` with filesystem permissions.
- [ ] Rotate the broker credential after any suspected exposure.
- [ ] Review the audit log after sensitive operations (reveal, backup, restore, broker resolve, key rotation).
