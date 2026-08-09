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

---

## Testing a fix

A regression test is a claim that a specific defect cannot come back. Prove the claim:

**Revert the fix, run the test, and watch it fail.** Then restore the fix and watch it pass. If it passes both ways it is testing something else, and the defect is unguarded no matter how green the suite looks.

This is not a formality. Every one of these ran green against code that was already known to be broken:

- A backup test committed a row and **closed** the connection. SQLite checkpoints when the last connection closes, so the data was in the `.db` file before the backup ran — the very thing the test existed to catch could not happen. Holding the connection open, as a live server always does, made it fail.
- A restore test asserted that a credential resolved afterwards. The keyring accumulates keys, so it resolved whether or not the fix was present. Asserting the actual invariant — the keys cached in the process match the keys on disk — made it fail.
- A concurrency test released one thread before the other had reached the lock they were supposed to contend over, so they never did.
- An event-loop test created its async task but never yielded, so the task started *after* the blocking call and timed the wrong interval.

The pattern is always the same: **the test never established the precondition the defect needs.** Setup that looks reasonable — closing a connection, releasing a lock, creating a task — can quietly remove the only circumstance under which the bug appears.

When a test genuinely cannot be made to fail against the old code, say so in the test docstring rather than implying coverage that is not there.

---

If this project is opened to outside contributors later, this file should be replaced with a standard issue, pull request, test, style, and security reporting workflow.
