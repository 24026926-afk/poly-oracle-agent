Closes the current phase.

Usage: /phase-close 2

Steps — execute ALL in sequence, ABORT and report on any failure:

1. Run: `python -m pytest --asyncio-mode=auto tests/ -q`
   - ABORT if any test fails. Phase cannot close with red tests.

2. Determine N = $ARGUMENTS (phase number, e.g. "2")

3. Verify the phase PRD exists:
   - Check `docs/PRD-v{N}.0.md`
   - ABORT if the file does not exist

4. Generate `~/documents/integration_task/04_Archive/poly-oracle-agent/Phase-{N}/PHASE-{N}-COMPLETE.md` with:
   - Close date: today
   - Phase name and objective (read from PRD Section 1)
   - All WIs: name, branch, final commit hash, test count (read from STATE.md)
   - Final coverage and develop HEAD commit
   - All invariants from PRD Section 4 (Phase DoD)

5. Update `STATE.md`:
   - Change active phase status to `Phase {N} COMPLETE — archived`
   - Add `Phase {N} Archive` block referencing `docs/PRD-v{N}.0.md` and `PHASE-{N}-COMPLETE.md`
   - Set active work item to `None — awaiting Phase {N+1} PRD`

6. Append to `~/documents/integration_task/03_Daily/{today}.md`:
   "🏁 Phase {N} archived → 04_Archive/poly-oracle-agent/Phase-{N}/PHASE-{N}-COMPLETE.md"

7. Run:
   `git add docs/ STATE.md`
   `git commit -m "docs(archive): phase {N} complete — generate PHASE-{N}-COMPLETE.md"`

8. Print: "📦 Phase {N} archived to ~/documents/integration_task/04_Archive/poly-oracle-agent/Phase-{N}/PHASE-{N}-COMPLETE.md"
