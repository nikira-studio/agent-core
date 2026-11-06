# Asking a person first

A person can set any action of a service to **ask a person first**. When an agent, an operator token or a script then runs that action, Agent Core does not run it. It stores the exact request and waits for a signed-in person to approve or reject it. This page is the design, for anyone adding to it.

## What a person sees

- **Setting it up (Services).** View Actions shows **Ask a person first** on each action, with a choice of holding every caller or only named agents. The gate belongs to the service and the action, never to one binding, because an agent can create or edit bindings. Nothing is held until a person turns it on, and the author's own `approval_required` hint in an action's policy stays advisory: it is displayed and never turns the gate on.
- **Noticing it (Overview).** A held request is in **Waiting on you** and in the agent's own session briefing and `attention_list`.
- **Deciding it (Approvals).** The page leads with one plain sentence ("Build Agent wants to run create_issue on the GitHub service (binding X, workspace Y)"), says when the request expires, shows exactly what would be sent (anything key- or password-shaped is hidden), then **Approve and run** and **Reject**, with an optional note the agent can read. Each request has a stable link; opening it only shows the request, and a GET never decides.

## What is enforced

- Every programmatic caller is held: agent keys, operator tokens and any other bearer credential, over MCP and over REST. The one exemption is a person pressing Run in their own browser with the dashboard cookie. Core's internal authority is held too, so configured background checks cannot skip a gate.
- The held request is stored encrypted with a fingerprint of the binding (endpoint, config, credential id). Approving re-checks the fingerprint and that the caller still has access, then runs the request once as the caller who asked. If the binding or its service changed in between, the request fails and says so.
- States: `pending`, then `executing`, then `done` or `failed`, or `rejected` or `expired`. Each move is a conditional update, so a decision happens once and a replay finds it already decided. A request left `executing` after a crash is marked failed, never re-run. An identical pending request is not duplicated, and one caller and the install as a whole have a cap on waiting requests. It expires after `AGENT_CORE_CONNECTOR_APPROVAL_EXPIRY_HOURS` (24).
- **Who may decide:** an admin, or someone with write access to the binding's workspace. The decide endpoint takes a browser cookie only (any `Authorization` header is refused), checks the CSRF token and the origin, and is rate-limited. No agent tool decides a request.
- **Who may change a gate:** only an admin in the dashboard. The endpoint refuses any `Authorization` header, so no key, token or script can switch a gate off, and every change is audited with who, which action, and before and after. An agent can only propose the change (`connector_gate.set`) through the approvals path, where a person approves it. Turning a gate off never releases a request already waiting.
- **What the agent is told:** `pending_approval` is a normal answer, not an error, with an approval id and an expiry, so it waits instead of retrying. `approval_get` shows the status, the person's note, an error or the result; a rejected, expired or failed request carries a message to stop and report.
- **Told elsewhere:** a held run is audited (`connector_action_held`), and a decision and its outcome are audited as `approval_approved`, `approval_rejected` and `approval_run_finished`. The `approval_requested` webhook event carries ids and the page path only.

## Not in this version

Batches of runs, one-tap approval links, and notification routing. A front end that carries the link to a person (a chat bot, a briefing) reads the `approval_requested` event or `attention_list`.

## Where the code is

`app/services/action_gate_service.py` (which actions are gated), `app/services/connector_approval_service.py` (holding, deciding, running once), the hook in `connector_service.execute_authorized_binding_action_with_logging`, the decide path in `approval_service.decide`, and `app/routes/approvals_page.py` for the page.
