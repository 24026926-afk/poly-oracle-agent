# Generate Phase PRD and Deliverables
You are a Staff Architect and Senior Python Engineer.

## Phase Structure Rule
- One PRD covers ONE complete Phase (minimum 3 WIs per Phase)
- Group WIs by architectural layer or feature cohesion
- Never generate a PRD for a single WI in isolation

## Execution Steps
1. Read `STATE.md` to identify the current Phase number and active WIs
2. Use Glob `**/WI-*.md` and `**/brief*.md` (case-insensitive) to discover all brief files for this Phase
3. Read `AGENTS.md` for hard constraints and class names
4. Use Glob `**/GLOBAL_STANDARDS.md` to find stack constraints. If not found, skip this step and continue with AGENTS.md constraints only.
5. Determine if the current Phase has >= 3 WIs. If not, propose the missing WIs before generating the PRD.

## Files to Generate
For each WI in the Phase, generate:
- `~/documents/integration_task/poly-oracle-agent/docs/deliverables/business_logic/business_logic_WI-XX-*.md`
- `~/documents/integration_task/poly-oracle-agent/docs/deliverables/implementation_prompts/prompt_WI-XX-*.md`

Plus one Phase-level file:
- `docs/PRD-v{PHASE}.0.md` — Full Phase PRD following this exact structure:
  1. Objective
  2. Scope Boundaries (In scope / Out of scope)
  3. Work Items (one section per WI: Goal, File Structure, Core Requirements, Definition of Done)
  4. Phase Definition of Done (global gate — ALL WI DoDs must pass)
  5. Constraints & Non-Negotiables (reference AGENTS.md hard constraints)
  6. Dependencies to Add (new packages only)
  7. Deliverables Summary (table: WI → file created)
  8. State & Documentation Updates on Phase Completion

## After Generation
- Update `STATE.md` to reflect all WIs in this Phase are ready for implementation
- Confirm all files written with their full paths
