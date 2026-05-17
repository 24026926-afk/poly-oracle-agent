Multi-Agent Audit Protocol — runs before every commit. Usage: /maap WI-04

Steps:
1. Run: `python -m pytest --asyncio-mode=auto tests/ -q` — ABORT if any test fails
2. Run: `python -m coverage report -m` — ABORT if coverage < 80%
3. Run: `git diff HEAD` and review all changes
4. Audit checklist (flag any violation — do NOT proceed if found):
   - [ ] No `float` for money, odds, position sizing, or PnL — `Decimal` only
   - [ ] No direct DB session or raw SQL in agent code — repository classes only
   - [ ] No `LLMEvaluationResponse` Gatekeeper bypass before execution routing
   - [ ] No `dry_run` bypass before signing, broadcasting, or order mutation
   - [ ] No schema drift outside Alembic migrations or `Base.metadata.create_all()` in runtime code
5. If all clear → print: "✅ MAAP cleared — run /wi-done $ARGUMENTS 'feat(wi-XX): description'"
6. If findings → list them, block commit until fixed
