# Dashboard map

A walk-through of the dashboard as a new install's admin meets it, the one question each page answers, and the changes proposed to make it understandable to someone who has never seen it. This is an audit: nothing on the pages changes until each item below is reviewed and built on its own branch. The "Status" column records which have been built.

The rule for anything new: a feature says where it lives in this map, what the user sees first, and gives one line of in-page help for each control it adds. A new setting joins an existing section or justifies a new one. No control stands alone.

## The first five minutes

What an admin does, in order, and where the dashboard sends them today.

| Step | What they need to do | Where it happens today | Problem |
|---|---|---|---|
| 1 | Sign in and see what Agent Core is for | **Overview**: a "Capability Snapshot", a search box, recent activity | Nothing says what to do next. A fresh install shows empty tables. |
| 2 | Add an agent | **Agents** → Create Agent | Done: pick a role (Assistant, Service agent, Proposer, Custom) instead of seven permission boxes. |
| 3 | Connect that agent's tool to Core | **Integrations** (renamed Connect): choose a user, workspace and agent, then copy the generated key and config | Separate page from Agents. The agent form ends with a link to it. Easy to miss. |
| 4 | Give it a credential or an outside service | **Credentials** (store a secret), then **Connectors** (to be renamed Services: add a service, then add a binding to it) | Three places for one idea: "let this agent use that service". |
| 5 | See what it did | **Activity** | Fine. |
| 6 | Approve something it proposed | **Approvals** | Only appears once something is pending. Nothing on Overview points to it. |

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
| Approvals | What is waiting for my decision? | Yes. |
| Audit | What happened, and who did it? | No (export and prune are tools for the same question). |
| Webhooks | How does Core talk to the outside, and the outside to Core? | Yes: an inbound receiver and outbound notifications. |
| Settings | (see below) | Yes: about eight. |

## Proposed sections

The sidebar has thirteen flat entries. Group them under headings; every page stays where it is, so no link or bookmark breaks.

- **Home**: Overview.
- **Work** (things to do): Activity, Approvals, Memory (its Clean-up suggestions are the part a person acts on).
- **Access** (who and what can get in): Users, Agents, Workspaces, Connect.
- **Capabilities** (what agents can do beyond remembering): Services, Credentials.
- **System**: Audit, Webhooks, Settings. Webhooks carry events between Core and outside systems and are admin-only, like Audit.

Two pages are renamed in the sidebar and headings, because a stranger cannot tell them apart: **Integrations → Connect** (generates the key and config an agent's tool uses to sign in to Core) and **Connectors → Services** (the outside services agents can use). The routes `/integrations` and `/connectors` stay. Credentials keeps its name.

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
| **Account** | Name and email, timezone, operator tokens | Who am I, and how do my scripts and devices sign in? |
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

**Approvals** keeps one question, "what is waiting for my decision".

**Activity** keeps **Assign work**, the one human action on the page. Pruning history moves to Settings → Data and backup beside the retention controls. Workspace sync status becomes one line in the page header (for example "3 workspaces in sync, 1 behind") that expands, not a section of its own.

## Agents

Built: role choice on the create and edit forms (Assistant, Service agent, Proposer, Custom with every permission described), a Memory access section that collapses the three scope lists and says how many extras it holds, and a role badge on the list.

Still to do: the "Agent Access" card above the list repeats help that now lives in the form; trim it to one line. The Integrations link in each row becomes **Connect** so its purpose is clear.

## Connectors

Built (the page is called Services): one **Add a service** button offers the three ways (from an API spec, from an MCP server, a plain web service), each with a line saying when to use it. The credentials button and the two browse buttons left the header; the API directory and adapters are linked from the Service Catalog note, and Credentials from the quick-start steps. The extra-settings JSON and the MCP-server override moved under **Advanced** in the binding forms, each with one line of help. Recent runs are a collapsed section; each binding keeps its own History button. The quick start shows only until the first binding exists. Counts are labelled in plain words (Services, Bindings, Enabled bindings, Bindings with errors, Enabled actions).

## Webhooks

Built: **Receiving** (the inbound address, its key, and how to send a command) and **Sending** (the list of webhooks) are two headed sections, each with a one-line introduction. The retry policy moved here from Settings → Data and backup and sits under **Retry policy** beneath the list, with a line on each number. The webhook delivery log retention stays with the other retention fields in Settings. The empty list says what a webhook is for.

## Memory

Built: the review queue is titled **Suggestions waiting for you** and sits above the records whenever something is waiting (below them otherwise), with the Overview "Waiting on you" list pointing at it. In the memory detail view the fields are grouped under **What it says** (content, class, scope, topic, standing context), **How sure** (confidence, importance, last confirmed, status) and **Where it came from** (created, anchor, provenance, version history), each with one line of help; slot key, valid from and valid to sit under **Advanced**. The Save Memory form has the same Advanced group, and an empty page says how memories get there.

## Integrations

Built (the page is called Connect): reached from the **Connect** button on each agent's row and from the end of Create Agent. The selectors sit under **Who is connecting?**, each with a line: the user is whose personal memory the tool can read, the workspace is the shared space it saves team memory in, and the agent is the identity it signs in as. The note about what Generate connection does (it issues a fresh key and invalidates the old one) is folded under the agent selector.

## Pages with no structural change

Users, Workspaces, Credentials, Audit. Each still needs its controls explained in place; that happens when each is touched (below).

## In-page help

Each control that remains on a reworked page carries one line saying what it is for, right under it. The test `test_every_field_on_the_reworked_forms_has_a_line_of_help` lists the Services, Agents, Settings, Workspaces and Users fields that must keep theirs, so a new field cannot be added without it. The Webhooks name and address fields carry theirs in the label, and the Memory, Connect, Overview and Settings forms were written with theirs. Pages that already had help on every control (Activity's Assign work, Audit's prune date, the Credentials forms) were left alone. The Credentials page stays as it is until the owner has used it from a phone.

## Credentials: who can use it

Built: each credential shows who uses it (agent names and a count of services) to an admin, with one **Access** button. Its dialog has three things in the first view: the agents with access (each removable), a picker to give it to another agent, and the services that use it. A greyed-out agent is listed under "Can't receive it yet" with a link to change its role. Expiry and the write flag sit under **Advanced**. The rule is stated at the top: one credential per service, shared by grant, a separate credential only for a different account or an outside service. From the Agents page a service agent's row links to the same list for that agent.

## Extend before creating

Adding capability to what already exists is the normal path, so the Agents and Workspaces pages say so where a person decides: the intro card, the Create dialogs ("Does an existing agent already do this job?", "Does an existing workspace already fit?") and the empty lists. A new agent is for a separate tool or identity with its own key and history; a new workspace is for a separate body of work.

## Empty states

A fresh public install is mostly empty tables. Every page gets one sentence when it has nothing to show: what the page will show, and the one action to get started (for example, on Services: "No services yet. Add one so agents can act in an outside system.").

## Next item, not this pass: one flow for step 4

The first-five-minutes walk-through still sends an admin through Credentials, Services and a binding to let one agent use one service. The follow-up is a single **Give access to a service** flow started from an agent's row, which creates the binding and, if needed, the credential inline. It is recorded here so it is built after this pass, not forgotten.

## How the work is done

1. This audit, reviewed first.
2. One branch per item, each reviewed before it is merged, in this order: Agents leftovers with the sidebar grouping and the two renames, Overview (including the header search), Settings split and moves (taking in activity pruning), Services, Webhooks, Memory, Connect. Empty-state sentences are added on each page as it is touched.
3. As each page is touched, every control that remains gets one line of in-page help, and this document is updated with that page's final controls and what was removed, merged or moved. The final version is the walk-through a reviewer reads to ask "would a stranger's install understand this?".
4. **A page change is not done until its screenshots have been looked at, at desktop and phone width.** Passing tests is not the bar; the rendered page is. `scripts/ui_sweep.py` signs in as an admin on a copy or throwaway install, opens every page (and each Settings tab) at both widths, saves the screenshots, and flags anything wider than its card or any page that scrolls sideways. Open the images, check long names and empty states, and send before and after shots with the change. Stylesheet and script links carry their file's own content hash, so a changed file reaches browsers that cached the old one without anyone bumping a version by hand.

## Status

| Item | Status |
|---|---|
| Agents: role choice, shorter forms, role badge | Built and deployed |
| Sidebar grouping (Work, Access, Capabilities, System), Connect and Services names | Built and deployed; regrouped and Connect renamed to Connect on branch feat/sidebar-wording |
| Trusted devices | Removed before release: no sign-in path other than the dashboard session (and operator tokens for scripts) |
| Overview: Waiting on you, Health, Getting started, Search | Built and deployed. The header search on every page was not built: the existing Overview search stays, since a second place to search is a new mechanism. |
| Settings split into four tabs; proposal caps out of the UI; activity pruning moved in | Built, in review. The webhook retry policy has moved to the Webhooks page. |
| Services: Add a service, Advanced fields, collapsed runs, empty states | Built and deployed |
| Webhooks: Receiving and Sending, retry policy moved from Settings | Built and deployed |
| Memory: Suggestions waiting for you, grouped detail, Advanced | Built and deployed |
| Connect: Who is connecting?, a line for each selector | Built, in review |
| Help under every remaining control | Built and deployed |
| Extend before creating (Agents and Workspaces wording) | Built and deployed |
| Credentials: Used by column and Access dialog | Built, in review |
| Still to do | The summary for the owner: section map, what was removed, merged or moved (this document), and a clean-up of anything the owner finds unclear in use. The Credentials page stays as it is until the owner has used it from a phone. |

## Wording rules (batch 1, 2026-11-06)

One vocabulary, used in titles, tooltips and help. A page that needs a different word is a bug.

- **Service** and **binding** only. No "connector", "connector type" or "adapter" in visible text. Pages: Services, Service directory, Ready-made services, Services help.
- **Task** for a unit of work on Activity. **Memory / memories**, not "records". **Workspace** wherever a scope means a workspace; "scope" stays only in advanced areas and generated agent files. Never "principal" (say "owner").
- **Key**, not "bearer key" or "API key" in the same sentence. **Two-step sign-in**, not "OTP". **Encryption key**, not "Fernet key". **Local credential helper** for the broker.
- Buttons that act on one row (Deactivate, Retract) are plain; the filled blue button is the one main action on a page. The Connect output button is blue and reads "Generate One-Time Key + <output>".
- Anything that deletes for good names what goes with it (memories and tasks for a workspace, the key and history for an agent) and asks for the ID to be typed.
- Where Connect's checks all pass, one line says so and the list sits behind "Show the checks"; it opens by itself when something fails.

## Network limits (2026-11-06)

**Settings → Security → Network limits** (admin). *Private addresses*: Allow, Report only or Block, with an *Allowed private addresses* list (one host, address or range per line) and, in Report only, the hosts that would have been blocked this week with an **Allow** button each. *Credential broker callers*: an allowed-callers list and Report only or Refuse. Each control has one line of help. **Overview → Health** shows both as quiet lines (the broker line says when any caller is accepted). A refusal reads "Blocked: 192.168.1.50 is a private address. Allow it under Settings → Security → Allowed private addresses." and is shown when a service or binding is added, not first at run time.

## Running a person's own services (2026-11-06)

**Agents** (Create and Edit): the checkbox **Run its person's own services** (off by default; help: it lets the agent also run the services bound in its person's personal scope, not only those in workspaces it is given). Agents that have it on show a **runs personal services** tag in the list. The agent's person's memory scope is still readable either way.

## Access that has no effect yet (2026-11-06)

An agent acts for its default user and can use only the workspaces that person can reach. A workspace added to the agent but not reachable by that person has no effect, so **Agents** (a line under the agent's access), **Edit Agent** (a banner) and **Connect** (the access check) say so in plain words and say what to do: "No effect yet: Pat is not a member of Beta. Add them on Workspaces."

## Asking a person first (2026-11-06)

Where it lives: **Services** to set it up (View Actions → **Ask a person first** per action, for every caller or named agents), **Approvals** to decide, **Overview** (Waiting on you) to notice. What a person sees first on the approval page is one plain sentence, then what will be sent, then the two buttons. One line of help per control: the checkbox says "A run waits on the Approvals page until you approve it. The agent is told to wait."; the decision card says "Approving runs this one request now, exactly as shown. Rejecting runs nothing, and the agent is told so."; the note field is "Note for the agent (optional)". Vocabulary: service, binding, workspace. Design: [design/approval-gate.md](design/approval-gate.md).

## Connect in three steps (2026-11-06)

1. **Who is connecting?** User, workspace, agent, with the Access Check under it (one line when everything is in place).
2. **Which tool?** Claude Code, Codex, Cursor, Windsurf, Antigravity or Other MCP tool. Choosing one shows only what that tool needs, in order, each with Copy and Download: (a) its MCP config, with the **Generate One-Time Key + … MCP Config** button; (b) the file it reads (CLAUDE.md for Claude Code, AGENTS.md for Codex, the setup guide for the rest); for Claude Code, a pointer to the optional session hook. Other MCP tool shows the whole MCP config.
3. **Check it works.** The connection doctor and the Verification Prompt.

Everything else (environment variables, session prompt, hook, assistants prompt, the full MCP config, and the old tab links such as `?output_type=env`) is under **Other outputs**, which opens by itself when a link asks for one. The generated text is unchanged; the per-tool config is the same MCP config cut into its blocks. `?target=` picks the tool and is kept when the form is resubmitted.

## Phones and rows (1.2.1)

Where it lives: the sidebar on a phone, and the row lists on Agents, Services (bindings and recent runs) and Overview (Recent Activity). What a person sees first: the group headings with their pages; then one block per row with the name and its few labelled values. One line of help per control is unchanged; the new **More** button on an Agents or bindings row says what is under it ("Credentials, deactivate, delete" / "History, delete"). Vocabulary: row buttons are Edit, Connect, Test and More; Delete always sits under More. A decided approval says "Approved:" or "Rejected:" and links back to **Waiting on you**. The two key rotations say what keeps working and what stops before they run.

## Headings and the Overview (1.2.1)

The page title is the one h1; each card's title is an h2; a section inside a card is an h3 (class `sub-heading`). Pages that group cards under their own h2 (Webhooks, Connect) keep their cards at h3. Overview answers "what needs me, and is it healthy": **Waiting on you**, **Health**, **Search**, **Recent Activity**. The count tiles were removed because each count lives on its own page and none answered something Health does not.

## Parked candidates (not built)

- **An approvable operation for adding an action to a stored service.** `connector_type.actions` only turns existing actions off or on; it cannot add one, so an agent cannot propose "add this action to that service" for a person to approve on the Approvals page. Adding an action to an installed adapter today means editing its manifest on the host and calling the adapter-update route as the person (one short operator token). If this comes up again, a general `connector_type.import`-style operation would let agents propose a spec for a person to approve. Not built: one occurrence does not justify a new mechanism.

## Trims after the pre-release review (1.2.1)

- **Activity** lost its Coordination Snapshot (four counters, an ownership line and two execution-link percentages). The page is **Waiting on you**, the task list with its status tabs, and **Workspace synchronization** (closed when empty); Stale and Blocked tasks are one tab each and also appear on Overview's Waiting on you.
- **One name for the credential broker.** The dashboard, the Health strip, error messages and the docs say **local credential helper**; the Settings card is *Local credential helper*, its allowed callers are *Local credential helper callers* (Settings → Security → Network limits), and the Health line is *Helper callers*. The program, environment variables, API paths and audit action names keep "broker".
- **Search by meaning** (Settings → Memory): *Embedding dimension* and *Query prefix* sit under **Advanced** (open when a prefix is set); provider, model, URL, auth type and key stay in the first view.

- **One source for "waiting on a person".** Overview's Waiting on you, the Activity page's handoff table and `attention_list` (MCP and `GET /api/activity/attention`) read the same service (`app/services/waiting_service.py`): approvals, handoffs, credential requests and Memory Clean-up suggestions, each limited to what the viewer may see. `attention_list` keeps `items`, `count` and `approvals` as they were and gains `credential_requests` and `memory_suggestions`, so a coordinator can tell a person about every kind of request the dashboard shows.

- **Suggestions answered by a check.** When an agent (or a person) confirms a memory with evidence, or retracts it with a reason, the pending Memory Clean-up suggestions about that one memory close themselves and leave **Suggestions waiting for you**: accepted by whoever checked, kept in the decided history, and counted for the rule (a suggestion to retract counts as a miss when the memory is checked and kept). A confirm without evidence closes nothing; duplicate clusters and pin requests stay for a person. The audit log records each as `memory_proposal_answered` with the evidence.
- **Done on a blocked task.** A task that ended "blocked" can be closed as completed or cancelled by an admin or by the agent it belongs to, with a reason; the Done button on a blocked row asks for it. Nothing else changes a task once it has ended.

- **Memory suggestions are not "waiting on you".** They are optional housekeeping, so Overview's Waiting on you has no row or count for them; an admin sees one quiet line under the list ("Memory clean-up: N suggestions", linking to the Memory page). The Memory Clean-up card says none of it is urgent, that an agent can answer a suggestion by checking the memory, and that the scheduled scan refills the list up to its cap. `attention_list` keeps `memory_suggestions` so agents can find the work. A task that ended blocked can also be closed by its own agent through the MCP `activity_update` tool's optional `activity_id` (completed or cancelled, with a reason), for an agent that has no REST access.

