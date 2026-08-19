# urd

urd mirrors one Jira project's ticket history into a local DuckDB database and
renders it into a static HTML report. It is read only: every request to Jira
is a GET, and urd never writes anything back.

## Setup

Store the API token once, in the macOS keychain:

```
security add-generic-password -s urd -a <email> -w
```

(`URD_TOKEN` in the environment works too, and skips the keychain entirely.)

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
configured project; `/setup` adds another. Three projects mean three workflows, and
`status_order` is a single table per database, which is why they are separate files
rather than one.

Nothing about the CLI changes. `urd report` still writes the self-contained file,
and the server renders the same report with a controls form prepended to it.

### In a container

```
URD_TOKEN=... docker compose up
```

`URD_TOKEN` is passed through from your environment and is never written to the
database, a file, or a log. `URD_SITE`, `URD_EMAIL`, `URD_PROJECT`,
`URD_COMPONENT`, `URD_SINCE`, `URD_STATUS_ORDER`, `URD_START_STATUS` and
`URD_REVIEW_STATUS` seed the *first* project so a fresh volume comes up already
synced and derived; a database that is already configured wins over them, so
restarting with a stale compose file cannot rescope your data. Without the three
status keys, the seeded project can sync but derive refuses for want of
`--status-order`, landing on a page whose only action is Refresh, which repeats the
same failure.

### It has no authentication

Anyone who can reach the port reads every ticket title and can trigger a sync. The
Dockerfile's own `CMD` binds `0.0.0.0`; only compose's published port keeps that off
a network by default, so putting it there is one deliberate edit (`-p 8731:8731`
instead of `-p 127.0.0.1:8731:8731`), not two. Do not make that edit on a shared
host until authentication exists.

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
SVG, no JavaScript, no external references. Open it directly in a browser.

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
- Aging work in progress: open tickets by days in their current status, the chart that changes what you do today, with each ticket's title beside its key.
- Created versus closed per week: where created and delivered diverge, the
  backlog is growing. Dropped work is a third line, counted separately.
- New versus done, four week trend: the same counts smoothed, with net weekly
  change as bars on the same axis. Above zero the backlog grew that week.

- Open tickets over time: what the gap between the two adds up to, counted from
  the status history rather than created minus closed.
- New, delivered and dropped per sprint: every mutation attributed to the sprint
  that was running when it happened, rather than to a calendar week.
- Cumulative flow: tickets per status, sampled once a week. A widening band is a queue.
- Cycle time: one point per closed ticket. The 85th percentile is the number you can promise, the median is the one you'll be asked for.
- Median days in status, by issue type: where the weeks actually go, review queues show up here first.

**Reporting outward**
- Delivered versus open, per version: one bar pair per version a ticket is tagged with.
- Progress per epic: tickets done and still open, per parent. Parents outside the scope of this report appear by key alone.
- Ticket type mix per month: how much of each month was planned work, a growing bug or incident band is the interesting case.

**Retro**
- Rework per sprint: transitions that moved a ticket backwards through the workflow, the single best retro chart and one no built-in report draws.
- Carried into each sprint: tickets already in an earlier sprint. Persistent carry-over means the sprint is being planned optimistically.
- Cycle time per sprint: median and 85th percentile days per sprint. Tightening is the thing to look for, not the absolute value.
- Story points versus actual cycle time: whether the estimates carry information. A flat cloud means the points are ritual.

**People**
- Tickets closed per week, per person: one small chart each, deliberately, so the same data doesn't invite a reading a ranked bar chart doesn't support. Attributed to the assignee at close.
- Review load: who moves work out of the review status, counting a rejection back to in-progress the same as an approval. No built-in report exposes this.
- Handoffs: who starts work that someone else finishes. Read down the rows for what a person hands on, across for what they pick up.
- Story points closed per person: only meaningful if the field is filled consistently, which the coverage figure tells you.

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

The "in workflow" column comes from the project's own status list, fetched by
`sync`. A status marked `retired` appears in the history but not in the project's
current workflow, usually because it was removed or arrived with a ticket moved
in from elsewhere. Those are left out of the suggested `--status-order` while
staying in the listing, since they are still real history. If that call is
unavailable, every status is treated as current and the listing behaves as it
did before.

The order is by status category, then by the median days from ticket creation to
each ticket's *first* arrival at that status. First arrival, not every arrival:
a status re-entered after rework is reached late, and counting every visit sorts
it before the status that feeds it.

It is still a heuristic worth editing rather than trusting. A parking status such
as Blocked or On Hold sits mid-flow but is not a workflow position at all, so it
sorts earlier than it belongs and is usually better left out entirely. Statuses left out of `--status-order` are excluded from
rework detection entirely, which is the right home for parking states.

## Delivered, dropped, open

A ticket closed as "won't do" is a real outcome and is not delivery. Name the
done-category statuses that mean dropped, and they are counted apart from
delivered work everywhere at once:

```
uv run --with duckdb python urd.py derive --abandoned-status "Won't do,Duplicate"
```

Unset, nothing is treated as dropped, which is the previous behaviour: no
status name is universal, so urd will not guess one. A name that is not a
done-category status is rejected, because silently removing work from the
delivered line while leaving it open elsewhere is worse than a typo.

The flag drives `closures.abandoned` (per closure event) and `issues.abandoned`
(current state). Both exist because a ticket can close more than once, and a
current-state field cannot say which of those events was which.

## Interactivity

`report.html` carries its JavaScript inline: uPlot 1.6.31 from `vendor/`, plus
about 90 lines of first-party wiring. Nothing is fetched, so a saved report opens
offline, unchanged, years later, and no third party learns who reads a report
about internal work.

Every chart is upgraded in the browser: lines, scatters, stacks, bars and small
multiples all gain hover readouts and drag-to-zoom. On a stack, hovering reads the
band's own value rather than the running total it sits on. On vertical bars the axis is
categorical, so the names travel with the data and hover gives the whole name
rather than the three characters the axis has room for.

Charts whose categories are names use horizontal bars instead, and are not
upgraded. They are also drawn wider than the rest, 720px against 480: the label
gutter and the bars compete for the same width, and on the epic chart the labels
alone wanted 612px of a 480px frame. A name gets a whole line there rather than the width of a bar, so every
one is written in full and every value is printed at the end of its bar: there is
nothing left for zoom to reveal. Hovering still names the bar, through the SVG's
own tooltip and no script at all. These charts grow taller as rows are added
rather than thinning, on the principle that a page scrolls and a one-pixel bar
means nothing. Tickets closed per person is one chart with one line each: click a name in the
legend to isolate it, drag to zoom. `--min-closed N` sets how many closures inside
the window a person needs before they get a line, defaulting to three: fewer than
that draws a flat line at zero with one blip and spends one of eight palette slots
on it. With more people than the palette has
colours, three share each, so the legend and the readout are what tell two lines
apart rather than the colour.

There is a second per-person chart for individual trends: one panel each, four
week rolling average, and every panel scaled to its own data so a quiet person's
shape is visible instead of flattened against a busy one's. Two panels there
cannot be compared by eye, which is the trade, and the merged chart above is where
the comparison lives. Each heading carries the person's real closure count rather
than a sum of the averages being plotted.

A stack with more bands than the palette has colours folds its smallest into one
`Other`, because two identically coloured bands touching each other cannot be
told apart. The largest keep their identity, nothing is dropped, and every column
still totals what it did.

Tables with a `sortable` option sort by any column, click or Enter on the header,
numerically when the column is numeric.

All of it is additive. Every chart is rendered as SVG by Python and is present in
the file; the interactive version replaces that SVG at runtime, never in the
markup. A page opened with JavaScript disabled, or printed, loses hovering,
zooming and sorting, and nothing else. Nothing is computed in the browser that Python
could have computed, which is what keeps two reports of one database diffable:
every value a reader can hover is already in the markup.

Progress per epic is the chart that prompted it. At 141 epics, grouped bars came
to 423 marks in 480px, about one pixel each.

All four tables sort: aging work in progress (40 rows), median days in status
(44), handoffs (66) and progress per epic (141). Every one is past the point of
scanning by eye. A table added later must opt in deliberately, which a test
enforces: a genuinely short one may decline, but it cannot forget.

## Ticket links

Ticket keys in the aging and per-epic tables link to `https://<site>/browse/<KEY>`,
built from the site recorded by `sync`, so a report against a different instance
links to that instance. With no site recorded yet, keys render as plain text
rather than as half a URL.

This does not weaken the self-contained guarantee. The page still renders offline
and identically; a link is fetched only when a human clicks it, unlike `src`,
`@import`, `url()` or a stylesheet `href`, which the browser fetches on open with
no choice. The test that enforces this strips anchors and then applies every
pattern to what remains, and a companion test checks that the strip has not
blinded it.

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

The filter lives in the `issues`, `changes` and `issue_sprints` views rather than
in each query, so a chart added later inherits it and no chart can forget it. The
base tables keep an `_all` suffix and nothing outside `derive` touches them.
Filtering `issues` alone would be worse than not filtering: closures and durations
come from `changes`, so an excluded ticket would vanish from every ticket-based
chart while still driving every event-based one.

A database built before this existed has those three as tables, and `derive`
converts them on the next run.

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
chart exists to find: a six-month window hid every open ticket over 580 days old.
Exemptions live in `WINDOW_EXEMPT` in `charts.py` and the header reads them, so a
report never claims a coverage it does not have. A test checks the two agree in
both directions, so a chart cannot quietly stop obeying the flag, and an
exemption cannot outlive the chart it names.

## Attributing work to sprints

Most charts bucket by calendar week. One buckets by sprint, attributing each
ticket mutation to the sprint that was *running* when it happened rather than to
the ticket's own sprint membership. Two steps, and nothing is guessed:

1. If the ticket belongs to exactly one sprint running at that moment, that one.
2. Otherwise, if exactly one sprint was running, that one.
3. Otherwise unattributed.

Step 1 exists because parallel boards run sprints over the same fortnight: on one
real project it settles 56% of the otherwise ambiguous cases. Step 3 exists
because two sprints running with the ticket in neither has no answer, and picking
one would put work in a sprint it had nothing to do with.

About two thirds attribute, so that chart carries a coverage figure counted in
mutations rather than tickets. Sprint lengths vary from three to twenty days on
real data, so its bars are sprint totals and not rates; dividing by length would
invent a precision the boundaries do not have.

## Coverage figures

Some charts carry a `coverage` query alongside their main one: a numerator
and denominator, e.g. tickets with a cycle time over tickets resolved at all.
At or above the chart's threshold, the caption gains an "(N of M tickets)"
note. Below it, `run_chart` (`urd.py`) skips the chart entirely and renders
`coverage_strip` (`render.py`) instead: one sentence stating the shortfall.
That is the difference between a chart resting on data most tickets don't
carry and one that is honestly absent.

A chart names a *tier* rather than a number. There are two, and both can be
set per run and are then remembered:

```
uv run --with duckdb python urd.py report --threshold default=0.5 --threshold points=0.35
```

`default` covers most charts; `points` covers the two built on Story Points,
which is genuinely optional. A mistyped tier is an error rather than a
silently ignored flag.

These are judgements about how little data is still worth plotting, not
properties of the data, and the shipped values were set just under what one
real project measures. Lower them against a measurement, such as a chart that
draws something misleading, rather than to make one more chart appear: below
roughly a third the strip stops being a judgement and the caption's coverage
figure is doing all the work.

## Adding a chart

Append one `Chart` entry to `charts.py`: title, kind, SQL, caption, and
optionally a coverage query and a `tier`. Run the tests. Done. Nothing in
`urd.py` or `render.py` needs to change, unless the chart names a `kind` no
renderer handles yet, which the test suite fails on rather than leaving as a
blank space in the report.

A `kind` no renderer in `render.py` handles does not render as a blank space
in the browser: the test suite asserts every chart's `kind` is one
`render.FIGURE_KINDS` covers, and that assertion fails first.

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
