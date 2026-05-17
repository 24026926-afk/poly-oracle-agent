# Create Work Item
You are a Staff Architect. Create the next Work Item brief automatically.

1. Read `QWEN.md` to understand the project purpose and current phase
2. Read `STATE.md` to identify the active WI number and what's pending
3. Read `AGENTS.md` to understand the 4-layer pipeline architecture
4. DO NOT ask the user for the WI number or title — derive it from STATE.md
5. Use Glob `~/documents/integration_task/01_Brief Context/**` to discover brief context files, then create `WI-XX-kebab-title.md` in `~/documents/integration_task/01_Brief Context/` with this structure:

   - **Objective:** One sentence describing what this WI accomplishes
   - **Inputs:** What data/files this WI receives
   - **Outputs:** What this WI produces
   - **Acceptance Criteria:** Numbered list of testable conditions
   - **Anti-Patterns:** What to explicitly avoid (reference AGENTS.md)
   - **Dependencies:** Other WIs this depends on
   - **Target Layer:** Which pipeline layer this WI lives in

6. Update `STATE.md` to reflect the new WI is IN PROGRESS
7. Confirm the file was written and show its full path and content.
