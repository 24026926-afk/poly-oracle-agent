Runs at the start of every Work Item. Usage: /wi-start WI-05

Steps:
1. Run: `git checkout develop && git pull origin develop`
2. Read the active phase PRD in `docs/PRD-v*.md` and locate the WI matching $ARGUMENTS. Extract: title, objective, inputs, outputs, acceptance criteria, dependencies, and target layer. Derive {kebab-title} from the extracted title.
3. Run: `git checkout -b feat/wi-$(echo "$ARGUMENTS" | tr '[:upper:]' '[:lower:]' | sed 's/wi-//')-{kebab-title}`
4. Generate (if not already) `~/documents/integration_task/poly-oracle-agent/docs/deliverables/business_logic/business_logic_$ARGUMENTS-{kebab-title}.md` following the business logic format — NO code, only: objective, data models (Pydantic schema names only), key rules, edge cases, and invariants.
5. Generate (if not already) `~/documents/integration_task/poly-oracle-agent/docs/deliverables/implementation_prompts/prompt_$ARGUMENTS-{kebab-title}.md` following WI-TEMPLATE.md exactly — NO code, only: session context, objective, inputs, outputs, acceptance criteria, anti-patterns, dependencies, and target layer.
6. Show both generated files to the user and wait for approval before proceeding.
7. Only after user approves: commit both files as `docs(wi): add deliverables for $ARGUMENTS` and print: "✅ Branch feat/wi-$ARGUMENTS-{kebab-title} ready. Begin Step 0 of prompt_$ARGUMENTS-{kebab-title}.md"

Rules:
- NEVER write code in business_logic/ or implementation_prompts/ files
- NEVER proceed to Step 7 without explicit user approval of both files
- Filename format: `prompt_WI-{NUMBER}-{kebab-title}.md` and `business_logic_WI-{NUMBER}-{kebab-title}.md`
