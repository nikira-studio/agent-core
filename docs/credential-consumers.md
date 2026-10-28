# Resolve-only credential consumers

Use this when a service or container, rather than an AI agent, needs its own secrets from Agent Core at startup (database passwords, API tokens, OAuth refresh state). Each service gets a machine identity that can fetch only the entries granted to it, so one compromised service cannot read the rest of the vault. For agents, use `AC_SECRET_*` references through the [Credential Broker](credential-broker.md) instead.

Machine consumers can retrieve individual encrypted credentials without vault browsing, credential administration, memory or connector access. Create a dedicated agent with only `credentials_resolve`, then create exact entry grants through an administrator's human session. Inherited user and workspace scopes do not authorize resolution. Legacy agent defaults do not include this capability.

The owner API is `/api/credential-consumers`:

| Method and path | Authority | Result |
| --- | --- | --- |
| `POST /grants` | Administrator human session | Create a grant with `consumer_agent_id`, `entry_id`, optional future `expires_at` |
| `GET /grants?consumer_agent_id=…` | Administrator human session | Grant metadata, including expiry and revocation; maximum 1,000 with explicit truncation |
| `DELETE /grants/{grant_id}` | Administrator human session | Idempotently revoke an exact grant |
| `POST /entries/{entry_id}/resolve` | Dedicated agent API key | Secret plus entry/grant version metadata |
| `POST /entries/{entry_id}/status` | Dedicated agent API key | Same readiness/version checks, without returning the secret |
| `POST /leases/status` | Dedicated agent API key | Atomic metadata-only validation for 1–64 exact entry grants |

Resolve, status and lease-status require HTTPS and an `Authorization: Bearer ac_sk_…` header. They do not accept human sessions. No request-selected agent identity is accepted. Responses have `Cache-Control: no-store` and `Pragma: no-cache`; consumers must validate TLS and refuse redirects. Configure a trusted reverse proxy correctly so the application receives the actual HTTPS scheme; never trust arbitrary client forwarding headers. A plain-HTTP deployment such as the default `http://localhost:3500` cannot serve these endpoints: put Agent Core behind a TLS-terminating proxy first.

The standard single-entry response envelope contains `data.entry_id`, `grant_id`, `version`, `expires_at`, `grant_expires_at` and `revalidate_after_seconds` (30). Resolve additionally contains `value`. `version` hashes stored ciphertext, not plaintext. Vault key re-encryption therefore changes the version too. Expired, malformed-expiry, empty, whitespace-only or undecryptable entries fail closed. The batch lease response contains `data.entries`, with those metadata fields per unique entry ID and no values. The batch is all-or-nothing: a missing/expired grant or unavailable entry rejects the complete set. If several environment fields use the same credential, send its entry ID once and reuse that returned lease for each field. Metadata-only lease checks do not decrypt credentials or create a success audit row per heartbeat; denials remain audited. Successful secret resolutions and mutations remain auditable. Audit rows contain identities, resource IDs and outcomes only.

Each consumer requires its own bootstrap API key. Keep bootstrap files outside source and synchronized backup trees, accessible only to that process. Fetch values into memory before launching the application; do not write fetched secrets into environment files, container configuration or application settings. Remove old configuration precedence before cutover. Rotate the individual agent key to invalidate its former bootstrap immediately.

A running application can retain an already delivered value. The server cannot recall plaintext. A supervisor may revalidate all of its exact grants together through `/leases/status` within the minimum of 30 seconds, credential expiry and grant expiry, stop credential-dependent work on failure, and restart/reload on version change. Startup-only consumers guarantee startup refusal, not running-process lease enforcement. Keep these two guarantees separate in outage tests.

Grant/key mutations and resolution are serialized at the database boundary. A response authorized before a concurrent revocation may finish; subsequent resolutions fail. Database restore must invalidate imported consumer grants so an older backup cannot resurrect revoked authority. Reauthorize grants explicitly after recovery. Existing human reveal and shared Broker interfaces retain their contracts; do not distribute a Broker token to consumers.

`/api/credentials/rotate` rotates the vault encryption key. It does not rotate provider credentials, revoke them at their issuer, or restart consumers.

## Optional exact-entry writes

Read grants stay read-only after upgrades. An administrator may create a grant with the additional explicit boolean `allow_write: true`. Revoke an existing grant before replacing it with different authority. The consumer still needs only `credentials_resolve`; it gains no vault listing, reveal, general credential CRUD or authority over another entry.

A directly authenticated HTTPS consumer with that exact write grant may call `POST /entries/{entry_id}/write` with:

```json
{
  "request_id": "unique-operation-identity",
  "expected_version": "the-64-character-ciphertext-version-returned-by-resolve",
  "value": "the-complete-new-value"
}
```

The server authenticates the current key, owner, capability, grant and expiry before checking replay. Value, result and metadata audit commit in one transaction under the key-operation lock. A committed response contains only entry ID, request ID, state and new version. Values are limited to 65,536 UTF-8 bytes. Error responses do not echo submitted content.

Persist the request identity in the consumer's operation context before sending. If a transport response is lost, retry exactly the same identity, expected version and value. A committed replay returns its original result without re-encryption. Reusing the identity with another payload fails. Never resolve a fresh version and retry a rejected refresh value against it: that would silently overwrite concurrent work.

On a version conflict, Core retains the candidate encrypted as another ordinary vault credential, without a consumer grant, and returns HTTP 409 with `state: conflict_retained` and the candidate entry ID. Both resolve and status for the original entry then fail for every consumer until an administrator reconciles the conflict. The candidate participates in ordinary vault re-encryption and encrypted backups. It is never returned in an audit or error body. At most three retained conflict candidates per original entry are accepted; a blocked original rejects further new writes. If the storage bound is reached, the request fails explicitly and no candidate-retention claim is made.

An administrator must establish which provider response is current, update the authoritative entry using the verified value, remove its `consumer_write_conflict` metadata marker, and handle the retained candidates under the installation's retention policy. Clearing the marker alone does not establish provider validity. Do not grant consumers access to their quarantined candidates or let them clear the block.

For mutable OAuth, read Core at each operation and write refreshed state back through this versioned API. A startup environment snapshot cannot remain authoritative after refresh. Preserve application access controls before resolution and before mutation; remove the application's stale-value fallback only after an isolated compatibility test and live cutover proof. A conflict is a reconciliation event, not an instruction to repeat the provider refresh.

There is no atomic transaction spanning an external provider and Core. A process can fail after the provider rotates a token and before Core receives it. Memory-only consumers cannot promise recovery of that unseen value. Report that uncertainty and require provider/account recovery when needed; do not claim that CAS or transport replay removes this window.

## Audit retention

Every successful resolution is audited, which adds up quickly for services that fetch on each start. Nightly maintenance keeps individual `credential_consumer_resolved` rows for 30 days, then rolls them up into one row per consumer, entry and day (`credential_resolution_daily`). Denials, grant changes and credential changes are kept individually for 13 months. Both periods are fixed in `app/services/credential_audit_retention_service.py`.
