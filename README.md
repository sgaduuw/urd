# urd

urd mirrors one Jira project's ticket history into a local DuckDB database and
serves it as an HTML report. It is read only: every request to Jira
is a GET, and urd never writes anything back.

## Setup

Store the API token once, in the macOS keychain:

```
security add-generic-password -s urd -a <email> -w
```

(`URD_TOKEN` in the environment works too, and skips the keychain entirely. On
Linux and Windows it is the only option: the keychain is macOS-only.)

Set the trusted Jira hostname in the process environment before any sync or
setup request. This is independent of the form and stored database settings:

```
export URD_JIRA_HOST=example.atlassian.net
```

`URD_JIRA_HOST` must be a bare hostname, without a scheme, port, path or credentials.
Every live request requires it, including refreshes of existing databases.
The requested site must match it exactly (case-insensitive). To use another
Jira tenant, run a separate process with that tenant and its token. Offline
`derive` and `sql` commands do not require it.

First run needs the full scope. `--component` is optional and narrows what is
mirrored; leave it out to mirror the whole project and pick components on the
report instead:

```
uv run --isolated --with-requirements requirements.txt python urd.py sync \
  --site example.atlassian.net --email you@example.com \
  --project PROJ --component TEAM --since 2026-01-01
uv run --isolated --with-requirements requirements.txt python urd.py derive \
  --status-order "To Do,In Progress,Review,Done" \
  --start-status "In Progress" --review-status "Review" \
  --abandoned-status "Won't do"
uv run --isolated --with-requirements requirements.txt python urd.py serve
```

Every flag there is remembered in `sync_state`. From the second run on:

```
uv run --isolated --with-requirements requirements.txt python urd.py sync
uv run --isolated --with-requirements requirements.txt python urd.py derive
uv run --isolated --with-requirements requirements.txt python urd.py serve
```

Reports are read in `urd serve`. The controls apply filters to the page
you are looking at; **Save as default** stores them for every later visit.

## Serving it

`urd serve` renders the report over HTTP, with the report
filters as controls and a Refresh button that syncs in the background:

```
uv run --isolated --with-requirements requirements.txt python urd.py serve --volume ./urd-data
```

One DuckDB file per Jira project, all in the volume, each with its own workflow
configuration, component filter and report defaults. `/` redirects to the first
configured project; `/setup` adds another.

### In a container

```
URD_TOKEN=... docker compose up      # podman compose up works unchanged
```

`URD_TOKEN` is passed through from your environment and is never written to the
database, a file, or a log. `URD_JIRA_HOST` remains required as the credential
trust anchor on every run, including with an existing volume.
`URD_JIRA_HOST`, `URD_EMAIL`, `URD_PROJECT`,
`URD_COMPONENT`, `URD_SINCE`, `URD_STATUS_ORDER`, `URD_START_STATUS` and
`URD_REVIEW_STATUS` seed the *first* project so a fresh volume comes up already
synced and derived. A configured database keeps its stored scope; a changed
`URD_JIRA_HOST` blocks requests to that old site rather than rescoping the database.
Without the three
status keys the seeded project syncs, but derive refuses for want of
`--status-order` and lands on a page whose only action is Refresh.

`podman-compose config` prints `URD_TOKEN: null` where `docker-compose config`
prints the value. The token is not being dropped: a bare key is resolved from
your environment when the container starts, not when the file is rendered.

### Upgrading an existing container volume

The container now runs as UID/GID `10001:10001`. A new named volume gets the
correct owner automatically. For a volume created by an older root-run image,
stop the service and change its data ownership once before restarting:

```
docker compose stop urd
docker compose run --rm --no-deps --user 0 --entrypoint chown urd \
  -R 10001:10001 /var/lib/urd
docker compose up -d
```

Build the new image first with `docker compose build`. The ownership command
preserves the database contents. Bind-mounted directories need the same ownership.
Keep `URD_JIRA_HOST` set when restarting. The next sync refetches issues once because
child inventories are now requested for historical capacity coverage.

### It has no authentication

Anyone who can reach the port reads every ticket title and can trigger a sync. The
Dockerfile's own `CMD` binds `0.0.0.0`; compose's published port is what keeps that
off a network by default. Keep the published port bound to loopback.
The Host allowlist and Fetch Metadata checks protect against browser attacks;
a network client can forge both headers, so they do not restrict who can access
an exposed port. Add authentication before sharing access. `URD_JIRA_HOST` restricts
where the token can be sent, but does not restrict who can read reports or start
work against that tenant.

### The write lock

While `urd serve` is running it holds the write lock on every database in its
volume, so the CLI verbs cannot be used against them: DuckDB refuses a second
process even read-only. Stop the server first.

## The three verbs

`sync` fetches issues matching the persisted project, component and `since`
scope from Jira, and writes the raw JSON into `raw_issues`. It is the only
verb that touches the network, and the only one that writes `raw_issues`. A
ticket already stored is refetched if its `updated` timestamp moved, an outstanding
sync error needs retrying, or the requested field set changed. Errors clear after a
successful refetch; unchanged healthy tickets are skipped.

Sync writes timestamped, immediately flushed progress to stderr, including in
container logs. Lines identify the project (or database slug for a background
refresh), phase and elapsed time. Discovery reports every 100 tickets; fetching
reports every 50 attempts and at completion, including failures. Both also report
at the next completed item after 10 seconds, so small slow batches stay visible.
An in-flight request can still wait for the existing network timeout and retry.
Background refresh also logs derivation and completion or failure. Progress logs
omit response bodies; per-ticket error details remain in `sync_errors`.

`derive` rebuilds every relational table and view (`issues`, `changes`,
`issue_sprints`, and the metric views: `transitions`, `status_durations`,
`closures`, `cycle_times`, `rework`) from `raw_issues`. It is pure and
offline: no network call, and safe to rerun as often as you like. Changing a
metric's definition, or the workflow's status order, costs a `derive` run,
never a refetch.

The report reads the derived views and draws every chart as inline SVG on the
server.

## Widening the window

```
uv run --isolated --with-requirements requirements.txt python urd.py sync --since 2025-01-01
```

`keys_to_fetch` (see `urd.py`) applies one rule regardless of direction:
fetch a key not already stored, or whose `updated` has moved. Pushing
`--since` further back only fetches the newly included, older issues;
everything already held is left alone.

## The charts

Delivery totals count confirmed non-subtask work items. Subtask completions are
shown separately, so a completed parent and three completed subtasks contribute
one main delivery and three subtask completions. Jira's boolean issue-type
metadata supplies the classification, not the type name or the presence of a
parent in the report. Missing classification is shown as unknown and excluded
from confirmed totals. Attention tables still include all tickets.

Created, committed, open and dropped counts use the same non-subtask population
as their paired delivery counts. Story-point comparisons exclude subtask
points. Event charts count transitions into done, so reopening and completing a
ticket again remains another closure; sprint outcomes and current-state progress
count each ticket once. Neither measure proves that work shipped or was accepted.
Classification reflects the currently mirrored issue type, not a reconstructed
historical type. Converting a ticket to or from a subtask can change earlier totals.

After upgrading, run `derive` offline to recover classification from stored raw
issues. Historical totals will decrease where they previously combined parents
and subtasks. If raw metadata is missing, derivation preserves that uncertainty.

**Attention today**

- Aging work in progress: open tickets by days in their current status, with owner and ticket link.
- Open tickets by sprints carried: repeatedly carried tickets, worst first. Age and carry-over prompt a conversation; they do not establish why work has stalled.

**Flow over time**

- Created versus closed per week: arrivals, delivery and dropped work counted separately.
- New versus done, four week trend: smoothed counts and net weekly change.
- Cumulative flow: weekly tickets per status. A widening band is a queue.
- Open tickets over time: counted from status history.
- Cycle time: one point per closed ticket, with historical median and 85th percentile. These describe past delivery, not a promise about future work.

**Commitments**

- Active sprint scope: original commitment, added work, removed work and uncertain membership per active sprint, with main work, subtasks and unknown issue types counted separately.
- Active sprint tickets: linked tickets supporting those counts, with their origin and membership at the snapshot.
- Sprint scope changes: original work split into delivered, unfinished, removed and dropped tickets, alongside added work and its delivered subset.
- New, delivered and dropped per sprint: events attributed to the sprint running at the time.
- Delivered versus open, per version: one bar pair per tagged version.
- Progress per epic: delivered, dropped and open non-subtask children per parent.
- Epic scope and completions per week: recorded additions, removals, delivery and dropped work.
- Epic scope by week: linked epic breakdown of those events and net scope change.
- Epic history coverage: attributed events and missing or uncertain relationship evidence.
- Subtasks by parent: separate completion, dropped and open counts under actual direct parents.
- Issue classification coverage: counts of non-subtasks, subtasks and unknowns across all scoped tickets, independent of the report period.
- Carried into each sprint: tickets with earlier sprint memberships.

Epic scope charts count confirmed non-subtask events under parents whose stored
hierarchy metadata identifies them as epics. Moving a ticket from one epic to
another records a removal and an addition. Completion belongs to the epic recorded
at that event, not the current parent. Reclosures and repeated additions count
again. Delivery and dropped work do not remove scope; net scope is added minus
removed. These event totals complement the current distinct-ticket progress chart.

Modern and legacy parent changes are normalized without counting duplicate aliases
or parent-side child records twice. A simultaneous parent/status edit uses the
resulting parent. Missing ordering, broken history, unresolved parents and unknown
parent types remain unattributed. Current parent alone never establishes an initial
assignment or historical membership. The coverage table exposes these limits,
including parent relationships without change history and broken or unordered
histories even when no work has completed. Unknown issue types
and subtasks are excluded from confirmed counts; hierarchy and classification use
mirrored metadata rather than reconstructed historical issue types.

Event dates respect the since window; current relationship gaps remain visible regardless
of it. Component and epic exclusions apply, including historical epic keys after
a ticket has moved elsewhere. The weekly graph shows zero observed events in quiet
weeks, not a guarantee of complete history. The existing coverage threshold applies
to the graph and weekly table. No opening scope or backlog balance is inferred.
Run `derive` offline or Refresh after upgrading to build the new epic history.
Charts use the same dated snapshot rules as the active sprint tables below.

Active sprint tables describe the mirrored snapshot, with its source sync timestamp
shown beside each table. That cutoff is saved with the derived data, so a completed
sync cannot advance it until derive finishes. After upgrading, run `derive` offline
or use Refresh to establish the snapshot cutoff. Without it the tables show a notice.
If sync was interrupted after starting source changes, finish a sync before deriving.
This includes deleted tickets and metadata changes, even when no newer raw rows remain.
The same applies if raw rows exceed a legacy timestamp rounded to whole seconds.
The report does not query live Jira to establish whether a sprint is still active.

Original means in the sprint at its start, including changes exactly at the start.
Added counts each confirmed later addition once, including work created in the sprint.
Removed means absent at the snapshot, including changes exactly at its cutoff.
These counts overlap: an added ticket subsequently removed contributes to both.
An original ticket that leaves and returns stays original and is no longer removed;
a return whose initial membership is uncertain stays unknown instead of becoming
a confirmed addition. No ticket-detail rows are truncated. Component and epic
filters apply, while the report's since does not hide an ongoing sprint.

Parallel sprints remain separate by ID, even if their names match. Their state and
start date come from the freshest mirrored snapshots across the whole mirror.
Equally fresh snapshots must agree that the sprint is active and on its start date;
older planned dates do not override that evidence. A missing start or a start after
the snapshot prevents inclusion. Passing a scheduled end alone does not close a sprint.
Sprints with no remaining metadata in the mirror cannot be shown.

The closed-sprint scope-change chart uses non-subtask ticket counts, including unestimated work.
Original and added subtask completions are separate series. Main work and subtasks
have separate counts of uncertain membership or historical status; missing
classification is counted separately from both. Original
membership is reconstructed from recorded sprint changes. When the first
record already places an older ticket in the sprint, its start membership is
unknown; a later return does not count as added work. Conflicting sprint
snapshots use the earliest start and latest actual close time. Sprint state comes
from the most recently fetched snapshot across the whole mirror; equally fresh
snapshots that disagree do not establish closure. Only closed
sprints are shown, with outcomes measured just before actual closure. When no
close time is stored, the latest scheduled end is used and that sprint is labelled
`[scheduled end fallback]` before its name, so truncation keeps the warning visible. Sprints whose chosen cutoff is still in the future are
omitted. After upgrading, run `derive` to recover close times from stored raw
issues; this works offline. If the stored snapshots lack closure metadata, a
normal refresh may provide it when those issues are fetched again.
A ticket reopened after the cutoff retains its earlier outcome;
a ticket reopened before it is unfinished. A removed ticket re-added before the
cutoff is retained. Added work counts each ticket once, even if removed later;
its delivered subset includes only tickets retained and done at the cutoff.
For work created during the sprint, the first recorded Sprint change supplies
its prior membership, so later changes do not erase assignment at creation.
Dropped statuses use `--abandoned-status` and never count as delivery.
The `unknown` bar counts tickets with uncertain start membership or retained
tickets whose status category at the cutoff is missing. These never count as
confirmed delivered or unfinished work. Known additions with unknown outcomes
still count as added, so the unknown bar overlaps that scope count.

All counts describe the mirrored, filtered scope. Sprint dates come from the
whole mirror, so filtering out its remaining members does not hide removed work.
A sprint with no remaining membership anywhere in the mirror has no stored dates
and cannot appear. The report's period filter applies to sprint start dates.

**Retrospective**

- Median days in status, by issue type: where time goes.
- Ticket type mix per month: type does not establish whether work was planned or an interruption.
- Rework per sprint: backwards workflow transitions.
- Cycle time per sprint: median and 85th percentile days.
- Story points committed versus closed: the original estimate total beside closure points.
- Story points versus actual cycle time: whether estimates carry information.
- Tickets landing inside one sprint, by story point: delivery shares per estimate.

## Finding the status names

`derive` lists every status the fetched data mentions, with its category, how
many tickets sit there now, how many times work entered it, and the median days
from ticket creation to first arrival. It ends with a `--status-order` line to
paste and edit:

```
statuses found in the fetched data:
  category       status                         now  entered  median day
  new            Backlog                        206       21         0.0
  indeterminate  In Progress                     29      813        11.1
  done           Done                           600      655        15.6
```

The same listing replaces the first-run error, because otherwise `derive` asks
for an order over statuses it is the only thing able to enumerate.

A status marked `retired` appears in the history but not in the project's current
workflow, usually because it was removed or arrived with a ticket moved in from
elsewhere. Those are left out of the suggested `--status-order` while staying in
the listing, since they are still real history.

The suggested order is a heuristic worth editing rather than trusting. A parking
status such as Blocked or On Hold sits mid-flow but is not a workflow position at
all, so it sorts earlier than it belongs and is usually better left out entirely:
statuses left out of `--status-order` are excluded from rework detection, which is
the right home for parking states.

## Delivered, dropped, open

A ticket closed as "won't do" is a real outcome and is not delivery. Name the
done-category statuses that mean dropped, and they are counted apart from
delivered work everywhere at once:

```
uv run --isolated --with-requirements requirements.txt python urd.py derive --abandoned-status "Won't do,Duplicate"
```

Unset, nothing is treated as dropped: no status name is universal, so urd will
not guess one. A name that is not a done-category status is rejected.

## Interactivity

The report loads its JavaScript as files the server serves itself: uPlot and htmx
from `vendor/` and the first-party wiring in `static/urd.js`. Nothing is fetched from
another host.

Line, scatter, stack and combined charts gain hover readouts and drag-to-zoom; on
a stack, hovering reads the band's own value rather than the running total it sits
on. Charts whose categories are names are horizontal bars instead, drawn wider,
with every label and value written out, and are not upgraded. Tables sort by
any column, click or Enter on the header.

All of it is additive: every chart is rendered as SVG by Python and is present in
the page. A page opened with JavaScript disabled, or printed, loses hovering,
zooming and sorting, and nothing else. Nothing is computed in the browser that
Python could have computed, which is what keeps two renders of one database
identical.

## Ticket links

Ticket keys in the aging and per-epic tables link to `https://<site>/browse/<KEY>`,
built from the site recorded by `sync`, so a report against a different instance
links to that instance. With no site recorded yet, keys render as plain text
rather than as half a URL.

A link fetches nothing until a human clicks it, unlike `src`, `@import`, `url()`
or a stylesheet `href`, which the browser fetches on open with no choice.

## Leaving epics out

A trash-bin epic with a hundred abandoned children skews every total it appears
in, and nothing in the data distinguishes it from a real one:

Enter the keys in the exclude epic box, comma separated, and press Apply to see
the effect. **Save as default** stores the list; an empty box saved clears it. The
epic and every ticket parented to it disappear from every chart, and the header
names what was left out, because a report with an epic removed and one without
look identical and say different things about every total.

## Slicing by component

`sync --component` decides what is mirrored. The component tick boxes decide what
a page shows of it, without refetching anything. They sit above the report and
list the components the mirror holds, most tickets first. **Save as default**
stores the ticked boxes. No box ticked means every component, which is the default.

`(none)` is the slice of tickets that carry no component. It is offered only when
such tickets exist, since the list carries no counts and a box that can only
return an empty report is a trap.

A ticket in two components belongs to both slices. There is no primary component,
which would drop work from whichever slice lost it.

The header names the slice, and the title keeps naming the synced scope, so a
narrowed page is never mistaken for a mirror of that slice. To pick between
components on the page, leave `sync --component` empty and mirror the whole
project: the dropdown can only narrow what was fetched, so a mirror scoped to one
component offers the others as intersections with it.

## Reporting on a period

`sync --since` decides what is fetched. The since box decides what the charts
measure, without refetching anything. Enter a date such as `2026-03-01`, press
Apply, and use **Save as default** to keep it; enter `1900-01-01` to go back to
everything. The header
states the window, because a windowed report and a whole-history one look
identical otherwise.

A ticket counts if it was created or resolved inside the window; an event counts
if it happened inside it. That changes what some charts mean rather than just how
long they are: **progress per epic** and **per version** become what moved in the
window, not how far along the whole thing is.

One chart is exempt. **Aging work in progress** is always current, because a
window drops any ticket created before it, which is exactly the oldest work the
chart exists to find. Exemptions live in `WINDOW_EXEMPT` in `charts.py` and the
header reads them, so a report never claims a coverage it does not have.

## Attributing work to sprints

Most charts bucket by calendar week. One buckets by sprint, attributing each
ticket mutation to the sprint that was *running* when it happened rather than to
the ticket's own sprint membership. Nothing is guessed:

1. If the ticket belongs to exactly one sprint running at that moment, that one.
2. Otherwise, if exactly one sprint was running, that one.
3. Otherwise unattributed.

About two thirds attribute, so that chart carries a coverage figure counted in
mutations rather than tickets. Sprint lengths vary from three to twenty days on
real data, so its bars are sprint totals and not rates.

## Coverage figures

Some charts carry a `coverage` query alongside their main one: a numerator
and denominator, e.g. tickets with a cycle time over tickets resolved at all.
At or above the chart's threshold, the caption gains an "(N of M tickets)"
note. Below it, `run_chart` (`urd.py`) skips the chart entirely and renders
`coverage_strip` (`render.py`) instead: one sentence stating the shortfall.

A chart names a *tier* rather than a number. There are two, and both can be
set in the threshold box as `tier=share`, for example `points=0.35`, and then
saved as the default.

`default` covers most charts; `points` covers the two built on Story Points,
which is genuinely optional. A mistyped tier is an error rather than a silently
ignored value. The shipped values are the ones above: judgements about how little
data is still worth plotting, not properties of the data.

**Changing the shipped default in `charts.py` does not move a database that has
already saved thresholds**: the stored value wins, and the threshold has to be
saved once against that database. A database that has never saved one follows the
shipped default.

Apply, on the threshold, since and exclude-epic boxes alike, affects that one
request and stores nothing. The values are written inside the request's own
transaction, which is what keeps two browser tabs from fighting over each other's
view. Only **Save as default** stores them, and it stores all of them or none.

## Adding a chart

Append one `Chart` entry to `charts.py`: title, kind, SQL, caption, and
optionally a coverage query and a `tier`. Run the tests. Nothing in `urd.py`
or `render.py` needs to change, unless the chart names a `kind` no renderer
handles yet: the test suite asserts every chart's `kind` is one
`render.FIGURE_KINDS` covers, and that assertion fails first rather than
leaving a blank space in the report.

## Requirements

Python 3.11 or later. `_ts` in `urd.py` parses Jira's two timestamp shapes
(`...+0000` on issue fields, `...Z` on the Sprint field) with
`datetime.fromisoformat`, which only accepts both shapes natively from 3.11
onward. On 3.10 it raises `ValueError`.

## Checks and dependency updates

Run the same check used by pull requests (requires uv and ShellCheck):

```
sh tests/run.sh
```

It runs Python lint, shell lint, the privacy guard and every test script.
Missing required tests fail the check. Tests use the same dependency pins as the
container; `requirements.in` lists the direct dependencies and `requirements.txt`
locks their full dependency tree with hashes.

To update the lock, then validate it:

```
uv pip compile --universal --python-version 3.11 --generate-hashes \
  --upgrade --output-file requirements.txt requirements.in
sh tests/run.sh
```

For container changes, also run:

```
docker build -t urd-check .
sh tests/check-container.sh urd-check
```

Use `podman build` and `CONTAINER_ENGINE=podman sh tests/check-container.sh urd-check`
with Podman. The check uses a temporary volume to verify non-root execution and
persistence across container restarts.

Dependency update pull requests are checked weekly. No Jira credentials are
needed for the checks; network requests from tests are stubbed or refused.

## Privacy

`urd.duckdb` and any exported plan HTML contain real names, account ids and ticket
keys pulled straight from Jira. Both are gitignored. `tests/no-leaks.sh`
guards the repository itself: it scans every file that could be published,
every commit message and the commit author identity for anything
employer-specific, and must pass before every commit.

### Active and parked work

Attention today separates active / available work, deliberately parked work, and
unclassified work. Active means eligible for attention, not necessarily in progress.
Counts cover every open ticket in the report scope; each aging and carried-sprint
table shows up to 40 tickets within its own group. These views ignore the since box
so old open tickets remain visible. Component and excluded-epic filters still apply.

Configure parking explicitly with the existing workflow settings:

```sh
uv run --isolated --with-requirements requirements.txt python urd.py derive --parked-status "Deferred"
# Confirm that this workflow has no deliberately parked statuses, or clear a previous list:
uv run --isolated --with-requirements requirements.txt python urd.py derive --parked-status ""
```

Use a comma-separated list of open statuses in `--status-order`. The setting is
remembered, including an explicitly empty list. Setup offers the same list and a
“No parked statuses” checkbox. Leaving both blank keeps parking unconfirmed.
Existing databases start unconfirmed: their open work appears under Unclassified
until configured. No resync is needed. Unknown statuses or status categories also
remain unclassified. Age, workflow position and names such as “Blocked” never imply
parking; only the configured list does.

Setup verifies nonempty parking lists against the project's workflow before saving.
If discovery is unavailable, retry or leave parking unconfirmed. Offline derive checks
current status categories in mirrored tickets, not instance-wide status names; it
cannot validate categories for statuses absent from the mirror. A selected name with
any scoped done-category evidence is rejected, including conflicting open/done names.

## Licence

MIT. See `LICENSE`.

## Sprint capacity

Open **Plan capacity** on a configured project's page. Discover Scrum boards, add a
team with component mappings and a timezone, and enter each person's normal
seven-day Work and Meeting hours. Choose the team's boards and refresh their sprint catalogue.
Empty future sprints are supported; undated sprints need local planning dates.

Each team and sprint has its own grid. Enter Work and Meetings in each day's
cell. Work excludes meetings; total availability is Work plus Meetings. The
sprint focus percentage applies only to Work, with no second meeting deduction.
Blank means missing; enter zero explicitly where appropriate. Each day's sum
must be at most 24 hours. These are planned hours, not measured time worked.

Normal weekly patterns use the same daily split and seed new sprints. Changes
to a weekly pattern do not rewrite saved sprint plans.

Preview and explicitly confirm the plan. Confirmation is allowed before,
during or after a sprint. A first confirmation after closure reconstructs the
commitment at sprint start and labels the capacity retrospective. Subsequent
adjustments require a reason and preserve the original. A mistaken confirmation
can be voided with a reason, then replaced under the same or another team and
sprint. An existing valid destination cannot be overwritten.

Select previous team sprints for a capacity-weighted points-per-focus-hour
rate, or enter a manual rate. Selected source values and revision numbers are
frozen in the forecast. Later corrections and voids are visible without
rewriting earlier forecasts. Missing or incomplete history never becomes a
zero rate.

Delivery uses actual sprint start inclusive and closure exclusive. A ticket
must complete during that interval and remain Done, not dropped, in its own
team and sprint scope. Subtasks count independently of their parent's status.
Any estimated direct subtask, including an explicit zero or a subtask outside
the selected scope, suppresses its parent's points. Parent fallback requires
evidence that no direct subtask has an estimate. Missing historical facts are
shown as partial coverage.

Upgrade with a normal sync and derive before relying on historical coverage.
Sync now requests child inventories and retains type, status and observation
evidence. Older data can have gaps that a current metadata lookup cannot repair.
A source mirror restricted by component, too narrow a date window, missing
records or conflicting history can prevent a complete automatic rate.

Catalogue refresh is an explicit read-only network operation using Jira
Software's fixed board and sprint paths. Grid editing, saved plans, derivation
and reports work offline. A stale tab or busy refresh retains entered values
and asks for review or retry. Changed Jira dates require explicit reconciliation;
hours outside the revised range remain available for inspection.

The report shows one section at a time as tabs. Aggregate capacity lives in its
Capacity tab. A plan's **Export HTML**
action includes individual availability only when its checkbox is selected.
Free-text revision reasons stay in the local application. Exported files remain
read only. Spreadsheet import and Jira writes are not supported.

### Back up and restore local plans

Jira sync cannot recover locally entered availability, confirmations or forecasts.
They live in the same project DuckDB files as the mirror.

1. Locate the complete data volume: the directory supplied to `serve --volume`
   or `URD_VOLUME`; inside the container it is `/var/lib/urd`.
2. Stop URD cleanly and stop any CLI writers. For Compose, use
   `docker compose stop urd`. Verify all database connections are closed;
   on a host directory, `lsof +D ./urd-data` should list no open files.
3. Copy the **entire directory**, including database companion files, into a
   separate, new backup directory. Do not copy a live database file in isolation.
   For a stopped container, copy all of `/var/lib/urd/.`, not the image layer.
4. Restore the backup into another new directory or volume first. Open that
   restored volume with URD and verify teams, grids, original confirmations,
   revisions, voids, replacement links and saved forecast source values.
   Re-run derive and restart URD, then verify those records again.
5. Stop the restored instance. Only after verification, replace the working
   volume while retaining the previous volume separately. Preserve the
   ownership required by the runtime user, documented above.

For a host directory, these commands refuse an existing destination:

```sh
test ! -e ./urd-backup && cp -Rp ./urd-data ./urd-backup
test ! -e ./urd-restore-check && cp -Rp ./urd-backup ./urd-restore-check
uv run --isolated --with-requirements requirements.txt python urd.py serve --volume ./urd-restore-check
```

`test_capacity_report.py` exercises closed-database copying, separate restore,
derive and restart using synthetic confirmations, revisions, voids and forecasts.
