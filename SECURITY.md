# Security Policy

Agent Core handles credentials, memory, and local operational state. Treat deployments and backups as sensitive.

## Reporting Issues

This repository is not currently accepting public vulnerability reports through a formal program. If you are using Agent Core privately, report security issues to the project maintainer through your normal private channel.

Do not publish vulnerability details, working exploits, real credentials, backup files, database files, `vault.key`, or `broker.credential` in public issues.

## Supported Version

Only the current `main` branch is supported during the early v1 period.

## Sensitive Files

Never commit:

- `.env`
- `data/`
- `agent-core.db`
- `vault.key`
- `broker.credential`
- backup ZIP files
- logs
- `private/`

## Secret Handling Model

- Vault values are encrypted at rest.
- Normal vault, MCP, dashboard, audit, and metadata export responses do not include raw vault values.
- Agents receive `AC_SECRET_*` reference names, not plaintext credentials.
- The local Credential Broker resolves references only after broker authentication and agent scope validation.
- Backup ZIP files include both the database and vault key, so they are as sensitive as the running vault.

Run a secret scan before publishing:

```bash
gitleaks detect --source . --verbose
```
