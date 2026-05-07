# Generic REST Client — Agent Core Integration

> Static example template. The dashboard generator can produce equivalent REST guidance, but this file is a reusable reference.

## Authentication

All requests use the `Authorization` header with your agent API key:

```
Authorization: Bearer YOUR_AGENT_API_KEY
```

Base URL: `http://localhost:3500`

---

## Memory API

### Search Memory

```bash
curl -X POST http://localhost:3500/api/memory/search \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "user preferences",
    "limit": 20,
    "memory_class": "preference"
  }'
```

### Write Memory

```bash
curl -X POST http://localhost:3500/api/memory/write \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "User prefers dark mode",
    "memory_class": "preference",
    "scope": "agent:YOUR_AGENT_ID",
    "domain": "ui",
    "topic": "theme",
    "confidence": 0.9
  }'
```

### Get Memory by Scope

```bash
curl -X POST http://localhost:3500/api/memory/get \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"scope": "agent:YOUR_AGENT_ID", "limit": 50}'
```

### Retract Memory

```bash
curl -X POST "http://localhost:3500/api/memory/retract?record_id=RECORD_ID" \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY"
```

---

## Vault API

### List Vault Entries

```bash
curl "http://localhost:3500/api/vault/entries?scope=agent:YOUR_AGENT_ID" \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY"
```

### Get a Credential Reference

Vault responses never include raw secret values — only masked previews and `AC_SECRET_*` references.

```bash
curl -X POST http://localhost:3500/api/vault/entries/ENTRY_ID/reference \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY"
```

Response:

```json
{"ok": true, "data": {"variable_name": "AC_SECRET_GITHUB_TOKEN_1A2B3C4D"}}
```

Pass this reference to the Credential Broker to get the raw value at execution time.

---

## Activity API

### Create Activity

```bash
curl -X POST http://localhost:3500/api/activity \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_description": "Working on feature X",
    "memory_scope": "agent:YOUR_AGENT_ID"
  }'
```

### Heartbeat

Send every 1–2 minutes while actively working on a task. Tasks that miss heartbeats for more than 5 minutes are marked `stale`.

```bash
curl -X POST http://localhost:3500/api/activity/ACTIVITY_ID/heartbeat \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY"
```

### Update or Complete

```bash
curl -X PUT http://localhost:3500/api/activity/ACTIVITY_ID \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status": "completed", "task_description": "Feature X complete"}'
```

---

## Response Envelope

All responses use the standard envelope:

```json
{"ok": true, "data": { ... }}
```

Errors:

```json
{"ok": false, "error": {"code": "SCOPE_DENIED", "message": "Access denied to this scope"}}
```

---

## Credential Resolution

`AC_SECRET_*` references from vault calls are resolved at execution time by the Credential Broker. The raw value is never returned through the REST API.

**If you installed Agent Core via Docker**, copy the broker script from the container first:

```bash
docker cp agent-core:/app/runner/agent_core_broker.py ./agent_core_broker.py
```

**Run a command with secrets resolved:**

```bash
python agent_core_broker.py \
  --agent-id YOUR_AGENT_ID \
  --mode env \
  -- your-command arg1 arg2
```

See [Credential Broker](../docs/credential-broker.md) for full details.
