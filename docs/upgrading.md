# Upgrading

Agent Core upgrades its own database when it starts. You do not run anything by hand. Most steps only add a column or a table. A step that touches **who can reach what** is checked before it is kept.

## The access check

Some upgrade steps (the ones that change how agents, services, connections or credentials are stored) run under a check:

1. Agent Core copies the database beside the original (`pre-migration-<step>-<time>.db`, owner-only, the two newest are kept).
2. In one transaction it records access as the running code computes it: which connections each agent can see and run; which credentials each agent can get a reference to, by MCP and by the local credential helper; which credentials each service may fetch, with read or write and expiry; each key's hash; each credential's owner, name, reference name and a hash of its stored (encrypted) value; each connection's settings.
3. It applies the step and records access again.
4. If anything differs that the step did not say it would change, it **rolls the whole transaction back and does not start**. The database is exactly as it was, at the previous version. A report (ids, names and counts only, no secret values) is written to `migration-reports/` next to the database, and the start-up error names it.
5. Otherwise the step is recorded and kept.

Restoring a backup from an older version runs the same steps on the restored copy before it replaces the live database. If the check refuses, the restore is not carried out and the running installation is untouched.

To see what an upgrade will do without changing anything, run the check against a copy:

```
python -m app.access_check /path/to/agent-core.db
```

The file is opened read-only and a temporary copy is migrated and deleted. The output is the counts before and after, any difference, and the kind given to each entry.

To see exactly what the stricter local credential helper would take away, add `--helper-change`:

```
python -m app.access_check /path/to/agent-core.db --helper-change
```

It lists, by entry, the credentials the helper could fetch before and the ones it can fetch after. Only removed reach is expected; anything else is reported as unexpected.

## What 1.4.0 changes

### Who can use a connection is now a list

Each connection keeps its single owner scope (who may edit it) and gains a list of who may use it. The upgrade fills every list from the connection's scope, so that no agent gains or loses anything: a named agent for an agent-owned connection, the workspace for a workspace-owned one, and all of the person's agents for a personal one, with an exclusion for each agent whose own switch was off.

The step runs under the access check above: it compares, for every agent and every connection, whether the agent can see and run it under the old rule and under the new one, and it is refused, with nothing changed and the application not starting, on any difference.

The old per-agent switch still works and is kept in step: turning it off excludes the agent from its person's "all agents" connections, and a new agent starts excluded unless you include it. Creating or updating a connection with only an owner scope means what it always meant. The audience endpoints and the audience field are for administrators only in 1.4.0.

### The local credential helper is stricter

The internal resolution endpoint now applies the same rule as the MCP `credential_get` tool: the named entry must be an active agent that holds the **Reference credentials** permission, besides the scope and expiry checks it already made. A service is refused, because a service fetches with a grant.

Before, the helper checked scope and expiry only, so an entry without the permission, or a service, could resolve what its scopes reached. Every entry reads its person's own scope, so that reached the person's credentials.

A script that depended on the old behaviour now gets `403 PERMISSION_REQUIRED` or `403 NOT_AN_AGENT`, and every refusal is an audit row (`broker_resolve_denied`, with the reason). To fix it, switch the permission on for that agent (Agents > Access > Turn on), or give a service a grant on the credential. `python -m app.access_check <database> --helper-change` lists, on a read-only copy, exactly which entries would lose helper reach.

### Agents and services are told apart

The split needs nothing from you. Existing entries keep every permission, workspace, key and secret they had; the upgrade only adds the column that says which kind an entry is, and checks that access is identical afterwards. An entry is marked as a service only when its one permission is Fetch secrets and it has no recorded work, workspaces, credentials or connections of its own.

### A service cannot own a connection

Creating a connection with a service as its owner, or changing a connection's owner to a service, is refused (`SERVICE_CANNOT_OWN`). Connections you already have are not changed.

## If an upgrade is refused or fails

Nothing needs repairing: the step was rolled back, so the database is at the previous version.

1. Read the report named in the error (`migration-reports/`).
2. Start the previous image or version again. With the provided `deploy.sh` this happens by itself and it ends with "rolled back and healthy; no migration ran".
3. Send the report to the project (it holds ids and names only) so the step can be fixed. Do not edit the database by hand.

A step the database cannot make atomic (rebuilding a table) is the one case where an upgrade could stop half way. None of the guarded steps does that: they add columns and tables only. If you ever see "database may be at the newer schema" from `deploy.sh`, restore in this order:

1. Stop Agent Core.
2. Copy the snapshot taken before the deploy (named by `deploy.sh`) or the newest `pre-migration-*.db` over the database file, and delete the `-wal` and `-shm` files beside it.
3. Start the previous image.
4. Check `/health` and that the highest number in the `schema_migrations` table is the one from before the upgrade.

## Going back to an older version

An older build ignores new columns. Access you changed after upgrading with the newer build is not seen by the older one: anything the older build reads (an agent's permissions, a connection's single owner scope, a service grant) is what it reads. The changelog says, per version, what that means.

**Going back after changing who can use a connection.** Builds before 1.4.0 read the connection's owner scope and the per-agent switch, not the list. Changes made to the list afterwards are not seen by them, and an entry you removed is still granted by the old scope rule, so rolling back can restore access you had taken away. Rolling back right after upgrading, before any change, loses and gains nothing.

**Going back to 1.3.2.** That build ignores the column that says which kind an entry is, so a service created while it runs is stored as an agent and is not reclassified on the next upgrade. Change it to a service from its Edit dialog.
