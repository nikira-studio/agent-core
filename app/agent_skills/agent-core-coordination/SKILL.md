# Agent Core coordination

Start with `workspace_sync` for the workspace scope and reuse its execution ID. Search memory for stable project facts and decisions. Write activity updates while working. A fresh activity can return a bounded since_last_active fallback digest when the execution is not caught up; process it without treating it as complete history. Complete the activity with a short result when done.
