# How Agent Core Works

This document explains the whole system: what each part does, how the parts fit together, and why they are built the way they are. Read it once and you should be able to predict how Agent Core will behave without reading the code.

Everything here is current behaviour. For exact request and response shapes, see the [API Reference](api.md); for settings, see [Configuration](configuration.md).

---

## 1. The shape of the system

Agent Core is a single local HTTP service backed by one SQLite database. It stores five kinds of thing and does nothing else:

| | What it holds | Who writes it |
| --- | --- | --- |
| **Memory** | What stays true — facts, decisions, preferences | Agents |
| **Activity** | What happened — one record per task, with a heartbeat | Agents |
| **Credentials** | Encrypted secrets, exposed only as references | Operator |
| **Connectors** | How to reach external services, and with which credential | Operator |
| **Grants** | Lent authority — narrowed, expiring, revocable | Operator and delegating agents |

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

Nothing leaves the machine unless you configure something that reaches out: a connector action, an embedding backend, a review model, or an outbound webhook notification. Those are the only outbound paths, and all four are off until an operator sets them up.

### Two transports, one implementation

- **MCP** at `POST /mcp` — 35 tools, the path agents normally use. Speaks both JSON-RPC (`tools/list`, `tools/call`) and a plain `{"tool": ..., "params": ...}` shape.
- **REST** under `/api/...` — the same operations for scripts, CI, and non-MCP clients.

Both call the same service layer, so they enforce the same rules and fail the same way. If a request is malformed, both return a `400` with an error code rather than a stack trace.

### Identity and authentication

- **Agents** authenticate with a bearer API key, created in the dashboard and shown once. The key identifies which agent is acting, which determines its scopes. Only a hash is stored.
- **Humans** authenticate with a session and TOTP, and reach the dashboard and the admin-only endpoints.

Every write records who did it. Memory records carry server-generated provenance — the acting agent, the transport, and the activity that was open at the time — which clients cannot forge because they never supply it.

---

## 2. Scopes: who can see what

A scope is a named box. Every memory record lives in exactly one, and every agent has a list of scopes it can read and a list it can write.

| Scope | For | Default access |
| --- | --- | --- |
| `agent:<id>` | One agent's own scratch and self-knowledge | Private to that agent |
| `user:<id>` | The human owner's context and preferences | Agents read; write is an explicit grant |
| `workspace:<id>` | Shared, durable knowledge for a project or domain | Granted per agent |
| `shared` / `global` | Cross-user shared access | Explicit |

**Workspaces are the unit of shared knowledge.** Anything two agents both need, or that belongs to the project rather than to one agent, goes in a workspace. They are cheap — make a new one per domain instead of overloading one.

The common mistake is using `agent:<id>` as a handoff channel. It is private and it disappears with the agent; a shared workspace is both a cleaner grant and a durable one. If an agent has durable knowledge and no workspace to put it in, that is a setup gap to fix, not a reason to use the private scope.

Scope enforcement happens in one place, before any read or write reaches the data. An agent asking for a scope it does not hold gets `SCOPE_DENIED` — it is never silently given a filtered result, because a silently narrowed answer looks identical to a complete one.

Scopes are standing access — what an agent holds from now on. When an agent should have access for one task rather than from now on, that is a delegated grant, covered in [section 7](#7-delegation-authority-for-one-task).

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

"The build server is 192.0.2.10" is a fact — verifiable, and it can go out of date without anyone noticing. "Do not edit vendored dependencies directly" is a decision — nothing can verify it, and it stays true until a person revises it.

That split is why facts are re-checked on a schedule and lose ranking as they age unconfirmed, while decisions are left alone. Ageing decisions would bury exactly the constraints that are most expensive to rediscover.

`preference` records can carry a `slot_key`, which keeps one active value per slot: writing a new preference with the same `scope + slot_key` supersedes the previous one automatically. That makes "the current answer" deterministic instead of something retrieval has to guess at.

### Anchors: what would confirm a fact

A fact can name its `subject_anchor` — the thing a later session would look at to check it:

```
repo:app/services/memory_service.py     a path in the workspace's repository
host:build-01                            a machine
service:<binding_id>                     something reachable through a connector
```

**Repo paths are relative to the workspace root, never absolute.** The same directory has a different absolute path inside every agent's container, so an absolute anchor is resolvable only by whoever wrote it. Agent Core resolves the relative path against this installation's configured root, which is exactly what lets agents with different mounts share one corpus.

The vocabulary is open. `repo:`, `host:` and `service:` are what Agent Core can check by itself; an installation teaches it to check others — `url:`, `doc:`, `ticket:` — by mapping the type to a connector binding it already has (see [Configuration](configuration.md#verification-beyond-code)). Writing an anchor type nothing can check is allowed and warned about, not refused: the anchor still tells a human what would settle the record.

Anchors also work as a filter. `subject_anchor: "repo:app/services"` matches everything anchored under that prefix, which answers "what have we recorded about this part of the code".

### Two clocks

Every record carries two independent timelines, and keeping them apart is what lets the corpus answer *what was true in March* rather than only *what do we believe now*:

| | Columns | Question |
| --- | --- | --- |
| **Transaction time** | `created_at`, `status_changed_at` | When did the system learn this? |
| **Valid time** | `valid_from`, `valid_to` | When was this true in the world? |

Left unset, a record starts when it was written, so nothing silently claims to have always been true. Superseding a record closes the old one's `valid_to` automatically — at the successor's `valid_from` when the writer gives one, otherwise at the moment of supersession — and it never overwrites an end date the writer set deliberately.

Passing `as_of` to a search asks what the corpus held to be true at that instant. Superseded records come back when the instant falls inside their window; that is the whole point. Retracted records never do, because retraction says the record should not have been written, which is a different statement from "it stopped being true".

```
memory_search("where does the user live")                  → New York
memory_search("where does the user live", as_of="2025-06") → Seattle
```

### Lifecycle

A record is `active`, `superseded`, or `retracted`.

- **Superseding** links new to old (`supersedes_id`) and preserves the chain, readable through `GET /api/memory/{id}/chain`. The old record is not deleted; it is the answer to a point-in-time question.
- **Retracting** is a soft delete and is **reversible** (`POST /api/memory/restore`). Reversibility is what makes the clean-up flow safe to use: accepting a proposal can never be the last word.
- **Expiry** (`expires_at`) is for records that stop being true on their own. They drop out of search immediately and are swept on the next maintenance run.
- **Retention** eventually purges retracted and superseded records, and scratchpad notes, on windows set in **Settings → System Behavior**.

### Retrieval

Full-text search over FTS5 is always available. If an embedding backend is configured, search is hybrid: semantic similarity plus keyword, with an exact-match floor so a literal hit cannot be buried by a weak semantic one. Without embeddings, everything still works, but exact keywords matter — `memory_search("authentication")` will not match a record that says "login logic".

Ranking is adjusted by **what the system observed**, never by what the record claimed about itself:

- how often the record has actually been recalled (small, saturating)
- whether callers said it helped, via `memory_feedback` (worth more than a recall — being returned is the retriever's opinion, feedback is the caller's)
- how long since anyone confirmed it, for facts only
- whether it is inside its validity window

`confidence` and `importance` still exist and are caller-assigned, but ranking barely uses them. A record's author is the party least able to judge whether it will be useful later, which is why the signals that matter are collected after the fact.

Search results are a **lean projection** by default — content and the few fields needed to decide relevance, plus a derived `days_since_confirmed`. The lifecycle columns are omitted because on a full page of results they cost more context than they inform. Pass `view: "full"` or fetch one record when you need everything.

### Pinned standing context

A few records per scope can be **pinned**: the rules that apply whatever the task is. Pinned records are returned by `memory_pinned` at the start of a session rather than having to win a search, are included in every handoff briefing, and are skipped by the clean-up rules.

Two constraints make this safe:

- **The list is capped** (10 per scope by default). Standing context that nobody reads in full is not standing context.
- **Agents request pinning; operators grant it.** `memory_pin` queues a request for review rather than pinning. Standing context reaches every session in the scope, including other agents' sessions, which makes it the single most influential thing an agent could write — so it is granted, not taken.

### Keeping the corpus honest

A shared memory corpus decays in two different ways, and they need different treatment.

**Facts go out of date.** The maintenance sweep checks anchored facts against the thing they name and records what it found. The result is one of three states, and the distinction is the whole design:

| Result | Meaning | What happens |
| --- | --- | --- |
| `verified` | The subject is there | Record is stamped as confirmed today |
| `missing` | The subject is definitively gone (404, no such path) | Becomes a review proposal |
| `unverifiable` | Could not check — timeout, error, no configured root | **Nothing** |

Only `missing` is evidence. An outage is not a false memory, and a system that forgets that will delete true records the first time the network hiccups. Records that cannot be checked are left alone rather than condemned, and the sweep orders by least-recently-attempted so never-verifiable records cannot starve the ones that can be checked.

**Records lose their value.** Rules look for records that no longer earn their place and propose them for review:

| Rule | Looks for | Runs when |
| --- | --- | --- |
| `episodic_log` | A task log written as durable memory | You generate proposals |
| `ticket_closeout` | A record about work that has since closed | You generate proposals |
| `duplicate_cluster` | Near-identical records | You generate proposals |
| `stale_volatile` | Facts nobody has confirmed in a long time | You generate proposals |
| `anchor_missing` | The file or service it describes is gone | The verification pass finds it missing |
| `pin_request` | An agent asked for standing context | An agent calls `memory_pin` |
| `low_value` | A model found nothing a future session could act on | You run the usefulness review |

The first four are a pass you trigger — from the review page or `POST /api/memory/proposals/generate`. The last three are queued as things happen. Neither kind runs on the maintenance schedule: proposals are for a human to read, and generating them while nobody is looking only builds a backlog.

Every one of them **proposes; none of them act**. Nothing is applied until an operator answers, and each rule keeps a record of how often its suggestions were accepted, so a rule that keeps being wrong is visible as a number instead of as a vague sense that the queue is noisy. `low_value` can never be automated regardless of its record, because it is a judgement about worth rather than a measurement, and the cost of a wrong call is deleting a constraint someone depended on.

Confirming a record requires **evidence** naming what was checked. Reading a record is not checking it, and a corpus where "confirmed" sometimes means "someone glanced at it" is worse than one with no confirmations at all.

---

## 4. Activity: what happened

Memory holds what stays true. The activity trail holds what happened — one record per task, self-reported by the agent.

```
activity_update   open a task, heartbeat every minute or two, close with a result
activity_search   "what did we do on X last month"
activity_list     what is stale or pending
activity_get      one task, plus the memory records written during it
activity_pickup   claim work a human assigned to this agent
get_briefing      the prior task, its decisions, and relevant workspace memory
```

It is **self-reported on purpose**. There is no detection of agent work: an agent that does not call `activity_update` does not appear, and no briefing can be generated for it. The alternative — inferring activity — would produce a trail that looks authoritative and is quietly incomplete.

Because every memory write cites the activity that was open at the time, the trail links both ways: `activity_get` returns what a task concluded, not only what it was attempting. Picking up someone else's work shows both.

Work assigned from the dashboard is **pulled, not pushed**: `activity_pickup` returns the next assigned task or `null`. Agents check when they start or when idle. Nothing arrives unbidden, which is what keeps the handoff trail auditable.

---

## 5. Credentials

You store a secret once. It is encrypted at rest with Fernet, and a keyring preserves previous keys so rotation does not orphan existing entries.

Agents never receive the secret. They receive a reference:

```
credential_get → AC_SECRET_GITHUB_TOKEN_1A2B3C4D
```

From there, two paths:

- **Broker** — a local tool needs the secret in its own config or environment. The [Credential Broker](credential-broker.md) resolves the reference at runtime, on the machine, so the raw value never passes through a prompt, a log, or a generated config file.
- **Connector** — Agent Core makes the call itself, server-side, using the stored credential. The secret never leaves the service at all.

Prefer the connector path when both are available. The reference is what agents see, and a reference in a transcript is worth nothing to anyone who reads it.

---

## 6. Connectors: the hands

Memory is what agents know. Connectors are what they can do — and the point of putting them here is that every agent does a given thing the same way, with the same credential and the same audit trail, no matter which harness it runs in.

A **connector type** describes a service and its actions. Four ways to add one:

| Source | Use when |
| --- | --- |
| **OpenAPI import** | The service publishes a spec |
| **MCP server** | The service already speaks MCP |
| **Adapter** | You want a shareable, data-only integration — OAuth, session handshakes, CLI wrappers ([docs/adapters.md](adapters.md)) |
| **Generic HTTP** | Everything else; the escape hatch |

A **binding** joins one connector type to one stored credential, inside a scope. That is what agents actually use:

```
connectors_summary        what can I reach from here?
connectors_actions_list   what can this type do?
connectors_bindings_test  is this binding still good?
connectors_run            do it, server-side
```

Adapters declare their requirements, and a binding only exposes actions whose requirements are met — so a half-configured integration presents as unavailable rather than failing at the moment an agent depends on it. Every execution is logged, and the log is pruned on the same retention sweep as everything else.

**Read and write are separate here too.** Reading a binding lets an agent query the service; an action that changes state additionally requires write access to the binding's scope. Read-only is inferred from the action's method — `GET`, `HEAD`, `OPTIONS` — and anything that cannot be identified needs write, because the cost of guessing wrong is running a destructive call on someone's behalf.

---

## 7. Delegation: authority for one task

Scopes fit the agents you work with every day. Some access should not stand, though: a coordinator farming a task out to a worker agent, a scheduled job that needs one connector action a night, a new agent you are not ready to trust with anything permanent. Delegation covers that case — one actor lends an agent a narrowed slice of its authority, and the loan expires.

The pattern is the local equivalent of temporary credentials from a cloud provider (AWS STS role assumption, OAuth token exchange): attenuated authority, never broader than the issuer's, bounded in time, revocable at will.

### Grants

A **grant** names a recipient agent, a purpose, a TTL (capped at one hour), and exactly what it permits: memory and briefing operations in named scopes, specific activity records, and specific connector binding/action pairs. Whoever issues it (a human in the dashboard, or an agent with `can_delegate`) can only give away a subset of what they currently hold.

The recipient claims the grant once, over REST, with its own API key, and receives the grant secret exactly once. Claiming is deliberately not an MCP tool: a secret returned as a tool result would land in a model context, and from there in transcripts and logs. From then on, delegated requests carry both credentials together:

```http
Authorization: Bearer ac_sk_<the recipient's own key>
X-Agent-Core-Grant: ac_dg_<grant-id>.<secret>
```

While the grant header is present, the grant **replaces** the recipient's permanent authority — it is never added to it. A worker holding a grant for one workspace cannot also reach whatever else its key normally could. Issuer, recipient, expiry, revocation, and the issuer's own current authority are revalidated on every request, so revoking a grant (or demoting its issuer) takes effect immediately, not at the next claim.

### Requests: asking for authority you don't have

A coordinator does not need authority to ask for it. `delegation_request` records what is wanted (recipient, purpose, TTL, exact permissions), and the request appears on the dashboard's **Delegation** page. A human approves, narrows, or denies it there. Approval can keep or remove requested permissions; it can never add one. The decision is one-time, audited, and produces an unclaimed grant that goes through the normal claim flow — approval never returns a secret either.

This is the system's human-in-the-loop answer for agent authority: the agent states exactly what it wants, a person decides, and no secret ever passes through a model on the way.

### What this makes possible

Anything that coordinates worker agents (an orchestration framework, a scheduler, a script of your own) faces the same choice: give every worker standing broad credentials, or hand out narrow ones per task. Delegation makes the second choice available to anything that speaks REST, while Agent Core keeps identity, enforcement, and audit in one place. Every delegated action is attributed end to end — principal, issuer, coordinator, executor, grant — in the audit log, and delegated memory writes carry the same provenance.

Backups deliberately revoke grants: restoring a database brings back records, never live authority.

The exact permission shapes, lifecycle rules, and error codes are in the [Delegated Authorization contract](delegated-authorization-integration.md).

---

## 8. Optional capabilities

Three things make Agent Core better and none of them are required. Each has a defined fallback, because a memory layer that stops working when an optional service is down is not a memory layer.

| Capability | Enables | Without it |
| --- | --- | --- |
| **Embedding backend** | Semantic and hybrid search | Full-text search; exact keywords matter more |
| **Review model** | Usefulness review, judgement-based clean-up | Mechanical rules still run; model-backed features report as unconfigured |
| **Verification bindings** | Checking anchor types beyond `repo:`/`host:`/`service:` | Those anchors report `unverifiable`, which changes nothing about the record |

The rule they all follow: **a capability is never a dependency.** Configuring one turns on features — including for records written long before it existed. Removing one turns those features off and leaves everything else untouched. Nothing degrades into guessing.

The review model deserves one explicit note: pointing it at a model on a machine you control means record content never leaves that machine; pointing it at a hosted API means it does. That trade-off is why there is no default and why the features that use it are off until you turn them on.

---

## 9. What runs on a schedule

One in-process sweep, hourly by default, does all of it (`AGENT_CORE_MAINTENANCE_INTERVAL_MINUTES`, `0` to disable — the manual **Run Maintenance** button still works):

- marks activities stale when their heartbeat stops
- sweeps records past `expires_at`
- prunes scratchpad memory past its retention window
- purges retracted and superseded records past theirs
- prunes connector execution and webhook delivery logs
- verifies a batch of anchored facts, least-recently-attempted first

The last run's time, trigger, and results are visible in **Settings → Backup & Restore** and at `GET /api/backup/maintenance/status`.

---

## 10. Your data

```
data/
  agent-core.db       SQLite: memory, activity, agents, credentials, connectors, audit
  credential.key      Fernet encryption key
  credential.keyring  key history, for decrypting after rotation
  broker.credential   local broker credential (auto-generated)
  backups/
```

`data/` is gitignored. A full backup bundles the database **and** the key material — you need both to restore, and a backup of the database alone cannot decrypt a single credential.

---

## 11. The rules the system holds to

Every design decision above comes from one of these. When something in Agent Core surprises you, one of these is usually the reason.

**Propose, never act.** Anything that removes or elevates a memory record goes through an operator. Rules are good at finding candidates and bad at knowing what a constraint cost to learn. Retraction is reversible so the review can be wrong without being expensive.

**Only definite absence is evidence.** "I could not check" and "it is not there" are different findings, and collapsing them deletes true records during outages.

**Evidence over self-assessment.** Ranking uses what was observed — recalls, feedback, confirmations — not what a record's author claimed about its own importance.

**A capability is never a dependency.** Optional services add features. Their absence never breaks the core, and never degrades into guessing.

**Two clocks, not one.** When the system learned something is a different question from when it was true, and a corpus that conflates them cannot explain its own past.

**Explicit beats automatic.** Agents write and read deliberately; work is pulled, not pushed; nothing transfers by magic. It makes the trail auditable and the context intentional.

**Authority is lent, never taken.** A grant is a subset of what its issuer holds, replaces the recipient's own authority rather than adding to it, and expires on its own. Approval can narrow a request, never expand it, and no grant secret is ever visible to a model.

**Local by default.** Memory, credentials, and configuration stay on your disk. The outbound paths are the ones you configured, and there are only four.

---

## Where to go next

| | |
| --- | --- |
| [Quickstart](quickstart.md) | Install, first agent, first memory write |
| [Integrations](integrations.md) | Connecting Claude Code, Cursor, Codex, and others |
| [API Reference](api.md) | Every REST endpoint and MCP tool |
| [Configuration](configuration.md) | Settings, optional capabilities, data layout |
| [Security](security.md) | Scope model, secret handling, deployment checklist |
| [Delegated Authorization](delegated-authorization-integration.md) | The delegation contract for coordinators and agent runtimes |
| [Adapters](adapters.md) | Building and sharing connector integrations |
| [Backup & Restore](backup-restore.md) | Export, restore, maintenance |
| [Troubleshooting](troubleshooting.md) | Common issues and fixes |
