# Agent Core memory

Search memory before writing. Store durable facts and decisions, not routine progress. Keep facts checkable with a subject anchor. Treat duplicate and previously-retracted write warnings as prompts to inspect, not as failures. Confirm a fact only after checking it, and include evidence. Use activity updates for work status and temporary notes.

Use `memory_search(max_response_chars=6000)` when recall needs a bounded response. The budget includes pretty-printed JSON metadata and returns whole records in rank order. `response_limited: true` means the requested page did not fully fit. Advance `offset` by the number returned to continue; if `RESPONSE_BUDGET_TOO_SMALL` accompanies an empty page, raise the budget. Over MCP, omitting the budget trims to the inline size limit the same way. It measures characters, not tokens.
