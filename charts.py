"""Chart specifications. Adding a chart means writing SQL, nothing else.

Each spec is data: a query, a renderer kind, and the column names that renderer
needs. `coverage` is SQL returning one row of (numerator, denominator); when the
share falls below the chart's THRESHOLDS tier it is replaced by a strip saying so,
which
is the difference between a chart that is missing and one that is quietly wrong.
"""
from typing import NamedTuple

SECTIONS = ("Flow health", "Reporting outward", "Retro", "People")

# Optional-field charts are held to a lower bar than always-available ones: a
# field most tickets skip is still worth showing with a caveat, where an
# always-available one going quiet means something is wrong.
#
# 0.35 rather than the original 0.5 because of what the first live run measured:
# 195 of 517 closed tickets carry a real estimate, so at 0.5 the points charts
# hid themselves on the one project they were written for. Set deliberately just
# under that, which is the honest position: the number is a judgement about how
# little data is still worth plotting, not a property of the data. The caption
# carries the coverage figure either way, so a reader always sees what the chart
# is drawn from.
POINTS_TIER = 0.35
# 0.5 rather than the original 0.7, on the same evidence and for the same reason
# as POINTS_THRESHOLD. The first live run put the three affected charts at 60%,
# 64% and 52%, so 0.7 hid all three on data that was perfectly worth reading, and
# it had been chosen before any real data existed.
#
# This is close to the floor of what the mechanism is still worth having. Below
# about a third, a strip stops being a judgement and becomes a way of never
# saying no, at which point the caption's coverage figure is doing all the work
# and the threshold may as well go. Move it again only against a measurement,
# never to make one more chart appear.
DEFAULT_TIER = 0.5

# A chart names a TIER rather than carrying a number, so both knobs can be
# retuned from the command line without editing code. Both values above were
# moved twice in one sitting by editing source, which is what prompted this.
#   urd report --threshold points=0.4 --threshold default=0.6
# Overrides are remembered in sync_state, the same way the sync scope is.
# ponytail: two tiers, no per-chart override. Upgrade path when one chart
# genuinely needs its own number is to accept a chart key here as a third kind
# of name; nothing else has to change.
THRESHOLDS = {"default": DEFAULT_TIER, "points": POINTS_TIER}

# How far back the time series charts look. Long enough to show a trend, short
# enough that the x-axis stays readable and the report stops growing.
WINDOW_WEEKS = 26

# Charts that deliberately ignore `report --since`, and why. A dict rather than a
# comment because the report header reads it: a page claiming every chart covers
# a window while one does not is worse than no claim at all.
WINDOW_EXEMPT = {
    "aging_wip": "always current, so the window would hide the oldest work it exists to find",
}


class Chart(NamedTuple):
    key: str
    section: str
    title: str
    kind: str
    sql: str
    caption: str
    # Shared across instances, which is safe only because nothing mutates it.
    # A NamedTuple field cannot be rebound, but the dict itself is still shared;
    # treat it as read-only.
    options: dict = {}
    coverage: str | None = None
    tier: str = "default"   # a key of THRESHOLDS, not a number


CHARTS = [
    Chart(
        key="aging_wip",
        section="Flow health",
        title="Aging work in progress",
        kind="table",
        caption="Open tickets by days in their current status. The chart that "
                "changes what you do today. Ignores --since: a window drops any "
                "ticket created before it, which is exactly the oldest work here.",
        options={"headers": ["key", "summary", "status", "assignee", "days"],
                 "shade": "days", "sortable": True, "links": ["key"]},
        sql="""
            SELECT i.key,
                   -- Truncated in SQL rather than styled in CSS: the table
                   -- renderer has no per-cell tooltip to hold the rest, and the
                   -- key beside it is a link to the whole ticket.
                   CASE WHEN length(i.summary) > 60
                        THEN left(i.summary, 59) || '…' ELSE i.summary END AS summary,
                   i.status,
                   COALESCE(p.display_name, 'Unassigned') AS assignee,
                   date_diff('day', d.entered, now()) AS days
            FROM issues i
            LEFT JOIN people p ON p.account_id = i.assignee_id
            JOIN (SELECT key, max(entered) AS entered FROM status_durations GROUP BY key) d
                 ON d.key = i.key
            WHERE i.status_category <> 'done'
            ORDER BY days DESC
            LIMIT 40
        """,
    ),
    Chart(
        key="created_vs_closed",
        section="Flow health",
        title="Created versus closed per week",
        kind="lines",
        caption="Where created and delivered diverge, the backlog is growing. "
                "Dropped work is counted separately: it is a real outcome, and "
                "it is not delivery.",
        # Interactive: ~100 weekly points across three series, where reading an
        # exact value off a 480px axis is guesswork.
        options={"x": "week", "series": ["created", "delivered", "dropped"],
                 "interactive": True},
        sql="""
            WITH c AS (
                SELECT date_trunc('week', created) AS week, count(*) AS created
                FROM issues WHERE in_window(created) GROUP BY 1
            ),
            d AS (
                SELECT date_trunc('week', ts) AS week,
                       count(*) FILTER (WHERE NOT abandoned) AS delivered,
                       count(*) FILTER (WHERE abandoned) AS dropped
                FROM closures WHERE in_window(ts) GROUP BY 1
            )
            -- ::DATE because date_trunc returns a TIMESTAMP, and the axis label is
            -- the value's own str(): a weekly chart was printing '2026-01-05 00:00:00',
            -- nineteen characters of which the last eight are always midnight.
            SELECT COALESCE(c.week, d.week)::DATE AS week,
                   COALESCE(c.created, 0) AS created,
                   COALESCE(d.delivered, 0) AS delivered,
                   COALESCE(d.dropped, 0) AS dropped
            FROM c FULL OUTER JOIN d ON c.week = d.week
            ORDER BY week
        """,
    ),
    Chart(
        key="flow_trend",
        section="Flow health",
        title="New versus done, four week trend",
        kind="lines",
        caption="The same counts as the chart above, smoothed over four weeks. "
                "Week to week noise hides the direction; this is the direction. "
                "Work leaves the backlog by being delivered or dropped, so read "
                "the new line against both of the others, not against done alone.",
        options={"x": "week", "series": ["new_trend", "done_trend", "dropped_trend"],
                 "interactive": True},
        # The week series is generated rather than taken from the data, so a week
        # in which nothing happened is a zero rather than a missing row. Without
        # that a four-week mean averages four arbitrary weeks instead of four
        # calendar ones, and reads a quiet period as steady output.
        #
        # The mean is computed over every week and the window is applied after,
        # so the first weeks shown average the weeks genuinely before them. Filter
        # first and the mean restarts at the window edge, drawing a ramp out of
        # nothing.
        sql="""
            WITH weeks AS (
                SELECT unnest(generate_series(
                    (SELECT min(date_trunc('week', created)) FROM issues),
                    (SELECT max(date_trunc('week', created)) FROM issues),
                    INTERVAL 1 WEEK))::DATE AS week
            ),
            c AS (SELECT date_trunc('week', created)::DATE w, count(*) n
                  FROM issues GROUP BY 1),
            d AS (SELECT date_trunc('week', ts)::DATE w, count(*) n
                  FROM closures WHERE NOT abandoned GROUP BY 1),
            -- Dropped work leaves the backlog exactly as delivered work does.
            -- Omitting it made the gap between the two lines read as backlog
            -- growth of 271 over 26 weeks where the backlog chart showed 121.
            x AS (SELECT date_trunc('week', ts)::DATE w, count(*) n
                  FROM closures WHERE abandoned GROUP BY 1),
            trend AS (
                SELECT weeks.week,
                       round(avg(COALESCE(c.n, 0)) OVER (
                           ORDER BY weeks.week ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
                       ), 1) AS new_trend,
                       round(avg(COALESCE(d.n, 0)) OVER (
                           ORDER BY weeks.week ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
                       ), 1) AS done_trend,
                       round(avg(COALESCE(x.n, 0)) OVER (
                           ORDER BY weeks.week ROWS BETWEEN 3 PRECEDING AND CURRENT ROW
                       ), 1) AS dropped_trend
                FROM weeks
                LEFT JOIN c ON c.w = weeks.week
                LEFT JOIN d ON d.w = weeks.week
                LEFT JOIN x ON x.w = weeks.week
            )
            SELECT week, new_trend, done_trend, dropped_trend
            FROM trend
            WHERE in_window(week)
            ORDER BY week
        """,
    ),
    Chart(
        key="flow_per_sprint",
        section="Flow health",
        title="New, delivered and dropped per sprint",
        kind="hbars",
        caption="Every ticket mutation attributed to the sprint that was running "
                "when it happened, rather than to a calendar week. Sprints vary "
                "from three to twenty days here, so read these as totals for a "
                "sprint and not as rates.",
        options={"labels": "sprint", "series": ["arrived", "delivered", "dropped"],
                 # Coverage counts mutations here, not tickets: "of 22347 tickets"
                 # would be a false sentence about a project with 1151 of them.
                 "unit": "mutations"},
        # Sprint totals, not per-day rates: the lengths genuinely differ, and
        # dividing would invent a precision the sprint boundaries do not have.
        sql="""
            SELECT ms.sprint_name AS sprint,
                   count(*) FILTER (WHERE ms.kind = 'created') AS arrived,
                   count(*) FILTER (WHERE c.key IS NOT NULL AND NOT c.abandoned) AS delivered,
                   count(*) FILTER (WHERE c.key IS NOT NULL AND c.abandoned) AS dropped
            FROM mutation_sprint ms
            LEFT JOIN closures c
                   ON c.key = ms.key AND c.ts = ms.ts AND ms.kind = 'status'
            WHERE in_window(ms.ts)
            GROUP BY 1, ms.sprint_start
            ORDER BY ms.sprint_start DESC
        """,
        # Two thirds attribute; the rest fall between sprints or inside two with
        # the ticket in neither. Stated rather than implied.
        coverage="""
            SELECT (SELECT count(*) FROM mutation_sprint WHERE in_window(ts)),
                   (SELECT count(*) FROM mutations WHERE in_window(ts))
        """,
    ),
    Chart(
        key="cfd",
        section="Flow health",
        title="Cumulative flow",
        kind="stacked",
        caption="Tickets per status, sampled once a week. A widening band is a queue.",
        # Interactive: 26 weekly snapshots across 9 bands, where reading one
        # band off a stacked SVG means measuring the gap by eye.
        options={"x": "day", "band": "status", "value": "tickets", "interactive": True},
        # One snapshot day per week, NOT date_trunc + count(*) over the week: that
        # sums seven days of counts and inflates every value about sevenfold while
        # leaving the shape intact, so the error is invisible in the picture. Daily
        # bars were the plan's version and produced 222 bars 1px wide in a 78KB SVG.
        sql=f"""
            WITH horizon AS (
                -- Spans close at the moment derive ran, so a series running to
                -- current_date ends after the data does. The inner join then
                -- drops that snapshot rather than showing it as zero, and the
                -- chart stops early with nothing to say it has.
                SELECT least(current_date, max(left_at)::DATE) AS d FROM status_durations
            ),
            days AS (
                SELECT unnest(generate_series(
                    greatest((SELECT min(created)::DATE FROM issues),
                             (SELECT d FROM horizon) - INTERVAL {WINDOW_WEEKS} WEEK),
                    (SELECT d FROM horizon), INTERVAL 1 DAY))::DATE AS day
            ),
            snapshots AS (SELECT max(day) AS day FROM days
                          WHERE in_window(day) GROUP BY date_trunc('week', day))
            SELECT s.day, d.status, count(*) AS tickets
            FROM snapshots s
            JOIN status_durations d
              -- left_at, not left_at::DATE: truncating excludes the final day,
              -- because midnight on that date is not less than the date itself.
              ON s.day >= d.entered::DATE AND s.day < d.left_at
            GROUP BY 1, 2
            ORDER BY 1, 2
        """,
    ),
    Chart(
        key="net_open",
        section="Flow health",
        title="Open tickets over time",
        kind="lines",
        caption="What the gap between new and done adds up to. Counted from the "
                "status history rather than created minus closed, because a "
                "reopened ticket closes twice and that arithmetic double counts it.",
        options={"x": "day", "series": ["open_tickets"], "interactive": True},
        sql=f"""
            WITH horizon AS (
                SELECT least(current_date, max(left_at)::DATE) AS d FROM status_durations
            ),
            days AS (
                SELECT unnest(generate_series(
                    greatest((SELECT min(created)::DATE FROM issues),
                             (SELECT d FROM horizon) - INTERVAL {WINDOW_WEEKS} WEEK),
                    (SELECT d FROM horizon), INTERVAL 1 DAY))::DATE AS day
            ),
            snapshots AS (SELECT max(day) AS day FROM days
                          WHERE in_window(day) GROUP BY date_trunc('week', day))
            SELECT s.day,
                   count(DISTINCT d.key) FILTER (WHERE st.category <> 'done') AS open_tickets
            FROM snapshots s
            JOIN status_durations d ON s.day >= d.entered::DATE AND s.day < d.left_at
            JOIN statuses st ON st.name = d.status
            GROUP BY 1
            ORDER BY 1
        """,
    ),
    Chart(
        key="cycle_scatter",
        section="Flow health",
        title="Cycle time",
        kind="scatter",
        caption="One point per closed ticket. The 85th percentile is the number "
                "you can promise; the median is the one you will be asked for.",
        # 309 points on the real project. Zoom and per-point readout are the
        # difference between a cloud and a chart you can interrogate.
        options={"x": "resolved", "y": "cycle_days", "interactive": True, "guides_sql": """
            SELECT quantile_cont(cycle_days, 0.5), quantile_cont(cycle_days, 0.85)
            FROM cycle_times
        """},
        sql="SELECT resolved, cycle_days FROM cycle_times "
            "WHERE in_window(resolved) ORDER BY resolved",
        coverage="""
            SELECT (SELECT count(*) FROM cycle_times WHERE in_window(resolved)),
                   (SELECT count(*) FROM issues
                    WHERE resolved IS NOT NULL AND in_window(resolved))
        """,
    ),
    Chart(
        key="time_in_status",
        section="Flow health",
        title="Median days in status, by issue type",
        kind="matrix",
        caption="Where the weeks actually go. Review queues show up here first.",
        # A matrix, not grouped bars: the type names are data, so a bar chart
        # would need a pivot with dynamic columns. Shading a table costs nothing
        # and reuses a renderer that already exists.
        # `tickets` is not decoration: flows skip statuses, so each row is a median
        # over a different population. On the first real project one status held
        # 1145 tickets and another held 1, and shaded by days alone the two rows
        # looked equally authoritative. Shading stays on days; the count is there
        # to say how much weight the number can carry.
        options={"headers": ["type", "status", "days", "tickets"], "shade": "days",
                 "sortable": True},
        # Done-category statuses are excluded because status_durations closes an
        # open span at now(): a closed ticket's Done span measures time since
        # resolution, which read 207 days in the fixtures and swamped the real
        # queues. An open ticket's current span still ends at now(), and that one
        # is exactly what this chart should measure.
        sql="""
            SELECT i.type, d.status,
                   quantile_cont(date_diff('minute', d.entered, d.left_at) / 1440.0, 0.5) AS days,
                   count(DISTINCT d.key) AS tickets
            FROM status_durations d
            JOIN issues i ON i.key = d.key
            JOIN statuses s ON s.name = d.status
            WHERE s.category <> 'done' AND in_window(d.entered)
            GROUP BY 1, 2
            HAVING count(*) >= 1
            ORDER BY 1, 2
        """,
    ),
    Chart(
        key="per_fix_version",
        section="Reporting outward",
        title="Delivered versus open, per version",
        kind="hbars",
        caption="The delivery view. One bar pair per version a ticket is tagged with.",
        options={"labels": "fix_version", "series": ["delivered", "dropped", "open"]},
        # UNNEST, so a ticket tagged with two versions counts in both rather than
        # being dropped or arbitrarily attributed to one.
        sql="""
            SELECT v AS fix_version,
                   count(*) FILTER (WHERE status_category = 'done' AND NOT abandoned)
                       AS delivered,
                   count(*) FILTER (WHERE abandoned) AS dropped,
                   count(*) FILTER (WHERE status_category <> 'done') AS open
            FROM issues i, UNNEST(i.fix_versions) AS t(v)
            WHERE (in_window(i.created) OR in_window(i.resolved))
            GROUP BY 1
            ORDER BY 1
        """,
    ),
    Chart(
        key="per_epic",
        section="Reporting outward",
        title="Progress per epic",
        kind="bars",
        caption="Tickets per parent, largest first. Drag across the chart to zoom, "
                "and hover a bar for the epic title and the numbers. Parents "
                "outside the scope of this report appear by key alone, which is "
                "about a third of them.",
        # Back to bars now that a plot can be zoomed. 141 epics is 423 marks in
        # 480px, about a pixel each, which is why this was a table for a while:
        # the static SVG below is still that dense and that is the honest cost of
        # the fallback. Drag-to-zoom is what makes the upgraded version readable.
        # delivered/dropped/open stay disjoint and sum to the total.
        options={"labels": "epic", "series": ["delivered", "dropped", "open"],
                 "interactive": True},
        sql="""
            SELECT CASE WHEN e.summary IS NULL THEN i.parent
                        ELSE i.parent || '  ' || e.summary END AS epic,
                   count(*) FILTER (WHERE i.status_category = 'done' AND NOT i.abandoned)
                       AS delivered,
                   count(*) FILTER (WHERE i.abandoned) AS dropped,
                   count(*) FILTER (WHERE i.status_category <> 'done') AS open,
                   round(100.0 * count(*) FILTER (WHERE i.status_category = 'done'
                                                    AND NOT i.abandoned) / count(*)) AS percent_done
            FROM issues i
            -- The parent is routinely outside the fetched scope, so this is a LEFT
            -- JOIN and the label falls back to the bare key rather than half a one.
            LEFT JOIN issues e ON e.key = i.parent
            WHERE i.parent IS NOT NULL AND (in_window(i.created) OR in_window(i.resolved))
            GROUP BY 1
            ORDER BY count(*) DESC
        """,
    ),
    Chart(
        key="type_mix",
        section="Reporting outward",
        title="Ticket type mix per month",
        kind="stacked",
        caption="How much of each month was planned work. A growing bug or "
                "incident band is the interesting case.",
        options={"x": "month", "band": "type", "value": "tickets", "interactive": True},
        # ::DATE for the same reason as created_vs_closed: date_trunc returns a
        # TIMESTAMP and the tick label is the value's own str().
        sql="""
            SELECT date_trunc('month', created)::DATE AS month, type,
                   count(*) AS tickets
            FROM issues i
            WHERE (in_window(i.created) OR in_window(i.resolved))
            GROUP BY 1, 2
            ORDER BY 1, 2
        """,
    ),
    Chart(
        key="rework_per_sprint",
        section="Retro",
        title="Rework per sprint",
        kind="hbars",
        caption="Transitions that moved a ticket backwards through the workflow. "
                "The single best retro chart, and one no built-in report draws.",
        options={"labels": "sprint", "series": ["backward_moves"]},
        # Attributed by when the transition happened, not by the ticket's latest
        # sprint: a backward move belongs to the sprint it occurred in.
        sql="""
            SELECT s.sprint_name AS sprint, count(*) AS backward_moves
            FROM rework r
            JOIN issue_sprints s
              ON s.key = r.key AND r.ts >= s.start AND r.ts < s."end"
            WHERE in_window(r.ts)
            GROUP BY 1
            ORDER BY min(s.start) DESC
        """,
        # From the first live run: 64 tickets carried rework and the project used
        # no sprints, so this drew a bare "no data" that reads as "no rework",
        # the opposite of the truth. Counted in tickets rather than transitions
        # because that is the unit coverage_strip's wording uses.
        coverage="""
            SELECT (SELECT count(DISTINCT r.key) FROM rework r
                    JOIN issue_sprints s
                      ON s.key = r.key AND r.ts >= s.start AND r.ts < s."end"
                    WHERE in_window(r.ts)),
                   (SELECT count(DISTINCT key) FROM rework WHERE in_window(ts))
        """,
    ),
    Chart(
        key="carry_over",
        section="Retro",
        title="Carried into each sprint",
        kind="hbars",
        caption="Tickets that were already in an earlier sprint. Persistent "
                "carry-over means the sprint is being planned optimistically.",
        options={"labels": "sprint", "series": ["carried"]},
        sql="""
            SELECT sprint_name AS sprint, count(DISTINCT key) AS carried
            FROM issue_sprints
            WHERE ordinal > 1 AND in_window(start)
            GROUP BY 1
            ORDER BY min(start) DESC
        """,
        # Same reason as rework_per_sprint: with no sprints anywhere this drew an
        # empty chart rather than saying the project does not use them.
        coverage="""
            SELECT (SELECT count(DISTINCT key) FROM issue_sprints WHERE in_window(start)),
                   (SELECT count(*) FROM issues i WHERE in_window(i.created)
                                                     OR in_window(i.resolved))
        """,
    ),
    Chart(
        key="cycle_per_sprint",
        section="Retro",
        title="Cycle time per sprint",
        kind="hbars",
        caption="Median and 85th percentile days per sprint. Tightening is the "
                "thing to look for, not the absolute value.",
        options={"labels": "sprint", "series": ["median_days", "p85_days"]},
        # Attributed to the ticket's LAST sprint rather than to whichever sprint
        # window contains its resolution. Work routinely closes after the sprint
        # that carried it (PROJ-1 resolves the day after its sprint ends), and
        # the window version dropped every such ticket, leaving the chart empty
        # while its coverage still reported 100%.
        sql="""
            WITH last_sprint AS (
                SELECT key, sprint_name, start,
                       row_number() OVER (PARTITION BY key ORDER BY ordinal DESC) AS rn
                FROM issue_sprints
            )
            SELECT s.sprint_name AS sprint,
                   quantile_cont(c.cycle_days, 0.5) AS median_days,
                   quantile_cont(c.cycle_days, 0.85) AS p85_days
            FROM cycle_times c
            JOIN last_sprint s ON s.key = c.key AND s.rn = 1
            WHERE in_window(c.resolved)
            GROUP BY 1
            ORDER BY min(s.start) DESC
        """,
        # Counts what the chart plots (cycle times that have a sprint) over what it
        # would plot if every ticket had one, not sprint membership over all issues.
        coverage="""
            SELECT (SELECT count(*) FROM cycle_times c
                    WHERE in_window(c.resolved) AND EXISTS
                      (SELECT 1 FROM issue_sprints s WHERE s.key = c.key)),
                   (SELECT count(*) FROM cycle_times WHERE in_window(resolved))
        """,
    ),
    Chart(
        key="points_vs_cycle",
        section="Retro",
        title="Story points versus actual cycle time",
        kind="scatter",
        caption="Whether the estimates carry information. If the cloud is flat, "
                "the points are ritual.",
        options={"x": "story_points", "y": "cycle_days", "interactive": True,
                 "guides_sql": """
            SELECT quantile_cont(cycle_days, 0.5), quantile_cont(cycle_days, 0.85)
            FROM cycle_times
        """},
        sql="""
            SELECT i.story_points, c.cycle_days
            FROM issues i JOIN cycle_times c ON c.key = i.key
            -- > 0 rather than IS NOT NULL: this instance stores an unestimated
            -- ticket as 0, not NULL, so IS NOT NULL reported 100% coverage while
            -- 69% of tickets carried no estimate and 131 of 309 plotted points
            -- sat on the x axis at zero.
            WHERE i.story_points > 0 AND in_window(c.resolved)
            ORDER BY i.story_points
        """,
        # Rows actually plotted over rows that could be, not points-present over all
        # issues: a ticket with points but no cycle time never reaches this chart.
        coverage="""
            SELECT (SELECT count(*) FROM issues i JOIN cycle_times c ON c.key = i.key
                    WHERE i.story_points > 0 AND in_window(c.resolved)),
                   (SELECT count(*) FROM cycle_times WHERE in_window(resolved))
        """,
        tier="points",
    ),
    Chart(
        key="throughput_per_person",
        section="People",
        title="Tickets closed per week, per person",
        kind="multiline",
        caption="One line each, on one set of axes. Click a name in the legend to "
                "isolate it, and drag across the chart to zoom. Attributed to the "
                "assignee at close.",
        # One chart rather than 25 panels. The panels were legible individually and
        # hopeless for the comparison people actually want, which is who is
        # carrying what over the same weeks. With more people than the palette has
        # colours, three share each: the legend and the hover readout are what
        # separate them, which is why this shape needs the upgrade more than most.
        options={"band": "person", "x": "week", "y": "closed", "value": "closed",
                 "interactive": True},
        # ::DATE for consistency with the other time-bucketed charts. Not for the
        # midnight-label guard: small multiples emit facet titles and no tick
        # labels, so that guard never reaches this chart.
        sql="""
            SELECT COALESCE(p.display_name, 'Unassigned') AS person,
                   date_trunc('week', c.ts)::DATE AS week,
                   count(*) AS closed
            FROM closures c
            JOIN issues i ON i.key = c.key
            LEFT JOIN people p ON p.account_id = i.assignee_id
            WHERE NOT c.abandoned AND in_window(c.ts)
            GROUP BY 1, 2
            ORDER BY 1, 2
        """,
    ),
    Chart(
        key="review_load",
        section="People",
        title="Review load",
        kind="hbars",
        caption="Who moves work out of the review status, counting a rejection "
                "back to in-progress as well as an approval. This is the "
                "invisible contribution: no built-in report exposes it.",
        options={"labels": "reviewer", "series": ["reviews"]},
        sql="""
            SELECT COALESCE(p.display_name, 'Automation') AS reviewer,
                   count(*) AS reviews
            FROM transitions t
            LEFT JOIN people p ON p.account_id = t.author_id
            WHERE t.from_status = (SELECT review_status FROM sync_state)
              AND in_window(t.ts)
            GROUP BY 1
            ORDER BY reviews DESC
        """,
    ),
    Chart(
        key="handoffs",
        section="People",
        title="Handoffs",
        kind="matrix",
        caption="Who starts work that someone else finishes. Read down the rows "
                "for what a person hands on, across for what they pick up.",
        options={"headers": ["started_by", "finished_by", "tickets"], "shade": "tickets",
                 "sortable": True},
        sql="""
            WITH started AS (   -- assignee display name at the first move into the start status
                SELECT t.key,
                       arg_min(COALESCE(a.to_str, r.display_name), t.ts) AS started_by
                FROM transitions t
                JOIN issues i ON i.key = t.key
                LEFT JOIN people r ON r.account_id = i.reporter_id
                LEFT JOIN changes a
                       ON a.key = t.key AND a.field = 'assignee' AND a.ts <= t.ts
                WHERE t.to_status = (SELECT start_status FROM sync_state)
                  AND in_window(t.ts)
                GROUP BY t.key
            ),
            finished AS (
                SELECT c.key, COALESCE(p.display_name, 'Unassigned') AS finished_by
                FROM closures c
                JOIN issues i ON i.key = c.key
                LEFT JOIN people p ON p.account_id = i.assignee_id
            )
            SELECT s.started_by, f.finished_by, count(*) AS tickets
            FROM started s JOIN finished f USING (key)
            WHERE s.started_by IS DISTINCT FROM f.finished_by
            GROUP BY 1, 2
            ORDER BY tickets DESC
        """,
    ),
    Chart(
        key="points_per_person",
        section="People",
        title="Story points closed per person",
        kind="hbars",
        caption="Only meaningful if the field is filled consistently, which the "
                "coverage figure tells you.",
        options={"labels": "person", "series": ["points"]},
        sql="""
            SELECT COALESCE(p.display_name, 'Unassigned') AS person,
                   sum(i.story_points) AS points
            FROM issues i
            LEFT JOIN people p ON p.account_id = i.assignee_id
            -- > 0, not IS NOT NULL: an unestimated ticket is stored as 0 here.
            WHERE i.status_category = 'done' AND NOT i.abandoned
              AND i.story_points > 0 AND (in_window(i.created) OR in_window(i.resolved))
            GROUP BY 1
            ORDER BY points DESC
        """,
        coverage="""
            SELECT (SELECT count(*) FROM issues
                    WHERE story_points > 0 AND status_category = 'done' AND NOT abandoned
                      AND (in_window(created) OR in_window(resolved))),
                   (SELECT count(*) FROM issues
                    WHERE status_category = 'done' AND NOT abandoned
                      AND (in_window(created) OR in_window(resolved)))
        """,
        tier="points",
    ),
]
