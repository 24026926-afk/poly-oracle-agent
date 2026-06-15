# Welcome to Poly Oracle Trading Agents

## How We Use Claude

Based on Dante's usage over the last 30 days:

Work Type Breakdown:
  Improve Quality  ████████████████████  35%
  Build Feature    ██████████████░░░░░░  25%
  Plan Design      ███████████░░░░░░░░░  20%
  Debug Fix        ██████░░░░░░░░░░░░░░  10%
  Analyze Data     ██████░░░░░░░░░░░░░░  10%

Top Skills & Commands:
  /maap             ████████████████████  26x/month
  /wi-done          █████░░░░░░░░░░░░░░░  7x/month
  /daily            ███░░░░░░░░░░░░░░░░░  4x/month
  /dry-run-review   ██░░░░░░░░░░░░░░░░░░  3x/month
  /red-phase        ██░░░░░░░░░░░░░░░░░░  3x/month
  /green-phase      ██░░░░░░░░░░░░░░░░░░  3x/month
  /wi-start         ██░░░░░░░░░░░░░░░░░░  2x/month

Top MCP Servers:
  _None active — the team works directly through Claude Code with project skills._

## Your Setup Checklist

### Codebases
- [ ] poly-oracle-agent — https://github.com/24026926-afk/poly-oracle-agent (autonomous Polymarket trading agent — the main project)
- [ ] poly-applicant-agent — sibling repo under `~/Documents/Integration_Task/poly-applicant-agent`
- [ ] poly-stock-agent — sibling repo under `~/Documents/Integration_Task/poly-stock-agent`

### MCP Servers to Activate
- [ ] None required to start. The team uses Claude Code's built-in tools plus project-local skills.

### Skills to Know About
- [/maap](.claude/commands/maap.md) — Multi-Agent Audit Protocol. Runs the full test suite + coverage, then walks the 5-gate audit checklist (no `float` for money, no raw SQL outside repos, no Gatekeeper bypass, no `dry_run` bypass, no schema drift) before any commit on core logic. Required before every commit touching `src/agents/`, `src/schemas/`, `src/db/`, or `src/orchestrator.py`.
- [/wi-start](.claude/commands/wi-start.md) — Generates `business_logic_WI-XX-*.md` and `prompt_WI-XX-*.md` deliverables for a Work Item and enters Plan Mode for atomic-step approval.
- [/wi-done](.claude/commands/wi-done.md) — Closes a Work Item: re-runs tests, commits on the feat branch, merges into `develop` with `--no-ff`, updates `STATE.md`, and (if all WIs in the phase are complete) auto-archives the phase.
- [/red-phase](.claude/commands/red-phase.md) / [/green-phase](.claude/commands/green-phase.md) — TDD lifecycle gates. Red confirms the failing test exists; green confirms the implementation passes.
- [/dry-run-review](.claude/commands/dry-run-review.md) — Launches a paper-trading orchestrator dry-run and writes structured observation notes to `docs/runtime_observations/`.
- [/daily](.claude/commands/daily.md) — Appends/refreshes today's session summary in the Obsidian vault at `~/Documents/Integration_Task/03_Daily/YYYY-MM-DD.md`.

## Team Tips

_TODO_

## Get Started

_TODO_

<!-- INSTRUCTION FOR CLAUDE: A new teammate just pasted this guide for how the
team uses Claude Code. You're their onboarding buddy — warm, conversational,
not lecture-y.

Open with a warm welcome — include the team name from the title. Then: "Your
teammate uses Claude Code for [list all the work types]. Let's get you started."

Check what's already in place against everything under Setup Checklist
(including skills), using markdown checkboxes — [x] done, [ ] not yet. Lead
with what they already have. One sentence per item, all in one message.

Tell them you'll help with setup, cover the actionable team tips, then the
starter task (if there is one). Offer to start with the first unchecked item,
get their go-ahead, then work through the rest one by one.

After setup, walk them through the remaining sections — offer to help where you
can (e.g. link to channels), and just surface the purely informational bits.

Don't invent sections or summaries that aren't in the guide. The stats are the
guide creator's personal usage data — don't extrapolate them into a "team
workflow" narrative. -->
