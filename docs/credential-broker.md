# Local credential helper

> **Names.** This page, the dashboard (Settings → Security) and the Health strip all say *local credential helper*. The program is `runner/agent_core_broker.py`, and the environment variables, API paths and audit action names keep the word "broker" (for example `AGENT_CORE_BROKER_TOKEN_FILE` and `/api/dashboard/broker/rotate`), so searching for them still works.

Agent Core stores secrets encrypted on disk. When an agent needs one, it doesn't get the actual value; it gets a placeholder reference like `AC_SECRET_SERVICE_TOKEN_1A2B3C4D`. The **Local credential helper** turns that placeholder into the real thing at runtime, injecting it directly into a tool or script without the model ever seeing it.

```
What the model sees:   AC_SECRET_SERVICE_TOKEN_1A2B3C4D
What the tool gets:    the real secret value  (injected at runtime)
```

Anything you put in a prompt or config file might end up in a log, a response, or a stored conversation. Agent Core sidesteps this entirely. The helper, a small local script, swaps references for real values in the child process environment. Your actual API keys never enter the model's context window.

## How it works

Here's the full flow from storage to execution:

1. You store a credential in Agent Core from the **Credentials** page or API.
2. An agent calls `credential_get` (MCP) or `POST /api/credentials/entries/{id}/reference` (REST).
3. Agent Core returns an `AC_SECRET_*` reference name, not the secret.
4. The agent passes that reference into a tool configuration or environment variable.
5. When you run the tool, you wrap the command with the helper script.
6. The helper scans the environment for `AC_SECRET_*` references.
7. For each one it finds, it calls Agent Core's internal `/internal/credentials/resolve` endpoint.
8. Agent Core verifies the helper key, checks the agent's permissions, and returns the raw value.
9. The helper injects the real value into the child process environment before the tool starts.

> **Important:** The `agent_id` the helper uses for permission checks comes from your integration configuration, not from the model. The model should never be able to specify which agent identity resolves a secret.

This helper path is for local tools and scripts that need a secret in their own environment. If you want Agent Core itself to run the external action, use the **Services** page and a service binding instead of the helper.

You can edit credentials from the Credentials page. Metadata changes do not alter the encrypted secret. The secret is replaced only when you enter a new replacement value.

---

## The helper key

The helper needs its own credential to authenticate with Agent Core. This is separate from agent API keys; it's a special token with a narrow purpose: allowing the helper to resolve credentials on behalf of configured agents.

When Agent Core starts for the first time, it automatically generates this credential and writes it to `data/broker.credential`. The helper reads that file automatically when run from the repo root or inside the container.

**Protect `data/broker.credential` like any other secret.** It's gitignored, and you should keep it out of any shared directories or backups that aren't already secured.

If you move the helper script to a different location (outside the repo), tell it where to find the credential file:

```bash
# Option 1: point to the file explicitly
python agent_core_broker.py --token-file /path/to/broker.credential ...

# Option 2: set an environment variable
export AGENT_CORE_BROKER_TOKEN_FILE=/path/to/broker.credential

# Option 3: pass the credential value directly (less recommended)
python agent_core_broker.py --token ac_broker_xxxxx ...
```

## Keep the link encrypted

The helper sends its credential to Agent Core and receives raw secret values back. Over plain `http://` to another machine, anyone who can watch that network can read both. A private address or a Docker network name does not change that.

- `http://` to this machine (`localhost`, `127.0.0.1`, `::1`) is fine and says nothing.
- `https://` is fine anywhere.
- `http://` to any other host prints a warning to stderr and carries on. Set `AGENT_CORE_BROKER_PLAIN_HTTP=refuse` to make it stop instead.
- If you trust the link (for example a tunnel you set up yourself), pass `--allow-plain-http` or set `AGENT_CORE_BROKER_ALLOW_PLAIN_HTTP=1`; the warning and the refusal both go away. A link you have marked as trusted wins over `refuse`.
- Anything other than `http://` or `https://` (such as `file://`) is always refused.

To rotate the helper key (e.g., after a suspected exposure):

```bash
curl -X POST http://localhost:3500/api/dashboard/broker/rotate \
  -H "Authorization: Bearer <operator-token>"
```

Replace `<operator-token>` with a token from **Settings → Account → Operator tokens** (valid 5 to 60 minutes, shown once). The sign-in session token is refused on these routes.


Agent Core atomically updates `data/broker.credential` and returns the new
credential once. A helper that reads this file needs no manual update. If a
helper uses `--token` or a copied token file, update that value from the
response.

---

## Using the helper script

The helper script is at `runner/agent_core_broker.py`. If you installed via **Docker**, copy it out first:

```bash
docker cp agent-core:/app/runner/agent_core_broker.py ./agent_core_broker.py
```

Then use `python agent_core_broker.py` instead of `python runner/agent_core_broker.py`.

### Environment injection mode

This is the most common mode. Any environment variable in the current shell that contains an `AC_SECRET_*` reference gets that reference swapped for the real value, but only in the child process. Your parent shell is never touched.

```bash
export MY_API_KEY="AC_SECRET_SERVICE_TOKEN_1A2B3C4D"

python runner/agent_core_broker.py \
  --agent-id coding-agent \
  --mode env \
  -- your-tool-or-script
```

When `your-tool-or-script` starts, `MY_API_KEY` will contain the real token.

### Header injection mode

Resolves references and exposes the real values as `AC_HEADER_*` environment variables, which some HTTP tools can pick up directly as request headers.

```bash
python runner/agent_core_broker.py \
  --agent-id coding-agent \
  --mode header \
  -- your-tool-or-script
```

---

## The resolve endpoint

The helper calls this internally. You generally don't need to use it directly, but it's documented here for transparency and for building custom integrations:

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
- Valid helper key in the `Authorization` header
- The specified `agent_id` must have read access to the credential entry's scope

**Error responses:**

| Status | Code | Meaning |
| --- | --- | --- |
| `401` | — | Missing or invalid helper key |
| `403` | `SCOPE_DENIED` | The agent doesn't have read access to this credential's scope |
| `410` | `CREDENTIAL_EXPIRED` | The credential has a TTL and it's past |

---

## Scope requirements

The `agent_id` you configure for the helper determines what credentials it can resolve. The agent needs read access to the scope where the credential entry lives:

| Credential scope | Agent needs this in `read_scopes` |
| --- | --- |
| `user:alex` | `user:alex` |
| `workspace:my-project` | `workspace:my-project` |
| `shared` | `shared` |
| `agent:coding-agent` | `agent:coding-agent` |

You can view and update an agent's scopes from the dashboard under **Agents → [agent] → Edit**, or via `PUT /api/agents/{agent_id}`.
