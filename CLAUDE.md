# CLAUDE.md — DFS Pipeline

Persistent context for Claude Code sessions on this repo. Read this first, every session.

## What this project is

A DFS (daily fantasy sports) pipeline for NFL. Data ingestion → projections → contest simulation → lineup selection, with an experiment ledger measuring whether any of it actually produces edge.

Owner is a solo builder with a full-time job. Python/pandas/SQL background, comfortable with data engineering, newer to ML. Evenings-and-weekends pace. **Scope discipline matters more than cleverness** — see the monthly cadence in `docs/dfs-roadmap-v2.md`.

## Current scope (through January 2027)

NFL **Showdown** slates and **soft-field contests only**. Not Sunday main-slate large-field GPPs. One modeling improvement per month. Anything outside this is in the parking lot — if a task seems to require it, stop and flag rather than expanding scope.

## Read these before working

- `docs/dfs-roadmap-v2.md` — current plan. Supersedes Phase 2+ of the July roadmap.
- `docs/phase1-spec.md` — data foundation spec. Phase 1 stands as written.
- `TASKS.md` — the work queue. Pick the top unblocked task.
- `docs/data-sources.md` — known data quirks and workarounds.

## Non-negotiable principles

1. **No look-ahead.** At week W, only data knowable before week W's lock may influence week W's output. Every model/backtest change must keep the leakage test green. This is the one bug that silently invalidates everything.
2. **Archive raw, always.** Every fetched or downloaded file lands in `data/raw/` untouched before parsing. Parsers have bugs; source data disappears.
3. **Fail loudly.** Unexpected CSV columns, partial downloads, join coverage below threshold — raise, don't warn-and-continue. A quietly wrong number is worse than a crash.
4. **Idempotent ingestion.** Re-running any ingest must not duplicate rows. `INSERT OR REPLACE` on real primary keys.
5. **ROI is the metric, not MAE.** A model change ships only if the contest sim says it improves expected ROI on our contest types. Lower projection error is a proxy, not the goal.
6. **Never auto-enter contests.** Code may recommend lineups and prepare CSVs. A human reviews and uploads. No exceptions — this is real money.

## Conventions

- Canonical player ID = GSIS ID. All vendor names route through `id_crosswalk`.
- Filenames: `{season}-w{week:02d}_{source}.{ext}`, e.g. `2026-w01_dk_showdown_sea_ne.csv`
- Secrets in `config.local.yaml` (gitignored), deep-merged over `config.yaml`. Never hardcode keys.
- DK Showdown exports list every player twice (CPT and FLEX rows). **Parse the FLEX/UTIL row for base salary**; the 1.5x multiplier is applied in code.
- Every lineup that gets entered is logged in the ledger with the git commit hash that built it. If it isn't committed, it isn't entered.

## Definition of done for any task

- Tests pass, including the leakage test
- New behavior has at least one test
- Raw inputs archived, ingestion idempotent
- `TASKS.md` updated: task moved to Done with a one-line result note
- Anything that surprised you added to `docs/data-sources.md`
- If the task revealed a blocker only the owner can clear, add it to the Human-only queue in `TASKS.md`

## When to stop and ask

Stop and write the question into `TASKS.md` under "Blocked — needs owner" rather than guessing, when:
- A task needs a paid subscription, account, or login
- A task needs a judgment call about money, stakes, or contest selection
- Data contradicts an assumption in the roadmap (e.g. standings files don't contain ownership)
- The work would expand scope beyond Showdown/soft-field
- A "fix" would require weakening the leakage guarantee

Do not silently work around these. A wrong assumption compounding for three sessions costs more than one blocked evening.
