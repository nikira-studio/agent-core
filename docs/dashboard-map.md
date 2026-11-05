# Dashboard map

A walk-through of the dashboard as a new install's admin meets it, the one question each page answers, and the changes proposed to make it understandable to someone who has never seen it. This is an audit: nothing on the pages changes until each item below is reviewed and built on its own branch. The "Status" column records which have been built.

The rule for anything new: a feature says where it lives in this map, what the user sees first, and gives one line of in-page help for each control it adds. A new setting joins an existing section or justifies a new one. No control stands alone.

## The first five minutes

What an admin does, in order, and where the dashboard sends them today.

| Step | What they need to do | Where it happens today | Problem |
|---|---|---|---|
| 1 | Sign in and see what Agent Core is for | **Overview**: a "Capability Snapshot", a search box, recent activity | Nothing says what to do next. A fresh install shows empty tables. |
| 2 | Add an agent | **Agents** → Create Agent | Done: pick a role (Assistant, Service agent, Proposer, Custom) instead of seven permission boxes. |
| 3 | Connect that agent's tool to Core | **Integrations** (to be renamed Connect a tool): choose a user, workspace and agent, then copy the generated key and config | Separate page from Agents. The agent form ends with a link to it. Easy to miss. |
| 4 | Give it a credential or an outside service | **Credentials** (store a secret), then **Connectors** (to be renamed Services: add a service, then add a binding to it) | Three places for one idea: "let this agent use that service". |
| 5 | See what it did | **Activity** | Fine. |
| 6 | Approve something it proposed | **Approvals**, or a phone link for a trusted device | Only appears once something is pending. Nothing on Overview points to it. |

Proposed: Overview shows a short **Getting started** list on a fresh install (add an agent, connect it, give it a service, watch Activity, approve) that disappears once each step is done, and a single **Waiting on you** list afterwards (see Overview below).

## The one question each page answers

A page that answers two or more questions is flagged. Those are the merge or split candidates.

| Page | Its question | Answers more than one? |
|---|---|---|
| Overview | What needs me, and is everything working? | Yes: capability snapshot, a search box and recent activity are three different things. |
| Users | Who can sign in? | No. |
| Agents | Which agents exist and what may each do? | No. |
| Workspaces | Which shared spaces exist and who reaches them? | No. |
| Memory | What does the system remember? | Yes: the Memory Clean-up review queue ("what needs me") sits on the same page. |
| Connectors | Which outside services can agents use? | Yes: the catalog, the bindings (who may use it, with which credential), recent executions, and three kinds of import. |
| Credentials | Which secrets are stored? | No. |
| Integrations | How do I connect an AI tool to Core? | No, but it is one step of "add an agent" kept on its own page. |
| Activity | What are agents doing and what did they do? | Yes today: workspace sync status, assigning work, pruning history. Fixed below. |
| Approvals | What is waiting for my decision? | Yes today: trusted-device management shares the page. Fixed by moving it to Settings → Account. |
| Audit | What happened, and who did it? | No (export and prune are tools for the same question). |
| Webhooks | How does Core talk to the outside, and the outside to Core? | Yes: an inbound receiver and outbound notifications. |
| Settings | (see below) | Yes: about eight. |

## Proposed sections

The sidebar has thirteen flat entries. Group them under four headings; every page stays where it is, so no link or bookmark breaks.

- **Home**: Overview.
- **Work** (things to do): Activity, Approvals.
- **People and access**: Users, Agents, Workspaces.
- **Connect**: Connect a tool, Services, Credentials, Webhooks.
- **Records and system**: Memory, Audit, Settings.

Two pages are renamed in the sidebar and headings, because a stranger cannot tell them apart: **Integrations → Connect a tool** (how an AI tool connects to Core) and **Connectors → Services** (the outside services agents can use). The routes `/integrations` and `/connectors` stay. Credentials keeps its name.

Regular users already see a subset (no Users, Audit, Webhooks); the grouping applies to what they see.

## Overview

| Today | Proposal |
|---|---|
| "Capability Snapshot" (what it counts is not obvious to a newcomer) | Replace with one **Waiting on you** list: pending approvals, handoffs that need a person, credential requests, Memory Clean-up items, each linking to where it is decided. This is the data `attention_list` already returns. |
| "Operational Search" | Becomes one search box in the header, on every page, that searches Memory, Audit and Activity. The page searches stay. |
| "Is everything working?" (lost with the Capability Snapshot) | A small **Health strip** under Waiting on you: Core version and health, failing bindings, failed webhooks in the last 24 hours, broker status. Each item links to its page. |
| Recent Activity | Keep. |

## Settings

Settings is the page with the least structure: account, security, preferences, operator tokens, a secrets snapshot, a long "System Behavior" form, vector search, the encryption key, the broker, backup and restore, maintenance and startup checks, all in one scroll. Proposed split into labelled sections (tabs on the same page, so `/settings` still works):

| Section | Holds | Its question |
|---|---|---|
| **Account** | Name and email, timezone, operator tokens, trusted devices (moved from Approvals) | Who am I, and how do my scripts and devices sign in? |
| **Memory** | Vector search and re-embed, duplicate warning, the Memory Clean-up settings (unconfirmed-inference days and importance, consolidation scan) | How does search and review behave? |
| **Data and backup** | Retention (scratchpad, retracted records, episodic expiry, connector log, webhook log) in one group, pruning activity history (moved from Activity), Run Maintenance, backup and restore, startup checks | What is kept, for how long, and how do I back it up? |
| **Security** | Encryption key (status, rotate, restore), broker credential, secrets snapshot | How are stored secrets protected? |

Individual changes:

| Control | Change | Why |
|---|---|---|
| Webhook retry policy (attempts, initial delay, maximum delay, jitter) | **Move** to the Webhooks page, under outbound notifications | It only affects webhooks. |
| The five retention fields | **Merge** into one "Data retention" group beside Run Maintenance | All five are "how long until Maintenance deletes it". |
| Proposal pending cap per rule, total cap, generation budget per run | **Remove from the UI** (keep as configuration with the current defaults 20 / 50 / 20) | Three limits no admin can reason about; the defaults are the answer. Needs the owner's decision before it is removed. |
| Unconfirmed-inference days and minimum importance | **Merge** under "Memory review" with a sentence on what the queue is | Two numbers that mean nothing apart from the queue. |
| Duplicate warning threshold | **Keep**, labelled "Warn about near-duplicate memories above" with the 0 to 1 scale explained | Understandable once phrased as an effect. |
| Secrets snapshot | **Keep** under Security, add one line on what the statuses mean | Unexplained table today. |
| Encryption key: Check status, Rotate, Restore | **Keep**, each with a line on when to use it | Rotate re-encrypts everything; Restore is for a lost key. Different situations, one block of three buttons. |
| Broker: Rotate broker credential | **Keep**, one line: "Use this if a local tool that resolves references should lose access." | |

## Approvals and Activity

**Approvals** keeps one question, "what is waiting for my decision". Its trusted-devices card moves to Settings → Account; the Approvals page links there for a person who wants to trust a phone.

**Activity** keeps **Assign work**, the one human action on the page. Pruning history moves to Settings → Data and backup beside the retention controls. Workspace sync status becomes one line in the page header (for example "3 workspaces in sync, 1 behind") that expands, not a section of its own.

## Agents

Built: role choice on the create and edit forms (Assistant, Service agent, Proposer, Custom with every permission described), a Memory access section that collapses the three scope lists and says how many extras it holds, and a role badge on the list.

Still to do: the "Agent Access" card above the list repeats help that now lives in the form; trim it to one line. The Integrations link in each row becomes **Connect** so its purpose is clear.

## Connectors

One page holds four jobs. Proposed: keep one page, make the top of it one **Add a service** button that offers the three existing ways (from an API spec, from an MCP server, a plain HTTP service) instead of three buttons plus Quick Start plus the catalog's own Add Binding. Tuck "Native MCP endpoint override" and the JSON config field in bindings under **Advanced** with one line each. Execution history stays, collapsed under the binding it belongs to.

## Webhooks

Two things, kept on one page but separated into **Receiving** (the inbound URL and key, how to send a command) and **Sending** (outbound notifications, plus the retry policy moved here from Settings). Each gets a one-line introduction.

## Memory

Memory Clean-up stays on the page but is introduced as "Suggestions waiting for you", and its count also appears on Overview's Waiting on you. In the memory detail view, group the fourteen fields into **What it says** (content, class, scope, topic), **How sure** (confidence, importance, last confirmed, status) and **Where it came from** (provenance, anchor, version history); slot key, valid from and valid to go under **Advanced** with a line each.

## Integrations

Keep as the page that generates a connection, but reach it from Agents through the **Connect** button (it opens with that agent chosen) and from the end of "Create Agent". The three selectors (user, workspace, agent) are explained: whose memory the tool reads, which shared space, and which agent identity it signs in as.

## Pages with no structural change

Users, Workspaces, Credentials, Audit. Each still needs its controls explained in place; that happens when each is touched (below).

## Empty states

A fresh public install is mostly empty tables. Every page gets one sentence when it has nothing to show: what the page will show, and the one action to get started (for example, on Services: "No services yet. Add one so agents can act in an outside system.").

## Next item, not this pass: one flow for step 4

The first-five-minutes walk-through still sends an admin through Credentials, Services and a binding to let one agent use one service. The follow-up is a single **Give access to a service** flow started from an agent's row, which creates the binding and, if needed, the credential inline. It is recorded here so it is built after this pass, not forgotten.

## How the work is done

1. This audit, reviewed first.
2. One branch per item, each reviewed before it is merged, in this order: Agents leftovers with the sidebar grouping and the two renames, Overview (including the header search), Settings split and moves (taking in trusted devices and activity pruning), Services, Webhooks, Memory, Connect a tool. Empty-state sentences are added on each page as it is touched.
3. As each page is touched, every control that remains gets one line of in-page help, and this document is updated with that page's final controls and what was removed, merged or moved. The final version is the walk-through a reviewer reads to ask "would a stranger's install understand this?".

## Status

| Item | Status |
|---|---|
| Agents: role choice, shorter forms, role badge | Built and deployed |
| Sidebar grouping, Connect a tool and Services names | Built and deployed |
| Trusted devices off by default | Built and deployed |
| Overview: Waiting on you, Health, Getting started, Search | Built and deployed. The header search on every page was not built: the existing Overview search stays, since a second place to search is a new mechanism. |
| Settings split into four tabs; proposal caps out of the UI; activity pruning and trusted devices moved in | Built, in review. The webhook retry policy stays in Data and backup until the Webhooks page is reworked. |
| Everything else above | Approved as the plan; branches not yet started |
