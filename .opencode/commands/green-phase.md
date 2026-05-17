Implements a Work Item step by step until all tests pass. Usage: /green-phase WI-04

Steps:
1. Read `~/documents/integration_task/poly-oracle-agent/docs/deliverables/implementation_prompts/prompt_$ARGUMENTS-*.md`
2. Read `~/documents/integration_task/poly-oracle-agent/docs/deliverables/business_logic/business_logic_$ARGUMENTS-*.md`
3. For each implementation step in the prompt:
   a. Implement ONLY that step — no jumping ahead
   b. Run: `python -m pytest --asyncio-mode=auto tests/ -q`
   c. If any test fails, fix before moving to next step
4. When all steps done, run:
   `python -m coverage run -m pytest tests/ --asyncio-mode=auto`
   `python -m coverage report -m`
5. If coverage < 80% — add missing tests before proceeding
6. Print: "🟢 Green Phase complete. Run /maap $ARGUMENTS."
