# Credential Broker

Agent Core stores secrets encrypted on disk. When an agent needs one, it doesn't get the actual value — it gets a placeholder reference like `AC_SECRET_SERVICE_TOKEN_1A2B3C4D`. The **Credential Broker** turns that placeholder into the real thing at runtime, injecting it directly into a tool or script without the model ever seeing it.

```
What the model sees:   AC_SECRET_SERVICE_TOKEN_1A2B3C4D
What the tool gets:    the real secret value  (injected at runtime)
```

Anything you put in a prompt or config file might end up in a log, a response, or a stored conversation. Agent Core sidesteps this entirely. The broker — a small local script — swaps references for real values in the child process environment. Your actual API keys never enter the model's context window.

## How It Works

Here's the full flow from storage to execution:

1. You store a credential in Agent Core from the **Connectors** page or API.
2. An agent calls `credential_get` (MCP) or `POST /api/credentials/entries/{id}/reference` (REST).
3. Agent Core returns an `AC_SECRET_*` reference name — not the secret.
4. The agent passes that reference into a tool configuration or environment variable.
5. When you run the tool, you wrap the command with the broker script.
6. The broker scans the environment for `AC_SECRET_*` references.
7. For each one it finds, it calls Agent Core's internal `/internal/credentials/resolve` endpoint.
8. Agent Core verifies the broker credential, checks the agent's permissions, and returns the raw value.
9. The broker injects the real value into the child process environment before the tool starts.

> **Important:** The `agent_id` the broker uses for permission checks comes from your integration configuration — not from the model. The model should never be able to specify which agent identity resolves a secret.

This broker path is for local tools and scripts that need a secret in their own environment. If you want Agent Core itself to run the external action, use the **Connectors** page and a connector binding instead of the broker.

You can edit credentials from the Connectors page. Metadata changes do not alter the encrypted secret. The secret is replaced only when you enter a new replacement value.

---

## The Broker Credential

The broker needs its own credential to authenticate with Agent Core. This is separate from agent API keys — it's a special token with a narrow purpose: allowing the broker to resolve credentials on behalf of configured agents.

When Agent Core starts for the first time, it automatically generates this credential and writes it to `data/broker.credential`. The broker reads that file automatically when run from the repo root or inside the container.

**Protect `data/broker.credential` like any other secret.** It's gitignored, and you should keep it out of any shared directories or backups that aren't already secured.

If you move the broker script to a different location (outside the repo), tell it where to find the credential file:

```bash
# Option 1: point to the file explicitly
python agent_core_broker.py --token-file /path/to/broker.credential ...

# Option 2: set an environment variable
export AGENT_CORE_BROKER_TOKEN_FILE=/path/to/broker.credential

# Option 3: pass the credential value directly (less recommended)
python agent_core_broker.py --token ac_broker_xxxxx ...
```

To rotate the broker credential (e.g., after a suspected exposure):

```bash
curl -X POST http://localhost:3500/api/dashboard/broker/rotate \
  -H "Authorization: Bearer <admin-session>"
```

The new credential comes back in the response **once**. Agent Core doesn't automatically update `data/broker.credential` — you need to write it there yourself:

```bash
echo "ac_broker_new_value_here" > data/broker.credential
```

---

## Using the Broker Script

The broker script is at `runner/agent_core_broker.py`. If you installed via **Docker**, copy it out first:

```bash
docker cp agent-core:/app/runner/agent_core_broker.py ./agent_core_broker.py
```

Then use `python agent_core_broker.py` instead of `python runner/agent_core_broker.py`.

### Environment Injection Mode

This is the most common mode. Any environment variable in the current shell that contains an `AC_SECRET_*` reference gets that reference swapped for the real value — but only in the child process. Your parent shell is never touched.

```bash
export MY_API_KEY="AC_SECRET_SERVICE_TOKEN_1A2B3C4D"

python runner/agent_core_broker.py \
  --agent-id coding-agent \
  --mode env \
  -- your-tool-or-script
```

When `your-tool-or-script` starts, `MY_API_KEY` will contain the real token.

### Header Injection Mode

Resolves references and exposes the real values as `AC_HEADER_*` environment variables, which some HTTP tools can pick up directly as request headers.

```bash
python runner/agent_core_broker.py \
  --agent-id coding-agent \
  --mode header \
  -- your-tool-or-script
```

---

## The Resolve Endpoint

The broker calls this internally. You generally don't need to use it directly, but it's documented here for transparency and for building custom integrations:

```bash
curl -X POST http://localhost:3500/internal/credentials/resolve \
  -H "Authorization: Broker <broker-credential>" \
  -H "Content-Type: application/json" \
  -d '{
    "variable_name": "AC_SECRET_SERVICE_TOKEN_1A2B3C4D",
    "agent_id": "coding-agent"
  }'
```

Both conditions must be met for this to succeed:
- Valid broker credential in the `Authorization` header
- The specified `agent_id` must have read access to the credential entry's scope

**Error responses:**

| Status | Code | Meaning |
| --- | --- | --- |
| `401` | — | Missing or invalid broker credential |
| `403` | `SCOPE_DENIED` | The agent doesn't have read access to this credential's scope |
| `410` | `CREDENTIAL_EXPIRED` | The credential has a TTL and it's past |

---

## Scope Requirements

The `agent_id` you configure for the broker determines what credentials it can resolve. The agent needs read access to the scope where the credential entry lives:

| Credential scope | Agent needs this in `read_scopes` |
| --- | --- |
| `user:alex` | `user:alex` |
| `workspace:my-project` | `workspace:my-project` |
| `shared` | `shared` |
| `agent:coding-agent` | `agent:coding-agent` |

You can view and update an agent's scopes from the dashboard under **Agents → [agent] → Edit**, or via `PUT /api/agents/{agent_id}`.
