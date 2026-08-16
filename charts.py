"""Chart specifications. Adding a chart means writing SQL, nothing else.

Each spec is data: a query, a renderer kind, and the column names that renderer
needs. `coverage` is SQL returning one row of (numerator, denominator); when the
share falls below `threshold` the chart is replaced by a strip saying so, which
is the difference between a chart that is missing and one that is quietly wrong.
"""
from typing import NamedTuple

SECTIONS = ("Flow health", "Reporting outward", "Retro", "People")

# Optional-field charts are held to a lower bar than always-available ones,
# matching the rule that a field empty on half the tickets is worth showing with
# a caveat but not worth showing bare.
POINTS_THRESHOLD = 0.5
DEFAULT_THRESHOLD = 0.7

# How far back the time series charts look. Long enough to show a trend, short
# enough that the x-axis stays readable and the report stops growing.
WINDOW_WEEKS = 26


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
    threshold: float = DEFAULT_THRESHOLD


CHARTS = [
    Chart(
        key="aging_wip",
        section="Flow health",
        title="Aging work in progress",
        kind="table",
        caption="Open tickets by days in their current status. The chart that "
                "changes what you do today.",
        options={"headers": ["key", "status", "assignee", "days"], "shade": "days"},
        sql="""
            SELECT i.key, i.status,
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
        caption="Where the two lines diverge, the backlog is growing.",
        options={"x": "week", "series": ["created", "closed"]},
        sql="""
            WITH c AS (
                SELECT date_trunc('week', created) AS week, count(*) AS created
                FROM issues GROUP BY 1
            ),
            d AS (
                SELECT date_trunc('week', ts) AS week, count(*) AS closed
                FROM closures GROUP BY 1
            )
            -- ::DATE because date_trunc returns a TIMESTAMP, and the axis label is
            -- the value's own str(): a weekly chart was printing '2026-01-05 00:00:00',
            -- nineteen characters of which the last eight are always midnight.
            SELECT COALESCE(c.week, d.week)::DATE AS week,
                   COALESCE(c.created, 0) AS created,
                   COALESCE(d.closed, 0) AS closed
            FROM c FULL OUTER JOIN d ON c.week = d.week
            ORDER BY week
        """,
    ),
    Chart(
        key="cfd",
        section="Flow health",
        title="Cumulative flow",
        kind="stacked",
        caption="Tickets per status, sampled once a week. A widening band is a queue.",
        options={"x": "day", "band": "status", "value": "tickets"},
        # One snapshot day per week, NOT date_trunc + count(*) over the week: that
        # sums seven days of counts and inflates every value about sevenfold while
        # leaving the shape intact, so the error is invisible in the picture. Daily
        # bars were the plan's version and produced 222 bars 1px wide in a 78KB SVG.
        sql=f"""
            WITH days AS (
                SELECT unnest(generate_series(
                    greatest((SELECT min(created)::DATE FROM issues),
                             current_date - INTERVAL {WINDOW_WEEKS} WEEK),
                    current_date, INTERVAL 1 DAY))::DATE AS day
            ),
            snapshots AS (SELECT max(day) AS day FROM days GROUP BY date_trunc('week', day))
            SELECT s.day, d.status, count(*) AS tickets
            FROM snapshots s
            JOIN status_durations d
              ON s.day >= d.entered::DATE AND s.day < d.left_at::DATE
            GROUP BY 1, 2
            ORDER BY 1, 2
        """,
    ),
    Chart(
        key="cycle_scatter",
        section="Flow health",
        title="Cycle time",
        kind="scatter",
        caption="One point per closed ticket. The 85th percentile is the number "
                "you can promise; the median is the one you will be asked for.",
        options={"x": "resolved", "y": "cycle_days", "guides_sql": """
            SELECT quantile_cont(cycle_days, 0.5), quantile_cont(cycle_days, 0.85)
            FROM cycle_times
        """},
        sql="SELECT resolved, cycle_days FROM cycle_times ORDER BY resolved",
        coverage="""
            SELECT (SELECT count(*) FROM cycle_times),
                   (SELECT count(*) FROM issues WHERE resolved IS NOT NULL)
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
        options={"headers": ["type", "status", "days"], "shade": "days"},
        # Done-category statuses are excluded because status_durations closes an
        # open span at now(): a closed ticket's Done span measures time since
        # resolution, which read 207 days in the fixtures and swamped the real
        # queues. An open ticket's current span still ends at now(), and that one
        # is exactly what this chart should measure.
        sql="""
            SELECT i.type, d.status,
                   quantile_cont(date_diff('minute', d.entered, d.left_at) / 1440.0, 0.5) AS days
            FROM status_durations d
            JOIN issues i ON i.key = d.key
            JOIN statuses s ON s.name = d.status
            WHERE s.category <> 'done'
            GROUP BY 1, 2
            HAVING count(*) >= 1
            ORDER BY 1, 2
        """,
    ),
    Chart(
        key="per_fix_version",
        section="Reporting outward",
        title="Delivered versus open, per version",
        kind="bars",
        caption="The delivery view. One bar pair per version a ticket is tagged with.",
        options={"labels": "fix_version", "series": ["done", "open"]},
        # UNNEST, so a ticket tagged with two versions counts in both rather than
        # being dropped or arbitrarily attributed to one.
        sql="""
            SELECT v AS fix_version,
                   count(*) FILTER (WHERE status_category = 'done') AS done,
                   count(*) FILTER (WHERE status_category <> 'done') AS open
            FROM issues, UNNEST(fix_versions) AS t(v)
            GROUP BY 1
            ORDER BY 1
        """,
    ),
    Chart(
        key="per_epic",
        section="Reporting outward",
        title="Progress per epic",
        kind="bars",
        caption="Tickets done and still open, per parent. Parents outside the scope "
                "of this report appear by key alone.",
        # done and open rather than done and total: the two are disjoint and sum to
        # the total, so the pair is an honest side-by-side read. done against total
        # puts a bar inside another bar and invites reading them as separate work.
        options={"labels": "epic", "series": ["done", "open"]},
        sql="""
            SELECT parent AS epic,
                   count(*) FILTER (WHERE status_category = 'done') AS done,
                   count(*) FILTER (WHERE status_category <> 'done') AS open
            FROM issues
            WHERE parent IS NOT NULL
            GROUP BY 1
            ORDER BY done + open DESC
        """,
    ),
    Chart(
        key="type_mix",
        section="Reporting outward",
        title="Ticket type mix per month",
        kind="stacked",
        caption="How much of each month was planned work. A growing bug or "
                "incident band is the interesting case.",
        options={"x": "month", "band": "type", "value": "tickets"},
        # ::DATE for the same reason as created_vs_closed: date_trunc returns a
        # TIMESTAMP and the tick label is the value's own str().
        sql="""
            SELECT date_trunc('month', created)::DATE AS month, type,
                   count(*) AS tickets
            FROM issues
            GROUP BY 1, 2
            ORDER BY 1, 2
        """,
    ),
]
