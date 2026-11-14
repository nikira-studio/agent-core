# How Agent Core works

This document explains the whole system: what each part does, how the parts fit together, and why they are built the way they are. Read it once and you should be able to predict how Agent Core will behave without reading the code.

Everything here is current behaviour. For exact request and response shapes, see the [API Reference](api.md); for settings, see [Configuration](configuration.md).

---

## 1. The shape of the system

Agent Core is a single local HTTP service backed by one SQLite database. It stores four kinds of thing and does nothing else:

| | What it holds | Who writes it |
| --- | --- | --- |
| **Memory** | What stays true: facts, decisions, preferences | Agents |
| **Activity** | What happened: one record per task, with a heartbeat | Agents |
| **Credentials** | Encrypted secrets, exposed only as references | Operator |
| **Services** | How to reach external services, and with which credential | Operator |

Agents connect to it. It does not connect to agents, schedule them, or run their work. That boundary is deliberate: an agent's harness already decides what to do next, and a memory layer that also tried to would have to be replaced every time you changed agents. Agent Core is what stays constant while the agents around it change.

```
┌──────────────┐                      ┌────────────────────────────┐
│ Claude Code  │                      │        Agent Core          │
│ Cursor       │  MCP or REST         │  ┌──────────────────────┐  │
│ Codex        │ ───────────────────► │  │ scope enforcement    │  │
│ your script  │   Bearer API key     │  ├──────────────────────┤  │
└──────────────┘                      │  │ memory │ activity    │  │
                                      │  │ creds  │ connectors  │  │
                                      │  └──────────────────────┘  │
                                      └─────────────┬──────────────┘
                                                    │
                              ┌─────────────────────┴──────────────────┐
                              │ SQLite (FTS5) + Fernet-encrypted        │
                              │ credentials, on your disk               │
                              └─────────────────────┬──────────────────┘
                                                    │ only when a connector runs
                                                    ▼
                                            external services
```

Nothing leaves the machine unless you configure something that reaches out: a service action, an embedding backend, or an outbound webhook notification. Those are the only outbound paths, and all three are off until an operator sets them up.

### Two transports, one implementation

- **MCP** at `POST /mcp`: 35 tools, the path agents normally use. Speaks both JSON-RPC (`tools/list`, `tools/call`) and a plain `{"tool": ..., "params": ...}` shape.
- **REST** under `/api/...`: the same operations for scripts, CI, and non-MCP clients.

Both call the same service layer, so they enforce the same rules and fail the same way. If a request is malformed, both return a `400` with an error code rather than a stack trace.

### Identity and authentication

- **Agents** authenticate with a bearer API key, created in the dashboard and shown once. The key identifies which agent is acting, which determines its scopes. Only a hash is stored.
- **Humans** authenticate with a session and TOTP, and reach the dashboard and the admin-only endpoints.

Every write records who did it. Memory records carry server-generated provenance (the acting agent, the transport, and the activity that was open at the time) which clients cannot forge because they never supply it.

---

## 2. Scopes: who can see what

A scope is a named box. Every memory record lives in exactly one, and every agent has a list of scopes it can read and a list it can write.

| Scope | For | Default access |
| --- | --- | --- |
| `agent:<id>` | One agent's own scratch and self-knowledge | Private to that agent |
| `user:<id>` | The active principal's context and preferences | Every active agent for that principal inherits read access; write is an explicit grant |
| `workspace:<id>` | Shared, durable knowledge for a project or domain | Granted per agent |
| `shared` / `global` | Cross-user shared access | Explicit |

**Workspaces are the unit of shared knowledge.** Anything two agents both need, or that belongs to the project rather than to one agent, goes in a workspace. They are cheap; make a new one per domain instead of overloading one.

The common mistake is using `agent:<id>` as a handoff channel. It is private and it disappears with the agent; a shared workspace is both a cleaner grant and a durable one. If an agent has durable knowledge and no workspace to put it in, that is a setup gap to fix, not a reason to use the private scope.

Scope enforcement happens in one place, before any read or write reaches the data. An agent asking for a scope it does not hold gets `SCOPE_DENIED`; it is never silently given a filtered result, because a silently narrowed answer looks identical to a complete one.

---

## 3. Memory

### What belongs in memory

Memory is for what will still be worth knowing in a future session. It is not a log. "What we did today" belongs in the activity trail, which is searchable and is the right home for "what happened on X last month". Writing task progress into memory fills the corpus with records that were true for an afternoon, and the write path says so when it sees one.

### The four classes

| Class | Meaning | Settled by |
| --- | --- | --- |
| `fact` | Something about the world | **Checking it** |
| `decision` | Something someone chose | **Someone deciding** |
| `preference` | How the owner wants things done | The owner |
| `scratchpad` | Short-lived working notes | Nothing; swept on retention |

The line between `fact` and `decision` is the most important one in the system, because it determines what Agent Core is allowed to do on its own:

> If the record would be settled by **checking something**, it is a fact. If it would be settled by **someone deciding**, it is a decision.

"The build server is 192.0.2.10" is a fact: verifiable, and it can go out of date without anyone noticing. "Do not edit vendored dependencies directly" is a decision: nothing can verify it, and it stays true until a person revises it.

That split is why facts are re-checked on a schedule and lose ranking as they age unconfirmed, while decisions are left alone. Ageing decisions would bury exactly the constraints that are most expensive to rediscover.

`preference` records can carry a `slot_key`, which keeps one active value per slot: writing a new preference with the same `scope + slot_key` supersedes the previous one automatically. That makes "the current answer" deterministic instead of something retrieval has to guess at.

### Anchors: what would confirm a fact

A fact can name its `subject_anchor`, the thing a later session would look at to check it:

```
repo:app/services/memory_service.py     a path in the workspace's repository
host:build-01                            a machine
service:<binding_id>                     something reachable through a connector
```

**Repo paths are relative to the workspace root, never absolute.** The same directory has a different absolute path inside every agent's container, so an absolute anchor is resolvable only by whoever wrote it. Agent Core resolves the relative path against this installation's configured root, which is exactly what lets agents with different mounts share one corpus.

The vocabulary is open. `repo:`, `host:` and `service:` are what Agent Core can check by itself; an installation teaches it to check others (`url:`, `doc:`, `ticket:`) by mapping the type to a service connection it already has (see [Configuration](configuration.md#verification-beyond-code)). Writing an anchor type nothing can check is allowed and warned about, not refused: the anchor still tells a human what would settle the record.

Anchors also work as a filter. `subject_anchor: "repo:app/services"` matches everything anchored under that prefix, which answers "what have we recorded about this part of the code".

### Two clocks

Every record carries two independent timelines, and keeping them apart is what lets the corpus answer *what was true in March* rather than only *what do we believe now*:

| | Columns | Question |
| --- | --- | --- |
| **Transaction time** | `created_at`, `status_changed_at` | When did the system learn this? |
| **Valid time** | `valid_from`, `valid_to` | When was this true in the world? |

Left unset, a record starts when it was written, so nothing silently claims to have always been true. Superseding a record closes the old one's `valid_to` automatically, at the successor's `valid_from` when the writer gives one, otherwise at the moment of supersession, and it never overwrites an end date the writer set deliberately.

Passing `as_of` to a search asks what the corpus held to be true at that instant. Superseded records come back when the instant falls inside their window; that is the whole point. Retracted records never do, because retraction says the record should not have been written, which is a different statement from "it stopped being true".

```
memory_search("where does the user live")                  → New York
memory_search("where does the user live", as_of="2025-06") → Seattle
```

### Lifecycle

A record is `active`, `superseded`, or `retracted`.

- **Superseding** links new to old (`supersedes_id`) and preserves the chain, readable through `GET /api/memory/{id}/chain`. The old record is not deleted; it is the answer to a point-in-time question.
- **Retracting** is a soft delete and is **reversible** (`POST /api/memory/restore`). Reversibility is what makes automatic clean-up safe: a retraction by a rule or by a person is never the last word.
- **Expiry** (`expires_at`) is for records that stop being true on their own. They drop out of search immediately and the next maintenance run retracts them (every class, including decisions): restorable for the retention period like any retraction, and restoring one clears the passed expiry.
- **Retention** eventually purges retracted and superseded records, and scratchpad notes, on windows set in **Settings → Data and backup → How long things are kept**.

### Retrieval

Full-text search over FTS5 is always available. If an embedding backend is configured, search is hybrid: semantic similarity plus keyword, with an exact-match floor so a literal hit cannot be buried by a weak semantic one. Without embeddings, everything still works, but exact keywords matter: `memory_search("authentication")` will not match a record that says "login logic".

Ranking is adjusted by **what the system observed**, never by what the record claimed about itself:

- whether a never-confirmed fact has human-authenticated provenance; other source kinds receive a small penalty until evidence-based confirmation
- how often the record has actually been recalled (small, saturating)
- how long since anyone confirmed it, for facts only
- whether it is inside its validity window

`confidence` and `importance` still exist and are caller-assigned, but ranking barely uses them. A record's author is the party least able to judge whether it will be useful later, which is why the signals that matter are collected after the fact.

Search results are a **lean projection** by default: content and the few fields needed to decide relevance, plus a derived `days_since_confirmed`. The lifecycle columns are omitted because on a full page of results they cost more context than they inform. Pass `view: "full"` or fetch one record when you need everything.

### Keeping the corpus honest

A shared memory corpus decays in two different ways, and they need different treatment.

**Facts go out of date.** The maintenance sweep checks anchored facts against the thing they name and records what it found. The result is one of three states, and the distinction is the whole design:

| Result | Meaning | What happens |
| --- | --- | --- |
| `verified` | The subject is there | `anchor_checked_at` is set (returned with the record; nothing ranks on it or shows it on a page). The record is **not** confirmed: a file that still exists does not make the sentence about it true, so staleness still counts from the last evidence-based confirmation |
| `missing` | The subject is definitively gone (404, no such path) | The time is noted; a second `missing` at least an hour later retracts the record (soft, restorable, audited as the `anchor_missing` rule) |
| `unverifiable` | Could not check: timeout, error, no configured root, or no service connection for the host | **Nothing** |

What can be checked depends on the setup: a `repo:` anchor needs the workspace mapped to a directory on the Core host (`workspace_repo_roots`, see the configuration guide; empty on a new install, so repo anchors are unverifiable until it is set), a `host:` anchor is checked only through a service connection that targets that host, and a `service:` anchor through its connection.

Only `missing` is evidence. An outage is not a false memory, and a system that forgets that will delete true records the first time the network hiccups. Records that cannot be checked are left alone rather than condemned, and the sweep orders by least-recently-attempted so never-verifiable records cannot starve the ones that can be checked.

**Records lose their value, and plain noise retracts itself.** Four kinds of record are retracted without asking anyone, because a person answering for them was right every time it was measured:

| Rule | Retracts | Runs when |
| --- | --- | --- |
| `episodic_log` | A per-occurrence log written as durable memory (a heartbeat, a tick, an idle run) | Maintenance, hourly |
| `ticket_closeout` | Ticket-closeout narration ("PROJ-594 closed done ...") | Maintenance, hourly |
| `duplicate_cluster` | The same text written twice in one workspace (records that merely open alike are not duplicates); the newest is kept. Never a decision, and never a record written in the last 24 hours | Maintenance, hourly |
| `anchor_missing` | A fact whose anchor was definitively gone in two checks an hour apart | The verification pass |

A retraction is **soft**: the record is restorable for the retention period (`POST /api/memory/restore`, or Restore in the Retracted view of the Memory page, which is the same table as the active memories and says when it was retracted and by whom or which rule), it is audited as `memory_auto_retracted` with the rule as the actor, and a run retracts at most 20 records. There is no review queue and nothing for a person to answer. Whether a memory is *useful* is not something a rule decides: Core does not retract on age, on lack of confirmation, or on judgement. The other automatic removals leave a trace too: a memory removed because its `expires_at` passed, and a retracted memory deleted after the retention period, are named (id, topic, workspace, class) in the audit entry of that maintenance run.

Confirming a record requires **evidence** naming what was checked. Reading a record is not checking it. Use the separate **Confirm with evidence** action when the record was actually verified.

---

## 4. Activity: what happened

Memory holds what stays true. The activity trail holds what happened: one record per task, self-reported by the agent.

```
session_context   one session-start block: rules, waiting and unfinished work, recent results, changes
activity_update   open a task, report progress every few minutes, close with a result
workspace_sync    receive assignments and changes since this execution's cursor
workspace_sync_ack acknowledge a delivered page after processing it
activity_search   "what did we do on X last month"
activity_list     what is stale or pending
activity_get      one task, plus the memory records written during it
activity_handoff_resolve  settle a request to a person that the agent itself made
attention_list    everything waiting on a person (handoffs, approvals, credential requests) in the scopes the caller reads (coordinators)
```

It is **self-reported on purpose**. There is no detection of agent work: an agent that does not call `activity_update` does not appear, and no handoff is recorded for it. The alternative, inferring activity, would produce a trail that looks authoritative and is quietly incomplete.

Because every memory write cites the activity that was open at the time, the trail links both ways: `activity_get` returns what a task concluded, not only what it was attempting. Picking up someone else's work shows both.

Agents open and close their own tasks, and learn of each other's work at session start (the session context) or when they ask (`activity_list`, `workspace_sync`). Nothing is pushed to a running agent, and nothing is assigned to one.

Workspace changes use the same pull model. `workspace_sync` creates an execution ID for one host session and returns changes after that execution's acknowledged cursor. The response separates memory and activity changes. `other_session_changes` lists the IDs of changes written by another execution; the changes themselves are in their resource groups. After processing a page, the client advances its cursor with `workspace_sync_ack`.

Starting a new workspace activity through the MCP `activity_update` tool also has a bounded safety net. If the supplied execution is not already caught up, Agent Core can return `since_last_active`: a change-sequence-based digest since that agent's previous activity. It includes baseline and truncation metadata because the change feed has finite retention. The digest is best-effort and additive; activity creation still succeeds when it cannot be computed. It is omitted for write-only callers and for executions that already received the current change feed.

The Activity dashboard reports execution linkage for workspace activities over the installation lifetime and the last 30 days. Linkage means the activity carried an execution ID; it does not prove that the agent read or acted on every delivered change.

The durable change log does not replace memory or activities. It tells a session which source records changed. Agent Core retains change rows for a bounded period, so an expired cursor returns `cursor_reset: true` and a recent bootstrap instead of pretending that no changes occurred.

---

## 5. Credentials

You store a secret once. It is encrypted at rest with Fernet, and a keyring preserves previous keys so rotation does not orphan existing entries.

Agents never receive the secret. They receive a reference:

```
credential_get → AC_SECRET_GITHUB_TOKEN_1A2B3C4D
```

From there, three paths:

- **Service**: Agent Core makes the call itself, server-side, using the stored credential. The secret never leaves the service at all.
- **Credential consumer**: a container or service fetches its own granted secrets at startup ([Credential consumers](credential-consumers.md)). Use it for the software you run yourself.
- **Local credential helper**: a tool you run yourself needs the secret in its own config or environment. The [Local credential helper](credential-broker.md) resolves the reference at runtime, on the machine, so the raw value never passes through a prompt, a log, or a generated config file.

Prefer the service path when both are available. The reference is what agents see, and a reference in a transcript is worth nothing to anyone who reads it.

---

## 6. Services: the hands

Memory is what agents know. Services are what they can do, and the point of putting them here is that every agent does a given thing the same way, with the same credential and the same audit trail, no matter which harness it runs in.

A **service** describes a service and its actions. Four ways to add one:

| Source | Use when |
| --- | --- |
| **OpenAPI import** | The service publishes a spec |
| **MCP server** | The service already speaks MCP |
| **Adapter** | You want a shareable, data-only integration: OAuth, session handshakes, CLI wrappers ([docs/adapters.md](adapters.md)) |
| **Generic HTTP** | Everything else; the escape hatch |

A **connection** joins one service to one stored credential, inside a scope. That is what agents actually use:

```
connectors_summary        what can I reach from here?
connectors_actions_list   what can this type do?
connectors_bindings_test  is this binding still good?
connectors_run            do it, server-side
```

Adapters declare their requirements, and a connection only exposes actions whose requirements are met, so a half-configured integration presents as unavailable rather than failing at the moment an agent depends on it. Every execution is logged, and the log is pruned on the same retention sweep as everything else.

Connection visibility grants access to every action enabled on the service. Services define which actions exist and which are disabled; connections decide which users, agents, or workspaces can use the connection.

---

## 7. Optional capabilities

Two things make Agent Core better and neither of them are required. Each has a defined fallback, because a memory layer that stops working when an optional service is down is not a memory layer.

| Capability | Enables | Without it |
| --- | --- | --- |
| **Embedding backend** | Semantic and hybrid search | Full-text search; exact keywords matter more |
| **Verification connections** | Checking anchor types beyond `repo:`/`host:`/`service:` | Those anchors report `unverifiable`, which changes nothing about the record |

The rule they both follow: **a capability is never a dependency.** Configuring one turns on features, including for records written long before it existed. Removing one turns those features off and leaves everything else untouched. Nothing degrades into guessing.

---

## 8. What runs on a schedule

One in-process sweep, hourly by default, does all of it (`AGENT_CORE_MAINTENANCE_INTERVAL_MINUTES`, `0` to disable; the manual **Run Maintenance** button still works):

- marks activities stale once they have had no update for 5 minutes, at the next hourly sweep or when a dashboard page or API call that lists tasks loads (any update from the agent brings one back)
- sweeps records past `expires_at`
- prunes scratchpad memory past its retention window
- purges retracted and superseded records past theirs
- prunes service execution and webhook delivery logs
- verifies a batch of anchored facts, least-recently-attempted first

The last run's time, trigger, and results are visible in **Settings → Data and backup** and at `GET /api/backup/maintenance/status`.

---

## 9. Your data

```
data/
  agent-core.db       SQLite: memory, activity, agents, credentials, connectors, audit
  credential.key      Fernet encryption key
  credential.keyring  key history, for decrypting after rotation
  broker.credential   helper key for the local credential helper (auto-generated)
  backups/
```

`data/` is gitignored. A full backup bundles the database **and** the key material; you need both to restore, and a backup of the database alone cannot decrypt a single credential.

---

## 10. The rules the system holds to

Every design decision above comes from one of these. When something in Agent Core surprises you, one of these is usually the reason.

**Propose, never act.** Anything that removes or elevates a memory record goes through an operator. Rules are good at finding candidates and bad at knowing what a constraint cost to learn. Retraction is reversible so the review can be wrong without being expensive.

**Only definite absence is evidence.** "I could not check" and "it is not there" are different findings, and collapsing them deletes true records during outages.

**Evidence over self-assessment.** Ranking uses what was observed (recalls, confirmations), not what a record's author claimed about its own importance.

**A capability is never a dependency.** Optional services add features. Their absence never breaks the core, and never degrades into guessing.

**Two clocks, not one.** When the system learned something is a different question from when it was true, and a corpus that conflates them cannot explain its own past.

**Explicit beats automatic.** Agents write and read deliberately; work is pulled, not pushed; nothing transfers by magic. It makes the trail auditable and the context intentional.

**Local by default.** Memory, credentials, and configuration stay on your disk. The outbound paths are the ones you configured, and there are only three.

---

## Where to go next

| | |
| --- | --- |
| [Quickstart](quickstart.md) | Install, first agent, first memory write |
| [Integrations](integrations.md) | Connecting Claude Code, Cursor, Codex, and others |
| [API Reference](api.md) | Every REST endpoint and MCP tool |
| [Configuration](configuration.md) | Settings, optional capabilities, data layout |
| [Security](security.md) | Scope model, secret handling, deployment checklist |
| [Adapters](adapters.md) | Building and sharing service integrations |
| [Backup & Restore](backup-restore.md) | Export, restore, maintenance |
| [Troubleshooting](troubleshooting.md) | Common issues and fixes |

---

## What to expect (and what not to)

- **Agents learn of each other's work at session start, or when they ask.** The session context (injected by the Claude Code hook, fetched with `session_context`, or attached by Core to an agent's first call of any tool after a quiet hour) lists open work, recent results and key records of the workspace; `activity_list` and `workspace_sync` answer later questions. Nothing is pushed to an agent that is already running.
- **Two agents in one workspace see each other's finished work and shared memories** at their next session start. A workspace does not see another workspace unless an agent is told to look there and is allowed to.
- **Memory holds decisions and checkable facts, not general knowledge, and it is only as good as what was written.** Core refuses plain logs of one occurrence and identical repeats and retracts leftovers, but it cannot make a vague memory useful: that depends on what agents write, which the generated instructions try to steer.
- **History lives in two places.** Core keeps who is doing what now, lasting decisions and facts, and one outcome sentence per finished task; the repositories keep the detail (their changelogs and status files), and a task's result points at them.
- **What is automatic and what is not.** Done by Core: closing abandoned tasks (48 hours), ending an ask when its task closes, letting an unanswered ask lapse and a blocked task with nothing waiting end after 7 days, retracting plain noise, answering a blocked task when its credential arrives. Done by the host hook: Claude Code injects the session context at the start and, if a session ends with a task still open, asks for a one-or-two-sentence result. Enforced at the tool call: completing a task needs a result; plain logs and identical repeats are refused. Left to the agent's habit, and therefore only a request: keeping a task's note fresh, searching before deciding, writing a memory at all. Claude Code records the outcome automatically; other hosts rely on their instructions.

## Names

The dashboard and the API use different words for some things. Tool and field names never change; this is how they line up. A "scope" is the word the API and the tool documentation use for the workspace, personal area or agent-private area a memory, task or credential lives in; the dashboard says Workspace, Personal and Private to an agent.

| In the dashboard | In the API and tools |
| --- | --- |
| Workspace, Personal, Private to an agent | scope (`workspace:<id>`, `user:<id>`, `agent:<id>`) |
| Owner | `owner_user_id` (the agent's default user is the same person) |
| Task | activity (`activity_update`, `/api/activity`) |
| Service | connector type |
| Connection (a service set up for a workspace or an agent) | binding (`connector_bindings`) |
| Retract (hidden from search, restorable for the retention period) | `memory_retract`, `POST /api/memory/retract`; `POST /api/memory/restore` undoes it |
| Delete (gone for good) | agents, credentials, connections, and a retracted memory you delete |
| Two-step sign-in | TOTP, `otp_*` routes and audit events |
| Agent API key | the `ac_sk_…` key one agent presents (shown once; generated on Connect) |
| Operator token | `ac_op_…`, short-lived admin access, never called a key |
| Helper key | the key of the local credential helper (the "broker": program, environment variables and API paths keep that name) |
| Encryption key | the key that encrypts stored credentials at rest |
