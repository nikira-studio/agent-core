# Credential Broker

Agent Core returns stable reference names like `AC_SECRET_GITHUB_TOKEN` instead of raw secret values. The **Credential Broker** is a local helper that resolves those references at execution time, injecting the raw value into a tool's environment without ever exposing it to the model.

---

## Why This Matters

When an agent asks for a credential, it receives something like:

```
AC_SECRET_GITHUB_TOKEN_1A2B3C4D
```

Not the actual token. The agent can pass this reference into its tool configuration, and when the tool runs, the broker resolves it. The model — whether it's Claude, GPT, or a local LLM — never sees the raw secret in its context window.

---

## How Resolution Works

1. A human stores a secret in the Vault.
2. The agent calls `vault_get` or `POST /api/vault/entries/{id}/reference` and receives an `AC_SECRET_*` reference.
3. The agent includes that reference in its tool configuration or passes it to the local broker wrapper.
4. The broker scans for `AC_SECRET_*` patterns in the environment variables or configuration it was given.
5. The broker calls `POST /internal/vault/resolve` with the reference and a configured `agent_id`.
6. Agent Core verifies the broker credential and checks that the agent has read access to the vault entry's scope.
7. The broker injects the raw value into the child process environment or an approved HTTP header.

> **Important:** The `agent_id` used for resolution must come from your integration configuration — not from model-generated text. The model should never be able to choose which agent identity is used to resolve secrets.

---

## The Broker Credential

When Agent Core starts for the first time, it automatically:

- Generates a broker credential with an `ac_broker_` prefix.
- Stores only its hash in the database.
- Writes the plaintext credential to `data/broker.credential` for the bundled local broker to use.

The broker reads this file automatically when it is run from the repo root or inside the container. If you copy the broker elsewhere, pass `--token-file`, set `AGENT_CORE_BROKER_TOKEN_FILE`, or set `AGENT_CORE_BROKER_TOKEN`.

**Protect `data/broker.credential` like any other secret.** It's gitignored through `data/` and should never be committed or shared.

To rotate the broker credential (e.g., after a suspected exposure):

```bash
curl -X POST http://localhost:3500/api/dashboard/broker/rotate \
  -H "Authorization: Bearer <admin-session>"
```

The new credential is returned once in the response. Agent Core does **not** automatically update `data/broker.credential` — you'll need to save it yourself if you're managing the broker credential manually.

---

## Using the Bundled Runner

The broker script is at `runner/agent_core_broker.py` inside the repo. If you installed via **Docker**, the file is inside the container — copy it out first:

```bash
docker cp agent-core:/app/runner/agent_core_broker.py ./agent_core_broker.py
```

Then run it from your local directory using `python agent_core_broker.py` instead of `python runner/agent_core_broker.py`.

The broker script wraps local commands and resolves `AC_SECRET_*` references before passing them to a child process.

**Environment injection mode** — injects resolved values into a child process environment:

```bash
python runner/agent_core_broker.py \
  --agent-id coding-agent \
  --mode env \
  -- your-tool-or-script
```

Any environment variable in the current shell that contains an `AC_SECRET_*` reference will have that reference replaced with the real value in the child process. The parent shell is never modified.

**Header injection mode** — resolves references and exposes them as `AC_HEADER_*` variables:

```bash
python runner/agent_core_broker.py \
  --agent-id coding-agent \
  --mode header \
  -- your-tool-or-script
```

The broker reads the credential from `data/broker.credential` by default. Override with `AGENT_CORE_BROKER_TOKEN` or `--token`.

---

## The Resolve Endpoint

The broker calls this endpoint internally. You generally don't need to call it directly, but here's what it looks like:

```bash
curl -X POST http://localhost:3500/internal/vault/resolve \
  -H "Authorization: Broker <broker-credential>" \
  -H "Content-Type: application/json" \
  -d '{
    "variable_name": "AC_SECRET_GITHUB_TOKEN_1A2B3C4D",
    "agent_id": "coding-agent"
  }'
```

A valid broker credential is required to authenticate the request. The `agent_id` determines which scopes are checked — the agent must have read access to the vault entry's scope. Both conditions must be met.

**Error cases:**
- `403 SCOPE_DENIED` — the agent doesn't have read access to the vault entry's scope.
- `410 CREDENTIAL_EXPIRED` — the vault entry has a TTL and it has passed.
- `401` — invalid or missing broker credential.

---

## Scope Requirements

The configured `agent_id` must have read access to the vault entry's scope:

| Vault entry scope | Agent must have in `read_scopes` |
| --- | --- |
| `user:alex` | `user:alex` |
| `workspace:agent-core` | `workspace:agent-core` |
| `shared` | `shared` |
| `agent:coding-agent` | `agent:coding-agent` |

Grant or modify agent scopes from the dashboard or via `PUT /api/agents/{agent_id}`.
