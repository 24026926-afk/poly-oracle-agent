Runs when a Work Item passes all DoD checks. Usage: /wi-done WI-04 "feat(wi-04): description"

Steps:
1. Run: `python -m pytest --asyncio-mode=auto tests/ -q` — abort if any test fails, report failures to user
2. Run: `git add src/ tests/ docs/ .claude/ STATE.md README.md AGENTS.md CLAUDE.md QWEN.md`
3. Run: `git commit -m "$ARGUMENTS"` (second argument = commit message)
4. Run: `git checkout develop`
5. Run: `git merge --no-ff feat/wi-$(echo "$ARGUMENTS" | head -1 | tr '[:upper:]' '[:lower:]') -m "merge: $ARGUMENTS into develop"`
6. Update `STATE.md` — mark WI complete, update test count/coverage
7. Print Memory Consolidation summary
8. Read `STATE.md` and check if ALL WIs in the current phase show status COMPLETE.
   - If NOT all complete: stop here.
   - If ALL complete: execute the following automatically:
     a. Verify `docs/PRD-v{N}.0.md` exists (where N = current phase number)
     b. Create `~/documents/integration_task/04_Archive/poly-oracle-agent/Phase-{N}/PHASE-{N}-COMPLETE.md` with:
        - Close date (today)
        - All WIs: name, branch, final commit hash, test count
        - Final coverage and develop HEAD commit from STATE.md
     c. Append to `~/documents/integration_task/03_Daily/{date}.md`: "🏁 Phase {N} archived → 04_Archive/poly-oracle-agent/Phase-{N}/"
     d. Run: `git add docs/ STATE.md`
        Commit as: `docs(archive): phase {N} complete — generate PHASE-{N}-COMPLETE.md`
     e. Print: "📦 Phase {N} archived to ~/documents/integration_task/04_Archive/poly-oracle-agent/Phase-{N}/PHASE-{N}-COMPLETE.md"
