# Claude Code Context — poly-oracle-agent

See AGENTS.md for full project rules, constraints, and class name reference.
See docs/PRD-v4.0.md for Phase 4 scope.
See STATE.md for current WI checklist and status.
Consult docs/archive/ARCHIVE_PHASES_1_TO_3.md for legacy constraints (saves tokens vs. reading old PRDs).

Current branch: develop
Active phase: Phase 9 Complete — Operator Safety & Telemetry
Completed: WI-11 through WI-28 ✅ (Phases 4, 5, 6, 7, 8, and 9 sealed)
Current WI set: WI-28 complete; Phase 10 planning next; dry-run config boot blocker fixed
Next task: Phase 10 PRD / planning.

Update: STATE.md (metrics/tasks), README.md (env/commands), and CLAUDE.md (status) after each task completion.

## 🛑 MANDATORY DEFINITION OF DONE (DoD)
Before declaring ANY Work Item (WI) or Phase complete, and BEFORE asking the user for the next task, you MUST automatically execute the following Memory Consolidation step without being prompted:
1. Update `STATE.md` with the new test count, coverage, and change the active WI.
2. Document any critical bugs fixed or invariant violations caught during the WI into the appropriate `.agents/rules/` file or `AGENTS.md`.
3. Print a "🧠 Memory Consolidation Complete" summary in the terminal for the user.
4. **PHASE COMPLETION AUTOMATION:** If the completed Work Item marks the end of a Phase (e.g., Phase 4 is complete), you MUST automatically generate a historical archive file before stopping. 
   - Create `docs/archive/ARCHIVE_PHASE_[X].md`.
   - Summarize the pipeline architecture, completed WIs, MAAP audit findings, and critical invariants established during this phase.
   - NEVER modify older archive files like `ARCHIVE_PHASES`.
