# Daily Session Log
Write a session summary to the Obsidian vault. Run at the end of every session.

1. Create or append to `~/documents/integration_task/03_Daily/$(date +%Y-%m-%d).md`
2. Use this exact format:
   ```
   ## [HH:MM] Session Summary
   - Agent: Qwen Code
   - Active WI: [WI-XX or "none"]
   - Actions taken: [bullet list]
   - Files created/modified: [list with vault-relative paths]
   - Blockers or decisions: [any]
   - Next: [next action]
   ```
3. Update `STATE.md` with current WI status and metrics
4. Confirm with: "Memory Consolidation Complete — log written to ~/documents/integration_task/03_Daily/YYYY-MM-DD.md"
