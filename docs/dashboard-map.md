# Dashboard map (1.3.0)

The dashboard is for setting things up and for the few things that need a person. Nothing here is a chore that comes back every week.

Six sidebar entries in one plain list; every other page is a tab inside one of them, and every old address still works.

| Entry | Tabs | What it is for |
| --- | --- | --- |
| **Needs you** (`/`) | Approvals | The one list of what is waiting on a person: approvals (open to review), questions an agent left in a handoff, and credential requests, which are answered right in their row. An empty list says there is nothing to do. Below it: a Health strip (services, webhooks, local credential helper, network limits), search, and recent activity. A **Getting started** card shows until an agent has worked, with optional two-step sign-in as its last item. |
| **Activity** (`/activity`) | Audit log (admin) | What agents are doing and have done. A task row opens to its result, latest note, next steps, open questions and any ask to a person. Agents open and close their own tasks; a task nobody updates for 48 hours closes itself as "ended without report". The audit log is its own tab. |
| **Memory** (`/memory`) | none | Search, browse and save memories; retract (hidden from search, restorable for the retention period) and restore. The Retracted view is the same table as Active, with Restore where Retract was and who or which rule retracted each one. Nothing asks you to review memories: plain noise retracts itself. |
| **Agents** (`/agents`) | Connect, Workspaces | Create an agent and choose what it can read and write; **Connect** generates its key (shown once), tool configuration, instructions and a four-step verification prompt, and its access check has a **Give access** button; **Workspaces** lists the workspaces and their collaborators. |
| **Services** (`/connectors`) | Credentials | The services an agent may use (GitHub, a web service, an MCP server), their connections, and the encrypted credentials behind them. A new connection is tested as soon as it is created. |
| **Settings** (`/settings`) | Users, Webhooks (admin) | Account and two-step sign-in, memory and search settings, data and backup, security and network limits. Run Maintenance, Re-embed and Prune Log sit behind an Advanced fold: maintenance runs by itself every hour. |

## Not in the dashboard

There is no review queue, no stale-task list, no pinned standing context, no "assign a task to an agent" and no confidence column. See the 1.3.0 entries in the changelog.

## For a phone

The navigation is behind a **Menu** button; rows become blocks with the labels beside the values.
