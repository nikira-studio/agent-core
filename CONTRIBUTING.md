# Contributing

Agent Core is early-stage software. External contributions are not currently accepted.

For private development:

1. Create a branch.
2. Keep local runtime data in `data/` and private notes in `private/`.
3. Run the test suite:

   ```bash
   pytest -q
   ```

4. Run a secret scan before sharing code:

   ```bash
   gitleaks detect --source . --verbose
   ```

5. Do not commit `.env`, `data/`, `private/`, backup ZIPs, logs, or generated caches.

If this project is opened to outside contributors later, this file should be replaced with a standard issue, pull request, test, style, and security reporting workflow.
