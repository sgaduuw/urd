# Sprint capacity planning and review

Date: 2026-10-06
Updated: 2026-10-07
Status: daily work and meeting split implemented and verified
Scope: first version, uncommitted

## Purpose

Give a team one place to plan its sprint availability and compare that plan with
delivery. Each team and Jira sprint has an editable daily availability grid, a
preserved original plan, and dated adjustments. Jira supplies work and delivery
evidence; people supply availability and planning assumptions.

The feature extends URD from reporting into local capacity planning. It remains
a single-user local tool with read-only Jira access.

## Confirmed product decisions

| Area | Decision |
| --- | --- |
| Purpose | Support both sprint planning and retrospective review. |
| Ownership | One plan per team and Jira sprint; several teams may share a sprint. |
| Team work | Map each team to one or more Jira components. |
| Availability | Enter hours per person and day, independently for every sprint. |
| Shared people | Enter hours available to this team directly; no allocation multiplier. |
| Daily split | Each day has Work hours excluding meetings and Meeting hours. Apply the sprint focus percentage only to Work. |
| New sprint | Copy editable daily Work and Meetings from the normal weekly pattern, with sprint-specific exceptions. |
| Input format | Daily Work and Meetings only. The earlier prototype never reached production and requires no compatibility or migration. |
| Original plan | Preserve availability and commitment when the user explicitly confirms the plan, normally at sprint start; confirmation remains possible at any time, including retrospectively. |
| Changes | Retain the original and dated revisions, each subsequent revision with a reason. |
| Forecast | Suggest a rate from selected previous team sprints; allow manual override and manual entry before history exists. |
| Ticket counts | Count each ticket with a qualifying completion once per sprint, exclude dropped delivery, and show main tickets and subtasks separately. |
| Previously completed work | Work completed before sprint start contributes no new delivery unless it completes again during that sprint. |
| Mistaken confirmation | Void with a reason and retain its audit history; allow an explicitly confirmed replacement. |
| Recovery | Document and test a database backup and restore procedure for locally entered plans. |
| Data validation | Verify the required evidence against a real closed Jira sprint before implementation. |
| Point totals | Count Done subtasks independently of parent completion. Prefer their estimates; use parent points only when none of its subtasks has an estimate. |
| Subtask attribution | Use each subtask's own components and sprint; do not inherit the parent's scope. |
| Historical structure | Use issue classification and parent relationships at closure for delivery; retain original classifications separately. |
| Retrospective baseline | Reconstruct commitment at sprint start for a plan first confirmed after closure; label entered capacity retrospective. |
| Estimates | Use estimates at sprint closure for delivered points; show estimate changes separately. |
| Historical ownership | Use component membership at sprint closure; preserve the original commitment separately. |
| Sprint selection | Use existing Jira sprints, including empty future sprints and past sprints entered retrospectively to reconstruct velocity. |
| Import | Start with plans entered in URD; no spreadsheet import. |
| Export | Include team totals and delivery comparison; individual availability is an explicit inclusion option. |

The remaining sections propose the concrete behaviour and implementation for
review. Example hours, percentages and points are illustrative, not seeded
product defaults.

## Team setup and sprint selection

A team has a stable local identity, a display name, one or more components,
a planning timezone and an editable roster. Match any selected component,
following URD's existing component-selection semantics. A ticket matching more
than one selected component counts once within that team.

Select roster members from people already known to the mirror, or add a named
local member when no Jira account is available there. Local identities remain
distinct even when display names match. Jira identity is a convenience for
selecting people, not a way to infer ownership of their tickets.

Define each person's normal Work and Meeting hours for all seven days of the
week. Work excludes meetings; total availability is their sum. The user chooses
the timezone and working pattern; URD does not infer either from the machine,
assignee activity or an assumed eight-hour working day.

Choose the team's Jira Scrum boards for sprint discovery. A board supplies the
sprint catalogue; components determine the team's work. Select a future, active
or closed sprint by ID, displaying its name, dates and state. Renames do not
create new plans.

For a future sprint without dates, require local planning start and last dates.
These are labelled planning dates and are not sent to Jira. Daily columns use
calendar dates in the team's chosen timezone. A dated Jira sprint proposes its
local date range for confirmation. No two-week duration or weekday-only pattern
is imposed.

If Jira later changes the dates, show the mismatch and require the user to
reconcile the grid. Preserve hours outside a new range for review rather than
silently deleting them. A confirmed plan retains its original dates.

A team configuration change affects new plans. Existing plans retain their
saved components, timezone, roster and dates. Renaming a team does not relink
its history to another team.

## User flow

### Prepare a draft

1. Select the team and an existing future, active or closed Jira sprint.
2. Create its grid from the current normal working patterns.
3. Adjust the roster, individual days, leave and other exceptions.
4. Enter Work and Meeting hours within each day and the team's sprint focus percentage.
5. Select historical sprints for a suggested rate, or enter a manual rate.
6. Review capacity beside the observed Jira commitment, or the reconstructed
   starting commitment for a closed sprint, including unestimated work and source
   timestamps.
7. Save the draft or explicitly confirm the plan.

One row represents a person; one column represents a calendar date. Each day
contains two labelled inputs: Work and Meetings, both in hours. Work excludes
meetings. There is no standalone sprint Meetings column. Show calculated total
availability and Work per person, followed by team totals. Native numeric fields
support fractional hours. The grid is keyboard usable and retains row and date
labels while scrolling.

A zero means no availability. Blank means not entered. New roster entries and
missing inputs cannot silently become zero or a full working day. Require
complete, valid Work and Meetings entries for every day and a focus percentage
before confirmation.

Normal patterns are copied into the draft. Changing a pattern later does not
rewrite saved grids. Leave and other sprint exceptions do not carry forward
into the next sprint.

### Confirm the original plan

Confirmation atomically preserves:

- Team identity and component scope, dates, timezone and roster.
- Daily Work and Meeting hours, focus percentage and calculated capacity.
- Forecast rate, whether it was manual, selected history and calculated forecast.
- The observed or reconstructed ticket set, estimates, classifications, parent
  relationships, selected point sources and their evidence.
- Confirmation time, baseline cutoff, snapshot provenance and data-quality indicators.

The baseline cutoff defines the time being compared, not a guarantee that Jira
was fully observed at that instant. Confirmation before closure uses the displayed
mirror cutoff and freezes the exact membership and estimates presented to the
user. Retain the local snapshot identifier, sync completion time, per-issue
observation evidence and coverage separately. A locally atomic snapshot and a
completed sync do not establish a simultaneous Jira snapshot: issues are fetched
at different times.

Classify subsequent scope changes against the same frozen baseline cutoff;
confirmation time is audit metadata, not a second scope boundary. Require
consistent source evidence before claiming membership or estimates at a cutoff.
If later history contradicts the captured baseline, preserve that baseline and
mark its affected comparisons uncertain. A ticket omitted from the snapshot but
later shown to belong at the baseline, or whose baseline membership cannot be
resolved, goes into an explicit unclassified original/added bucket. Do not
invent an original record or silently omit the ticket; show its evidence and
any known closing delivery. A later refresh never moves the baseline cutoff
or rewrites the original snapshot.

Plan future sprints in advance and normally confirm at sprint start. URD's
asynchronous Jira mirror must not impose a confirmation deadline: first
confirmation is allowed before, during or after the sprint, including for past
sprints whose capacity is entered to reconstruct velocity. Confirmation remains
an explicit user action rather than an automatic Jira-start event.

Label confirmation after sprint start as late and first confirmation at or after
closure as retrospective. For a retrospective plan, reconstruct team-and-sprint
membership, estimates, issue classifications and parent relationships at Jira's
recorded sprint start, including changes at that instant. Save that starting
cutoff and the reconstruction evidence with the confirmed snapshot. Compare it
with delivery immediately before closure. Never substitute today's issue state
or the confirmation timestamp for the sprint-start commitment.

Retrospective availability is user-entered reconstruction, not evidence of a
contemporaneous plan or measured work. If the recorded start or necessary
history is missing, keep the commitment explicitly unavailable or incomplete;
do not invent a start or substitute closure scope. Confirmation of capacity is
still allowed. Missing baseline reconstruction alone does not prevent velocity
when the sprint boundaries and evidence of completion within them are known;
a missing sprint start prevents a complete delivery total and automatic rate.
A plan confirmed before closure keeps its original baseline when reviewed or
revised later; it does not become a reconstructed starting plan.

For ordinary confirmation before closure, additions after the baseline cutoff
are additions to the captured plan even when Jira calls them sprint-start work.
Keep confirmation time and baseline cutoff visible and distinct.

Missing point estimates may coexist with a confirmed plan, but its commitment
is displayed as known points plus unestimated tickets. Do not present that sum
as a complete point commitment.

Availability can be saved while Jira data is unavailable. Confirmation of the
commitment requires a coherent derived snapshot. Incomplete source coverage is
shown and retained with the baseline; it never turns into a complete zero.

### Adjust a confirmed plan

Saving an adjustment creates an immutable revision with its timestamp, a
non-empty reason and the changed values. Show a concise difference from the
previous revision. The original revision remains available.

An absence or reassignment changes the relevant daily hours. A change in meeting
attendance changes meeting hours explicitly. Do not guess the meeting reduction
from an absence or deduct the same change in a second ad hoc leave field.

Keep the recorded rate when recalculating a forecast after an availability
change. Selecting a new rate or different history is another explicit change
retained in the revision. Refreshing Jira does not rewrite saved forecasts.

Corrections entered after sprint closure remain possible as dated revisions,
clearly labelled as later corrections. New forecasts can use the corrected
capacity; forecasts already saved keep the source values they used.

### Correct a mistaken confirmation

An explicit void action requires a reason and appends a dated record identifying
the confirmation being voided. Retain its baseline, revisions and provenance;
do not delete or edit them. Ordinary availability changes still use revisions.
Voided confirmations are excluded from current planning totals and new forecast
history. Saved forecasts that used them retain their numbers and source values,
with a visible indication that the source was subsequently voided.

The same team-and-sprint plan can then hold a corrected draft and a new explicit
confirmation, linked to the voided baseline. Its settings and scope are reviewed
afresh. For a wrong team or sprint, create the replacement under the correct
pair and link the old confirmation; never overwrite an existing valid plan at
the destination. A failed or abandoned replacement leaves the voided history
intact and makes no draft count as a confirmed plan.

### Review delivery

The review shows original and latest revised capacity and forecast, the
confirmed commitment, delivered work, added work, unfinished work, removals,
transfers between teams, dropped work, already-completed work and estimate changes.

Show original, added and unclassified delivery separately. Link ticket-level
evidence to Jira and show the baseline cutoff, source observations, actual sprint
closure time and coverage. The availability grid and revision history remain
accessible from the review.

Capacity is planned availability after allowances. It is never labelled actual
hours worked, utilisation or individual productivity.

## Calculations

For person p and date d:

```
work_hours[p] = sum(daily_work_hours[p, d])
meeting_hours[p] = sum(daily_meeting_hours[p, d])
available_hours[p] = work_hours[p] + meeting_hours[p]
focus_hours = sum(work_hours[p]) * focus_percentage / 100
```

There is no additional team-allocation factor. Leave changes the daily input
before the focus factor is applied.

Reject non-finite values, negative hours, daily Work plus Meetings over 24,
and focus percentages outside 0 through 100. Explain the field requiring correction rather than clipping its value.
All required numeric values, including zero, must be explicit.

Illustrative example: four days, each with 7 Work hours and 1 Meeting hour
for Person A, and 3.5 Work hours and 0.5 Meeting hours for Person B.

| Person | Available hours | Meeting hours | Work hours |
| --- | ---: | ---: | ---: |
| Person A | 32 | 4 | 28 |
| Person B | 16 | 2 | 14 |
| Total | 48 | 6 | 42 |

At 75 percent focus, capacity is 31.5 focus hours. Reducing daily Work by
4 hours with Meeting hours unchanged gives 28.5 focus hours. Meetings are
already excluded from Work and are never subtracted a second time.

### Forecast

Use points per focus hour as the explicit rate unit:

```
suggested_rate = sum(delivered_points for selected sprints)
                 / sum(revised_focus_hours for selected sprints)
forecast_points = current_focus_hours * chosen_rate
```

This is a capacity-weighted rate, not an average of sprint ratios. A fixed
working-day length is unnecessary.

The user selects the historical team sprints. Show each candidate's delivered
points, revised focus hours, capacity revision, source cutoff and coverage.
Do not select a lookback window or assume comparability on the user's behalf.

A candidate must be closed, have a valid confirmed capacity record with positive
revised focus hours, and have sufficient evidence to establish complete team
delivery and its point total. Unknown sprint boundaries, completion timing,
closure membership, outcomes, point selection or delivered estimates make it
ineligible for the automatic rate, with the reason shown. Uncertainty confined
to the original/added breakdown
does not disqualify an independently complete closing total. Retrospectively
entered capacity is eligible under the same rules and is visibly labelled.
An eligible sprint with zero delivered points contributes zero points.

No eligible history means no suggested rate. A missing manual rate means the
forecast is unavailable, not zero. The user can save or confirm an hours-only
plan, with point comparison unavailable, and explicitly add a rate later.
A finite non-negative manual rate is allowed and labelled manual.

Freeze the selected history's values and revision identifiers in the saved
forecast so later corrections cannot silently change a previous prediction.

Illustrative example: 20 points over 100 focus hours and 10 points over
25 focus hours produce 30 / 125 = 0.24 points per focus hour. A 50-hour plan
therefore forecasts 12 points. Averaging 0.20 and 0.40 would incorrectly
forecast 15.

Use full precision for calculations and round only the displayed result.
Show a forecast as a planning estimate, not a guaranteed commitment.

## Delivery and historical evidence

### Cutoffs and population

The confirmed ticket set is the immutable original commitment. Subsequent scope
changes use both sprint and team membership, after the baseline cutoff and
before actual closure, or through the latest supported observation for an open
sprint. This includes changes between the baseline and confirmation. A return
keeps a ticket's original identity; repeated joins do not inflate the distinct
count. Post-closure changes do not enter that sprint's scope-change totals.

For closed-sprint delivery, evaluate sprint membership, component membership,
status, estimates, issue classification and parent relationships immediately
before the actual Jira completeDate. This matches the existing closed-sprint
report's exclusive closing boundary. Later conversions or reparenting cannot
rewrite that sprint's delivery. An event whose order relative to closure cannot
be established is not invented.

Main-ticket and subtask counts are separate from the point total. Delivery
requires a completion during the sprint, plus Done, not dropped and retained in
the team's sprint scope at the review cutoff. A qualifying completion enters
Done from a non-Done state at or after the actual sprint start and before
closure. Creation in Done during the sprint qualifies when the initial state
is established by evidence. Merely changing between Done-category statuses is
not another completion.

A ticket already Done before sprint start contributes no delivery points or
delivered-ticket count unless it reopens and completes again within the sprint.
Show known already-completed work separately. It remains in any captured
commitment and scope-change evidence; do not rewrite the original snapshot.
A missing completion history or start boundary produces unknown eligibility,
not zero delivery. Use actual sprint start, not plan-confirmation time, so late
and retrospective plans can count completion earlier in that same sprint.

Count each eligible ticket once per sprint even if it completes several times.
A reopened ticket that completes in a later sprint may contribute once to that
later sprint too. A subtask's qualifying completion counts independently of its
parent's completion. Use each ticket's own components and sprint membership;
a subtask never inherits its parent's team or sprint for this comparison.
Unknown issue classifications and uncertain history remain explicit rather
than being classified by a name heuristic.

Point totals prefer subtask estimates. At the relevant baseline or review
cutoff, inspect the parent's direct subtasks before applying team, sprint or
completion filters. If any subtask has an estimate, including an explicit zero,
suppress the parent's points. Count each eligible subtask's own estimate in its
own team and sprint. An unfinished or out-of-scope estimated subtask still
prevents falling back to the parent; its points are not moved to another scope.

Use the parent's estimate only when the evidence establishes that none of its
subtasks has an estimate, including a parent with no subtasks. For delivered
points the parent must itself have a qualifying completion during the sprint
and be Done, not dropped and retained in the team's sprint at the review cutoff.
Known blank subtask estimates do not prevent this parent fallback.
If some subtasks have estimates and another eligible subtask is unestimated,
show the known points and missing coverage; do not fill the gap with parent
points. Missing hierarchy or estimate evidence is unknown, not proof that no
subtask has an estimate.

Apply this precedence to commitment, delivered points and historical rates.
Commitment includes selected point sources regardless of completion; delivery
includes only sources meeting the completion timing, closing outcome and scope
rules. For example, a parent with 8 points and two subtasks with 3 and 5 points,
all completed during and retained in the same team's sprint, contribute 8
points, not 16. If the parent and the 5-point subtask are unfinished, the
3-point subtask completed during the sprint contributes 3 points.
A suppressed parent or a blank subtask covered by the parent fallback is not
an additional missing point estimate.

Active sprints show observed progress with its source timestamps and coverage;
a sync completion time alone cannot certify every issue's state at that instant.
They are not closed-sprint history for forecasting. A closed sprint missing its
actual start or completeDate has an incomplete review and is excluded from
automatic rate selection. Existing charts may retain their separately labelled
scheduled-end fallback.

### Original outcomes and scope changes

Give each original ticket one primary disposition, keeping main-ticket and
subtask counts separate using the classification saved in the baseline:

1. Removed from the sprint if known absent at closure.
2. Transferred out if retained in the sprint but no longer in the team's scope.
3. Dropped if retained by the team and dropped at closure.
4. Unfinished if retained by the team and not Done at closure.
5. Delivered if retained by the team with a qualifying completion in this sprint
   and Done but not dropped at closure.
6. Already completed if retained and Done but not dropped, with evidence that
   completion predates the sprint and no qualifying completion occurred in it.
7. Unknown when the evidence needed for those distinctions is missing.

A conversion keeps the ticket's original identity and disposition; annotate
the classification or parent change separately. Original outcome counts use
baseline classifications so they reconcile with the preserved original count.
Closing counts and point selection use closing classifications and relationships.
A conversion does not turn an original ticket into added work, and a later
conversion after closure cannot alter the recorded historical classification.

These dispositions reconcile to the confirmed original ticket count. Added work
includes tickets outside the original set that are proven absent from the
team-and-sprint scope at the baseline and enter or re-enter after it, before
the review cutoff, including ones subsequently removed or transferred. Count
each such ticket once. Membership at an open-sprint observation cutoff is
inclusive; closure is exclusive.

Tickets with unresolved baseline membership appear in the separate unclassified
original/added bucket. This includes tickets later proven present at the baseline
that the frozen original omitted. Mark the breakdown incomplete while retaining
the ticket in independently supported closing delivery. Reconcile closing
delivery as original, added and unclassified, without double counting. A gap in
this breakdown alone does not invalidate a complete closing point total or its
forecast rate. Show added delivery as a subset of added work; scope-change
columns overlap and are not summed into a total.

The original commitment survives a later transfer. A ticket transferred into
another team is evaluated using that team's own plan and closure membership.

Component mappings can overlap across teams. Show the overlap and count a
ticket once within each matching team, consistent with component filters.
Do not publish a sum across those teams as unique project delivery or silently
assign shared tickets to one team.

### Estimates

Keep the original snapshot's recorded estimate and its evidence. For proven
added tickets, reconstruct the estimate at their first entry after the baseline
cutoff and before the review cutoff, including joins before confirmation.
Unknown or conflicting baseline evidence remains explicit; a later observation
cannot become a historical estimate merely because its timestamp is known.

Delivered points use the estimate immediately before sprint closure. Show the
entry estimate, closing estimate and difference for each ticket with both
values known. Separate delivered-point changes from estimate changes; changing
an estimate is not itself a delivery event. Show changes in which ticket carries
the counted points separately from estimate changes, so moving point selection
from a parent to its subtasks is not labelled a re-estimate of the parent.

For example, a ticket selected as a point source with a confirmed estimate of
3 and a closing estimate of 5 contributes 3 to original commitment, 5 to
delivered points and +2 to estimate change. If it is later changed to 8, the
closing value remains 5. Point-source selection must avoid adding the parent's
estimate to its subtasks' estimates.

Resolve the configured point field using metadata and retain field IDs when
normalizing history. Reverse later changes from a known source value to
recover the closing value. Apply the same evidence discipline to component
membership, issue classification and parent relationships, and to retrospective
sprint-start reconstruction. A historical issue-type ID needs evidence of its
main-ticket or subtask classification; a name heuristic is insufficient.
Missing or conflicting history produces unknown, not the current value
presented as historical fact.

### Coverage and filtering

Use retained unfiltered issue history for the new historical team calculations.
URD's current report views filter by current components and would lose tickets
that moved later. Reuse established membership and status logic where its
cutoffs fit, without applying today's component filter first.

If the source mirror's component restriction or pruning prevents historical
coverage, disclose that limit and withhold a complete delivery total and
automatic rate. Reconstruct what is supported; do not fill missing work with
zero. User-confirmed snapshots survive pruning.

A plan's saved team scope controls its capacity comparison. General report
filters cannot silently change that denominator. For incompatible component or
epic filters, show a link to the full plan instead of a filtered capacity ratio.
Date filters may select sprint records; they do not truncate a selected sprint.

## Views and export

Add a Capacity entry to each configured project's local web interface:

- Team setup and normal working patterns.
- Sprint selection and availability editing.
- Planning summary and explicit confirmation.
- Review with ticket evidence, capacity revisions and optional grid detail.

Reuse native forms, existing table and chart rendering, and the existing local
server. Editing occurs in the served application. The exported HTML is a
self-contained read-only report.

Exports include the team, sprint, original and revised totals, forecast source,
delivery comparison, cutoffs and coverage. Include individual daily availability
only when explicitly selected. Without that option, do not embed person-level
grid data in hidden HTML or scripts.

Revision reasons can contain personal detail. Default exports show the aggregate
capacity changes and dates without free-text reasons; the local view retains
the full revision history. Grid inclusion is not consent to export unrelated
local data.

## Proposed implementation

### Persistent records

Use the existing project DuckDB file. Keep authoritative local inputs separate
from tables that derive drops and rebuilds.

| Record | Contents |
| --- | --- |
| Team | Stable ID, name, selected components, timezone, roster identities and normal weekly Work and Meeting hours. |
| Sprint catalogue | Jira sprint ID, board associations, state, dates and metadata fetch time, independent of issue memberships. |
| Plan | Team and sprint IDs, current revision and current valid confirmation; earlier voided confirmations remain linked in revision history. |
| Revision | Immutable version, timestamp, reason, grid, copied settings, rate provenance, and confirmation snapshot with baseline kind, cutoff, classifications, parent relationships, point sources and source evidence. |
| Review evidence | Derived historical membership, status, issue classifications, parent relationships, point values and uncertainty needed for the new comparison. |

Use a compact JSON payload for each small revision snapshot, with team, sprint,
version and timestamp as ordinary indexed columns. Its whole-record shape
matches atomic form saves and avoids a generic event system. Queries can expose
the report totals through SQL. A revision stores the inputs and source values,
not only rounded display strings.

A database uniqueness constraint enforces one plan record per team and sprint,
with at most one current valid confirmation. Voiding appends its reason and the
referenced confirmation to revision history and clears that current pointer;
a replacement confirmation points to a new immutable baseline. Prior baselines
remain addressable by their revision IDs. No additional active plan is created
for the same pair.

Saves, voids and replacements carry the version the form was opened on; stale
actions return a conflict with entered values preserved. Confirmation, revision
insertion and changes to the current pointer are atomic.

Serialize saves with the existing per-project write coordination and use
request cursors. During an incompatible refresh operation, preserve the form
and return a retryable message. Report readers continue to see a consistent
committed snapshot. Sync, derive and restart cannot reset planning inputs.

### Backup and restore

Document the data-volume location and a manual backup procedure: stop URD and
any CLI writers cleanly, verify that database connections are closed, and copy
the complete data volume, including database companion files, to a separate
backup location. Restore to a separate volume first and verify teams, grids,
confirmation history, voids and saved
forecast values before replacing a working copy. Keep the current files until
the restored copy has been verified. Re-syncing Jira is not a restore for locally
entered plans.

Exercise this procedure with synthetic plans, including revisions and a voided
confirmation, and verify that the restored plans survive derive and restart.
No scheduled backup service or new storage format is needed for this version.

### Jira catalogue discovery

Use read-only board and sprint discovery so empty future sprints are selectable.
Choose boards from a project-scoped catalogue and cache their paginated sprint
lists. Board permission failures and incomplete pagination are explicit errors;
neither is an empty successful catalogue.

Atlassian documents paginated board sprint discovery and shows future sprints
without dates in its response example. The individual sprint endpoint also
requires permission to see its originating board or an associated issue.
See the [board API](https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/#api-rest-agile-1-0-board-boardid-sprint-get)
and [sprint API](https://developer.atlassian.com/cloud/jira/software/rest/api-group-sprint/#api-rest-agile-1-0-sprint-sprintid-get).

Keep catalogue refresh in the explicit network path. Opening a cached plan,
saving availability, deriving history and rendering a report remain offline.
A failed catalogue refresh retains previously saved plans and labelled cached
metadata.

The current client prefixes paths with /rest/api/3. Add a narrowly scoped
Jira Software read path for /rest/agile/1.0, sharing the existing final-host
validation, GET-only transport, retries and redirect rejection. Neither form
input nor stored data may supply an arbitrary credential destination or API URL.

This extends the repository's API-path scope beyond platform REST v3 while
retaining Jira Cloud only. It does not introduce Jira writes.

### Code boundaries

- A capacity module owns local records, calculation validation and snapshots.
- A capacity blueprint owns the forms and routes using the existing Flask app.
- Jira client and sync code gain catalogue retrieval and durable metadata.
- Offline derivation gains historical point, component, classification and
  parent-relationship evidence for starting commitments and closing delivery.
- Chart specifications and the shared report renderer gain the capacity views.
- Repository scope documentation records the deliberate planning and API changes.

Use the installed standard library, DuckDB and Flask. No spreadsheet engine,
calendar library, new frontend framework or background scheduler is required.
Detailed schema and function names belong in the implementation plan.

## Validation against real Jira data

Before implementation, inspect a real closed sprint read-only using the local
mirror and, if necessary, permitted Jira reads. Check subtask estimates and their
own components and sprint memberships, sprint start and closure, completion
transitions, historical estimates, classifications and parent relationships.
Trace representative original and added work, a parent/subtask family, and
available conversion or reparenting evidence from source history to the expected
totals. If the selected sprint lacks such a case, record it as unvalidated or
inspect another relevant sprint; do not treat absence as proof of support.

Record which calculations the retained data supports and which remain unknown.
Resolve any gap that prevents the intended historical velocity calculation
before implementation. Keep real records and identifying evidence outside the
repository; create synthetic acceptance cases for the behaviours found. This
validation is a prerequisite, not a claim that it has already passed.

## Verification and acceptance

Use synthetic fixtures and the existing test runner. No workbook rows, real
names, internal hosts or captured Jira responses enter the repository.
Implementation must observe each meaningful regression fail for its intended
reason before relying on its passing result.

| Check | Required result |
| --- | --- |
| Team and sprint isolation | Two teams in one sprint and one team in two sprints retain independent grids and baselines. |
| Working patterns | New drafts copy both Work and Meetings; exceptions do not carry forward; later pattern edits leave saved plans unchanged. |
| Input completeness | Blank differs from zero in both inputs; invalid, non-finite and negative values or Work plus Meetings over 24 hours in a day are rejected; fractional hours are included. |
| Capacity arithmetic | Daily Work and Meetings sum to 48 available hours and 42 Work hours. Capacity is 31.5 focus hours, then 28.5 after a 4-hour Work reduction; no second meeting subtraction. |
| Weighted forecast | The two-history example produces 0.24 points per focus hour and a 12-point forecast. |
| History provenance | Corrections to a past sprint do not rewrite a previously saved forecast; zero-capacity and incomplete history are explained. |
| Original confirmation | Confirmed inputs and ticket estimates remain immutable; future, active and closed sprints can be confirmed; late and retrospective records are labelled correctly. |
| Retrospective plan | First confirmation after closure reconstructs sprint-start scope and estimates. A ticket in scope at start, completed during the sprint and delivered at closure remains in the reconstructed commitment after a later transfer out. A later join is added work. Missing baseline history does not block delivery proven independently; missing actual start prevents a complete total or rate but does not prevent capacity entry. |
| Cutoff before confirmation | Baseline cutoff 10:00, ticket joins at 11:00 with 3 points, confirmation 12:00, then Done at closure with 5 points: after refresh it is absent from original commitment and counted once as added delivery, with entry estimate 3, closing estimate 5 and change +2. Confirmation time never excludes this join or replaces the stored cutoff. |
| Change during sync | Ticket fetched outside the sprint at 10:00, joins at 10:01, sync finishes at 10:02, confirmation at 10:03: the next refresh preserves the original snapshot, exposes uncertain original/added attribution and includes known closing delivery once. Sync completion is not proof of source completeness; an independently complete closing point total remains eligible for a rate. |
| Revisions and conflicts | A reason is required after confirmation; stale tabs cannot overwrite later saves; failed writes leave the previous revision intact. |
| Voiding and replacement | A mistaken confirmation is voided with a reason, remains readable and is excluded from new rates. A corrected confirmation preserves and links the old baseline; a wrong-team replacement cannot overwrite a valid destination plan. Prior saved forecasts retain their values and indicate any voided source. Stale or failed void/replacement writes leave the prior state intact. |
| Future sprint discovery | A ticketless future sprint appears; missing dates require input; pagination and permission failures never masquerade as an empty result. |
| Date changes | Timezone boundaries and changed sprint dates preserve entered hours and require explicit reconciliation. |
| Delivery | A ticket completed during the sprint and retained Done at closure counts once; unfinished, dropped, removed and subtasks follow the specified separate outcomes. |
| Previously completed work | A 5-point ticket Done before sprint start and still Done at closure contributes no new delivery. If it reopens and completes during the sprint, it contributes 5 once. Completion before a late confirmation still qualifies when within the sprint. Missing completion history remains unknown. |
| Completion boundaries | Completion at sprint start is included; completion at closure is excluded. A change between Done-category statuses is not a new completion. Creation in Done during the sprint needs evidence of that initial state. |
| Team transfers | A later component change does not rewrite ownership at closure; transfers before closure reconcile with original commitments. |
| Estimate changes | For a selected point source, the 3-to-5-to-8 example retains commitment 3, delivery 5 and estimate change +2. |
| Parent and subtask points | A parent estimated at 8 and its subtasks estimated at 3 and 5, all completed during and retained in one team's sprint, contribute 8 points, never 16. Ticket counts remain separately visible. |
| Independent subtask delivery | An unfinished parent estimated at 8, a subtask at 3 completed during the sprint and an unfinished subtask at 5 contribute 3 delivered points. Parent completion is not a gate. |
| Parent fallback and coverage | A parent at 8 completed during the sprint with no estimated subtasks contributes 8. A subtask estimate of zero suppresses the parent. An estimated subtask outside the selected scope also suppresses it without moving points between scopes. Partial subtask estimates and unknown hierarchy produce explicit missing coverage rather than parent fallback. |
| Subtask attribution | A subtask in another team or sprint contributes only to its own matching scope with a qualifying completion; filtering out that subtask cannot restore its parent's points. |
| Conversions | A ticket converted before closure keeps its original cohort and baseline classification for reconciliation, while closing delivery uses its closing type and parent. Conversions and reparenting after closure do not rewrite the historical total. |
| Scope and uncertainty | Duplicate components and memberships do not duplicate tickets; missing history remains unknown; original outcomes reconcile. |
| Re-entry after baseline | A ticket joins before 10:00, leaves before the 10:00 baseline and rejoins at 11:00: proven absence at the baseline makes it added work, counted once, with the 11:00 entry estimate. A ticket present in the original baseline remains original on return. |
| Refresh and migration | Existing databases still open; refresh, derive, pruning and restart preserve plans; readers see coherent data during saves. |
| Backup and restore | Back up a closed synthetic database, restore it into a separate volume, and verify teams, grids, baselines, revisions, voids and forecast provenance before and after derive and restart. |
| Real-data validation | A read-only check of a real closed sprint records supported calculations and evidence gaps before implementation; real records stay outside the repository. |
| Export | Default HTML has team totals but no person-level availability or free-text revision reasons; opting in includes the grid. |
| Security | New routes preserve same-origin and Host checks; new API paths retain trusted-host, GET-only and no-redirect guarantees. |
| Existing behaviour | Existing charts and CLI reporting retain their documented metrics and filters. |

Run the repository's full test, lint and leak gate for implementation. For this
specification-only change, check document consistency, whitespace and leakage.

## First version boundaries

The first version includes the confirmed product decisions and the proposed
flows above. It does not import spreadsheet history, create local sprints
without Jira IDs, write to Jira, integrate an HR or calendar service, infer
actual worked time from Jira worklogs, schedule people across projects, or add
multi-user hosting.

No instance-specific hours, focus factor, rate, roster, timezone, component
mapping or historical selection is supplied as a hidden default.

After design review, create the implementation plan and identify its tracking
issue before coding. This specification does not authorize implementation,
committing, publishing or deployment.
