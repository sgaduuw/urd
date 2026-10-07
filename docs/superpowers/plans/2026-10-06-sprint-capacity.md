# Sprint Capacity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Implement the approved team-and-sprint availability, forecast and delivery workflow in the local URD application.

**Architecture:** Keep local planning inputs in durable DuckDB tables with immutable JSON revisions, and reconstruct Jira evidence offline from raw records. Use the existing Flask forms, request cursors and project lock. Add a fixed Agile GET path to the existing client; avoid a second client, frontend framework or event system.

**Tech Stack:** Python 3.11+, standard library, DuckDB, Flask and native HTML forms.

**Spec:** ../specs/2026-10-06-sprint-capacity-design.md

**Authorization:** User requested implementation on 2026-10-06. Worktree branch: `docs/sprint-capacity-spec`. Base: `9d5f2784628f121cd58bb9dfa9ca833d28eaddc4`. Tracking issue: https://github.com/sgaduuw/urd/issues/34. No commits or publication without separate authorization.

## Global Constraints

- Jira remains GET-only with independent trusted-host validation and redirect rejection.
- No new dependencies; no real Jira data, names or captured responses in source or fixtures.
- No hidden defaults for hours, focus, timezone, roster, components or selected history.
- Unknown historical facts remain unknown. A partial known sum is never labelled complete.
- Use actual sprint start inclusive and completion exclusive. Subtasks use their own scope and completion.
- Apply subtask point precedence before team, sprint and completion filters.
- Saved forecasts and original confirmations never change on refresh.
- Preserve local records across derive, pruning, restart and backup.
- Observe meaningful tests fail before implementation. Run the existing full gate at the end.
- Keep changes uncommitted as required by workspace instructions.

## Review Focus

1. A valid empty scope is different from unknown evidence; missing history must not manufacture zero (Task 2).
2. A request opened before refresh or another edit cannot overwrite newer data or confirm a different preview (Tasks 1 and 4).
3. Estimated children outside the reporting scope still suppress parent points (Task 2).
4. Catalogue pagination can stall, truncate or fail after a page; retain the old cache atomically (Task 3).
5. Default exports must contain no personal grid data or free-text revision reasons, including hidden payloads (Task 5).

## Task 1: Durable teams, plans, revisions and capacity calculations

**Files:** Create `capacity.py`, `test_capacity.py`; modify `urd.py:open_db`.
**Interfaces:**
- `capacity.init_db(con)`: idempotent durable schema creation during database open.
- `capacity.save_team(con, data, team_id=None, expected_version=0) -> dict`: stable identity, versioned team configuration.
- `capacity.new_plan(con, team_id, sprint_id, start, end) -> dict`: copies roster, weekly hours, scope, timezone and catalogue metadata; never reuses sprint exceptions.
- `capacity.totals(payload, complete=False) -> dict`: available/work/focus hours plus missing inputs; strict finite numeric validation.
- `capacity.save_plan(con, payload, expected_version, action='save', reason='', baseline=None, replaces=None) -> dict`: atomic revision insertion, confirmation and void pointers, optimistic concurrency; baseline supplied by Task 2.
- `capacity.get_plan(con, team_id, sprint_id, version=None) -> dict | None` and `capacity.revisions(con, team_id, sprint_id) -> list`.
- `capacity.forecast(focus_hours, rate) -> float | None` and `capacity.history_rate(sources) -> dict`: weighted rate with frozen provenance; review eligibility supplied by Task 2.

- [x] Write `test_capacity.py`: two teams in one sprint and two sprints in one team remain isolated; weekly patterns seed dates including weekends and blank inputs; edits do not alter existing plans. Assert 31.5 focus hours, then 28.5 after four hours removed.
- [x] Run `uv run --isolated --with-requirements requirements.txt python test_capacity.py`. Expected: failure because capacity API does not exist.
- [x] Implement schema, validation, versioned teams/plans/revisions, immutable confirmation and void/replacement history, frozen forecast sources. Distinguish missing from zero. Validate dates/timezone and member IDs; retain out-of-range entered hours until explicit reconciliation.
- [x] Extend the check for invalid/nonfinite input, meetings greater than availability, stale writes, required revision/void reasons, transaction rollback, immutable original estimates, replacement links, missing/zero/manual rate and weighted 0.24 rate producing 12 points from 50 hours. Run it before each new behavior, observe failure, implement, rerun.
- [x] Run the task test and `uv run ruff check capacity.py test_capacity.py urd.py`. Expected: pass. Record evidence and mark complete without committing.

## Task 2: Historical scope, completion and point evidence

**Files:** Create `capacity_history.py`, `test_capacity_history.py`; modify `urd.py` source metadata/pruning integration and `capacity.py` snapshot consumers.
**Interfaces:**
- `capacity_history.snapshot(con, settings, sprint, cutoff=None, inclusive=True, observed=False) -> dict`: per-issue scope, estimate, classification, parent, status, source timestamp and uncertainty; stable source identity.
- `capacity_history.baseline(con, payload, now=None) -> dict`: observed preview before closure or reconstructed sprint-start baseline at/after closure, with separate confirmation kind and cutoff.
- `capacity_history.review(con, payload, baseline) -> dict`: original dispositions; added and unclassified buckets; selected known points, point coverage, estimates/conversions; eligible historical rate input.
- `capacity_history.source_id(con) -> str`: identifies source rows/metadata and sync markers for stale preview detection.
- Persistent issue-type and status-ID metadata plus pruning coverage live beside local tables; existing report derivation remains unchanged.

- [x] Write synthetic tests for a parent at 8 and children at 3 and 5: 8 delivered, never 16; parent unfinished with one child finished gives 3; explicit zero or out-of-scope child estimate suppresses parent; all known blank children allow parent fallback.
- [x] Run `uv run --isolated --with-requirements requirements.txt python test_capacity_history.py`. Expected: missing API failure.
- [x] Implement reverse reconstruction of scalar fields, component add/remove deltas and sprint membership. Preserve field IDs, validate transition chains, use type-ID classification evidence and direct parent relationships. Unknown type, parent, estimate or component evidence propagates only where relevant.
- [x] Add RED→GREEN cases for completion before/start/closure, Done-to-Done changes, reopen/recompletion, creation in Done, dropped work, missing start/closure and missing history; assert one qualifying completion per sprint.
- [x] Add RED→GREEN cases for retrospective original and added work, post-close estimate/component/type/reparent changes, baseline cutoff versus confirmation time, mid-sync membership mismatch, re-entry after baseline, original conversion reconciliation, independent complete closing total despite incomplete attribution, pruning and restricted mirrors.
- [x] Run the task test, relevant existing sync and report checks, and Ruff. Expected: pass.

## Task 3: Secure board and sprint catalogue

**Files:** Modify `urd.py:Jira` and `capacity.py`; create `test_capacity_catalogue.py`; extend `test_security.py` if needed.
**Interfaces:**
- `Jira.agile_get(path, params=None)`: fixed `/rest/agile/1.0` prefix, shared trusted transport/retries.
- `Jira.boards(project)` and `Jira.sprints(board_id)`: validated pagination, project-scoped Scrum board discovery.
- `capacity.refresh_catalogue(con, jira, project, board_ids=None)`: explicit network operation; replace complete returned catalogue atomically, retain cache on failure.
- `capacity.boards(con)`, `capacity.sprints(con, board_ids=None)`, `capacity.sprint(con, sprint_id)`: offline cached records, fetch timestamp and board associations.

- [x] Write synthetic paginated empty-future-sprint test, malformed/stalled pagination and second-page permission error tests; verify cache unchanged after failure.
- [x] Run `uv run --isolated --with-requirements requirements.txt python test_capacity_catalogue.py`. Expected: missing API failure.
- [x] Implement fixed Agile path and catalogue persistence. Reject arbitrary paths/URLs and invalid IDs. Reuse the existing final URL host validator, GET requests and redirect rejection.
- [x] Verify offline access and security cases with the new catalogue test and `test_security.py`. Expected: pass. Run Ruff.

## Task 4: Local setup, grid editing, confirmation and review routes

**Files:** Create `views_capacity.py`, `test_views_capacity.py`; modify `webapp.py`, `views_report.py`.
**Interfaces:**
- Register capacity blueprint at `/<slug>/capacity/`.
- Native team form, explicit catalogue refresh, team/sprint selection, plan edit and review, revision history, void and replacement forms.
- All writes use the project lock and own cursor, with atomic capacity methods. Render entered values on validation/conflict/refresh-busy responses.
- Confirmation posts both expected plan version and displayed source identity. Recompute source identity under the lock; changed source requires a fresh preview.
- Reuse `render.CSS` and HTML escaping. Grid has sticky row/date labels, accessible labels and fractional numeric inputs.

- [x] Write route flow test: configure team with normal hours, discover cached empty future sprint, require local dates, edit hours, preview/confirm, revise with reason, inspect original, void and reconfirm.
- [x] Run `uv run --isolated --with-requirements requirements.txt python test_views_capacity.py`. Expected: 404.
- [x] Implement the forms and navigation. Support Jira roster selection and distinct local identities, explicit historical selection/manual rate, date mismatch reconciliation retaining old cells, adding/removing roster rows, copied scope and timezone review.
- [x] Add RED→GREEN checks for stale versions and source previews, busy lock retaining input, same-origin/Host guards, escaped text, validation failures and failure rollback, valid replacement destination protection, retrospective and late labels.
- [x] Run route, webapp and existing report-view tests plus Ruff. Expected: pass.

## Task 5: Shared reports, history rates and recovery documentation

**Files:** Modify `capacity.py`, `capacity_history.py`, `views_capacity.py`, `urd.py:report_html`, `README.md`, `CONTEXT.md`, `AGENTS.md`; create `test_capacity_report.py`.
**Interfaces:**
- `capacity.report_section(con, include_grid=False, plan=None) -> str`: aggregate capacity, forecast provenance, delivery and coverage; explicit grid opt-in; no revision reason export.
- Plan export route sends self-contained read-only HTML. General CLI report includes aggregate capacity only, respects sprint selection by date without truncating a sprint; incompatible component/epic filters link to full plan.
- History selector consumes independently complete Task 2 delivery plus current valid confirmation and positive revised focus hours; stores exact values and source revision IDs.
- Document manual clean shutdown, entire data-volume backup and separate-volume restore/verification.

- [x] Write export privacy test containing identifiable synthetic member/reason sentinel text. Default export contains neither sentinel nor grid payload; grid option includes only the member/grid, never reason.
- [x] Run `uv run --isolated --with-requirements requirements.txt python test_capacity_report.py`. Expected: missing reporting API failure.
- [x] Implement aggregate report integration, filter/overlap notices, point coverage, forecast provenance and voided-source warnings. Preserve original/added/unclassified reconciliation and type counts.
- [x] Add RED→GREEN synthetic backup test: save confirmation/revision/void/replacement and frozen historical forecast, close connections, copy entire volume, restore separately, derive and reopen, compare saved records. Test source correction/void does not rewrite an existing forecast and voided source cannot enter a new forecast.
- [x] Update repository scope documentation to permit the approved local planning and Agile read paths; document no spreadsheet import and recovery steps.
- [x] Run task checks and Ruff. Expected: pass.

## Task 6: Integration and independent review

**Files:** All changed files; `tests/run.sh` if explicit file-presence list needs the new checks.
- [x] Run `sh tests/run.sh`, `git diff --check`, and inspect tracked/untracked diff for secrets. Expected: all tests, Ruff, shellcheck, leak guard and whitespace pass.
- [x] Exercise local browser flow with synthetic data if a browser tool is available; otherwise verify complete Flask HTML flows and record that limitation.
- [x] Dispatch one fresh whole-diff review using the requesting-code-review skill, including this plan, the spec, the full uncommitted diff and new files. Review evidence gaps and security deliberately.
- [x] Reproduce material findings with failing tests, fix in one pass, run relevant tests and full gate. Record decisions and verification.
- [x] Leave changes uncommitted. Report worktree, tests, material limits and proposed Conventional Commit message.

Completion: implemented and verified on 2026-10-06, uncommitted in the named worktree. Full gate passed all 22 executable test scripts, Ruff, shellcheck and leak checks. Headless Chromium confirmed planning, revision and export privacy. Six independently reproduced Important findings were fixed with regression checks; no deferred findings. Jira catalogue transport was verified with synthetic responses; no live catalogue request or deployment was performed.

## Task 7: Daily Work and Meetings split (approved 2026-10-07)

The user approved daily Work excluding Meetings, focus applied only to Work,
and the same split in normal weekly patterns. They subsequently clarified that
the earlier capacity format never reached production. It requires no migration
or compatibility. This supersedes the earlier preservation and re-entry
requirements for that prototype format.

**Files:** `capacity.py`, `views_capacity.py`, existing capacity tests and browser
smoke; this plan, the design spec and README.
**Ownership:** Naomi owns code and tests; parent owns documentation and final
verification. Issue: https://github.com/sgaduuw/urd/issues/34. Keep changes uncommitted.

**Interfaces:** Preserve existing public function signatures. Each plan member
uses `daily: {date: {work, meetings}}`; normal `weekly` patterns contain seven
`{work, meetings}` objects. Use this one representation for calculations, forms,
reports, revisions and recovery. Remove old-format branches, reference fields,
migration guidance and compatibility-only tests.

- [x] Implement paired native Work/Meetings inputs in each day for sprint and
  weekly grids. Remove the standalone Meetings column and explain the inputs.
- [x] Verify arithmetic: four days at A=7 Work+1 Meeting and B=3.5 Work+0.5
  Meeting yield 48 available, 42 Work and 31.5 focus at 75%. Removing 4 Work
  hours yields 28.5 focus. Check blank versus zero, finite/nonnegative values,
  combined daily 24-hour limit, independent copies and retained excluded dates.
- [x] Preserve input values through intermediate form actions and failed saves.
  Confirmed revisions and frozen forecasts remain immutable.
- [x] Remove unreleased-format compatibility and replace mixed-format test
  fixtures with daily inputs while retaining forecast, export privacy and
  backup/restart coverage. Observe a focused failing check before the cleanup,
  then run affected scripts and Ruff.
- [x] Run the full 22-script gate, browser flow, whitespace and leak checks.
  Review the bounded cleanup delta and fix material regressions.

Completed and verified on 2026-10-07. All 22 test scripts, Ruff, shellcheck,
leak and whitespace checks pass. Headless Chromium confirms planning,
confirmation, adjustment and export privacy. The independent bounded cleanup
review found no material issues. Changes remain uncommitted in the existing
worktree; no data files were changed.
