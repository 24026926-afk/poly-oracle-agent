Runs Step 0 of a Work Item — writes failing test stubs. Usage: /red-phase WI-04

Steps:
1. Read `~/documents/integration_task/poly-oracle-agent/docs/deliverables/implementation_prompts/prompt_$ARGUMENTS-*.md`
2. Read `~/documents/integration_task/poly-oracle-agent/docs/deliverables/business_logic/business_logic_$ARGUMENTS-*.md`
3. Derive {kebab-title} from the WI title in the prompt file.
4. Create test file at `tests/unit/test_$ARGUMENTS-{kebab-title}.py` with stubs using `raise NotImplementedError`
5. Run: `python -m pytest --asyncio-mode=auto tests/unit/test_$ARGUMENTS*.py -v` — confirm ALL tests fail
6. Print: "🔴 X tests in red — Red Phase complete. Run /green-phase $ARGUMENTS to implement."
