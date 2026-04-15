# Claude Code Context — poly-oracle-agent

See AGENTS.md for full project rules, constraints, and class name reference.
See docs/PRD for Phase scope.
See STATE.md for current WI checklist and status.
Consult docs/archive/ for legacy constraints (saves tokens vs. reading old PRDs).

Current branch: feat/wi30-exposure-limits
Active phase: Phase 10 in progress — WI-30 and WI-32 complete
Completed: WI-11 through WI-32 (except WI-31/WI-33 pending) ✅
Current WI set: WI-30 complete (global exposure gate before routing with SQLite SUM(open positions) and typed SKIP on breach), WI-32 complete; full regression green at 644 tests, 94% coverage.
Next task: Phase 10 remaining WIs (WI-31 risk metrics, WI-33 backtesting) and phase archive seal.

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
