# urd

urd mirrors one Jira project's ticket history into a local DuckDB database and
renders it into a static HTML report. It is read only: every request to Jira
is a GET, and urd never writes anything back.

## Setup

Store the API token once, in the macOS keychain:

```
security add-generic-password -s urd -a <email> -w
```

(`URD_TOKEN` in the environment works too, and skips the keychain entirely. On
Linux and Windows it is the only option: the keychain is macOS-only.)

First run needs the full scope:

```
uv run --with duckdb python urd.py sync \
  --site example.atlassian.net --email you@example.com \
  --project PROJ --component TEAM --since 2026-01-01
uv run --with duckdb python urd.py derive \
  --status-order "To Do,In Progress,Review,Done" \
  --start-status "In Progress" --review-status "Review" \
  --abandoned-status "Won't do"
uv run --with duckdb python urd.py report
```

Every flag there is remembered in `sync_state`. From the second run on:

```
uv run --with duckdb python urd.py sync
uv run --with duckdb python urd.py derive
uv run --with duckdb python urd.py report
open report.html
```

## Serving it

`urd serve` renders the report over HTTP instead of writing a file, with the report
flags as controls and a Refresh button that syncs in the background:

```
uv run --with duckdb --with flask python urd.py serve --volume ./urd-data
```

One DuckDB file per Jira project, all in the volume, each with its own workflow
configuration, component filter and report flags. `/` redirects to the first
configured project; `/setup` adds another.

Nothing about the CLI changes. `urd report` still writes the self-contained file,
and the server renders the same report with a controls form prepended to it.

### In a container

```
URD_TOKEN=... docker compose up      # podman compose up works unchanged
```

`URD_TOKEN` is passed through from your environment and is never written to the
database, a file, or a log. `URD_SITE`, `URD_EMAIL`, `URD_PROJECT`,
`URD_COMPONENT`, `URD_SINCE`, `URD_STATUS_ORDER`, `URD_START_STATUS` and
`URD_REVIEW_STATUS` seed the *first* project so a fresh volume comes up already
synced and derived; a database that is already configured wins over them, so
restarting with a stale compose file cannot rescope your data. Without the three
status keys the seeded project syncs, but derive refuses for want of
`--status-order` and lands on a page whose only action is Refresh.

`podman-compose config` prints `URD_TOKEN: null` where `docker-compose config`
prints the value. The token is not being dropped: a bare key is resolved from
your environment when the container starts, not when the file is rendered.

### It has no authentication

Anyone who can reach the port reads every ticket title and can trigger a sync. The
Dockerfile's own `CMD` binds `0.0.0.0`; compose's published port is what keeps that
off a network by default. The app also refuses any request whose `Host` header is
not `127.0.0.1`, `localhost` or `[::1]` (`webapp.py`'s `_same_origin_only`), so
widening the published port alone (`-p 8731:8731` instead of
`-p 127.0.0.1:8731:8731`) is not enough: every request still 403s until that
allowlist is widened too. Do not make either edit on a shared host until
authentication exists.

### The write lock

While `urd serve` is running it holds the write lock on every database in its
volume, so the CLI verbs cannot be used against them: DuckDB refuses a second
process even read-only. Stop the server first.

## The three verbs

`sync` fetches issues matching the persisted project, component and `since`
scope from Jira, and writes the raw JSON into `raw_issues`. It is the only
verb that touches the network, and the only one that writes `raw_issues`. A
ticket already stored is refetched only if its `updated` timestamp moved.

`derive` rebuilds every relational table and view (`issues`, `changes`,
`issue_sprints`, and the metric views: `transitions`, `status_durations`,
`closures`, `cycle_times`, `rework`) from `raw_issues`. It is pure and
offline: no network call, and safe to rerun as often as you like. Changing a
metric's definition, or the workflow's status order, costs a `derive` run,
never a refetch.

`report` reads the derived views and writes `report.html`: one file, inline
SVG, no external references. Open it directly in a browser.

## Widening the window

```
uv run --with duckdb python urd.py sync --since 2025-01-01
```

`keys_to_fetch` (see `urd.py`) applies one rule regardless of direction:
fetch a key not already stored, or whose `updated` has moved. Pushing
`--since` further back only fetches the newly included, older issues;
everything already held is left alone.

## The charts

**Flow health**
- Aging work in progress: open tickets by days in their current status, with each ticket's title beside its key.
- Created versus closed per week: where created and delivered diverge, the
  backlog is growing. Dropped work is a third line, counted separately.
- New versus done, four week trend: the same counts smoothed, with net weekly
  change as bars on the same axis. Above zero the backlog grew that week.
- Open tickets over time: counted from the status history rather than created
  minus closed.
- New, delivered and dropped per sprint: every mutation attributed to the sprint
  that was running when it happened, rather than to a calendar week.
- Cumulative flow: tickets per status, sampled once a week. A widening band is a queue.
- Cycle time: one point per closed ticket. The 85th percentile is the number you can promise, the median is the one you'll be asked for.
- Median days in status, by issue type: where the weeks actually go, review queues show up here first.

**Retro**
- Rework per sprint: transitions that moved a ticket backwards through the workflow.
- Carried into each sprint: tickets already in an earlier sprint. Persistent carry-over means the sprint is being planned optimistically.
- Open tickets by sprints carried: which tickets those are, worst first, and since when. It does not know why: work parked by agreement looks the same as work quietly rolling.
- Cycle time per sprint: median and 85th percentile days per sprint. Tightening is the thing to look for, not the absolute value.
- Story points versus actual cycle time: whether the estimates carry information. A flat cloud means the points are ritual.
- Story points closed per sprint: credited to the sprint that was running at close. Sprint lengths differ, so these are totals and not a velocity to plan against.

**Reporting outward**
- Delivered versus open, per version: one bar pair per version a ticket is tagged with.
- Progress per epic: tickets done and still open, per parent. Parents outside the scope of this report appear by key alone.
- Ticket type mix per month: how much of each month was planned work, a growing bug or incident band is the interesting case.

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
uv run --with duckdb python urd.py derive --abandoned-status "Won't do,Duplicate"
```

Unset, nothing is treated as dropped: no status name is universal, so urd will
not guess one. A name that is not a done-category status is rejected.

## Interactivity

`report.html` carries its JavaScript inline: uPlot 1.6.31 from `vendor/`, plus
about 90 lines of first-party wiring. Nothing is fetched, so a saved report opens
offline, unchanged, years later.

Line, scatter, stack and combined charts gain hover readouts and drag-to-zoom; on
a stack, hovering reads the band's own value rather than the running total it sits
on. Charts whose categories are names are horizontal bars instead, drawn wider,
with every label and value written out, and are not upgraded. Three tables sort by
any column, click or Enter on the header.

All of it is additive: every chart is rendered as SVG by Python and is present in
the file. A page opened with JavaScript disabled, or printed, loses hovering,
zooming and sorting, and nothing else. Nothing is computed in the browser that
Python could have computed, which is what keeps two reports of one database
diffable.

## Ticket links

Ticket keys in the aging and per-epic tables link to `https://<site>/browse/<KEY>`,
built from the site recorded by `sync`, so a report against a different instance
links to that instance. With no site recorded yet, keys render as plain text
rather than as half a URL.

This does not weaken the self-contained guarantee: a link is fetched only when a
human clicks it, unlike `src`, `@import`, `url()` or a stylesheet `href`, which the
browser fetches on open with no choice.

## Leaving epics out

A trash-bin epic with a hundred abandoned children skews every total it appears
in, and nothing in the data distinguishes it from a real one:

```
uv run --with duckdb python urd.py report --exclude-epic PROJ-1 --exclude-epic PROJ-2
```

Repeatable, remembered between runs, and `--exclude-epic ""` clears the list. The
epic and every ticket parented to it disappear from every chart, and the header
names what was left out, because a report with an epic removed and one without
look identical and say different things about every total.

## Reporting on a period

`sync --since` decides what is fetched. `report --since` decides what the charts
measure, without refetching anything:

```
uv run --with duckdb python urd.py report --since 2026-03-01
```

Remembered between runs; pass `1900-01-01` to go back to everything. The header
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
set per run and are then remembered:

```
uv run --with duckdb python urd.py report --threshold default=0.40 --threshold points=0.35
```

`default` covers most charts; `points` covers the two built on Story Points,
which is genuinely optional. A mistyped tier is an error rather than a silently
ignored flag. The shipped values are the ones above: judgements about how little
data is still worth plotting, not properties of the data.

A `report` run on the command line remembers the thresholds it resolved, so
**changing the shipped default in `charts.py` does not move a database that has
already run `report`**: the stored value wins, and `--threshold` has to be passed
once against that database. A database that has only ever been served has nothing
stored and follows the shipped default.

The threshold box on the served page, like the since and exclude-epic boxes
beside it, applies to that one request and stores nothing. All three write into
the request's own transaction, which is what keeps two browser tabs from
fighting over each other's view.

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

## Privacy

`urd.duckdb` and `report.html` contain real names, account ids and ticket
keys pulled straight from Jira. Both are gitignored. `tests/no-leaks.sh`
guards the repository itself: it scans every file that could be published,
every commit message and the commit author identity for anything
employer-specific, and must pass before every commit.

## Licence

MIT. See `LICENSE`.
