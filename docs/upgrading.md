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
