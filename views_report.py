"""`/` and `/<slug>/`, and the flag controls.

Flags are read from the query string and applied inside this request's own
cursor, in a transaction that is always rolled back: the writes never reach
sync_state, so two browser tabs never fight over each other's window and the
CLI stays the way a default is changed.
"""
import threading

import flask

import projects
import render
import urd
import webapp

bp = flask.Blueprint("report", __name__)

# ponytail: one lock for every project, not one per project. A render measures
# about 194ms and this is a single-user tool, so two renders serializing is not
# worth solving further. Upgrade path: a per-project lock (mirroring
# projects.Project's refresh lock) if concurrent readers on different projects
# ever start to matter.
#
# This and the per-request cursor below are complementary, not alternatives:
# the cursor stops a render from reading state the sync thread's next
# execute() already overwrote (corruption between a render and a concurrent
# sync); the lock stops two overlapping renders from writing the same
# one-row report_window at once, which DuckDB detects as a write-write
# conflict, not corruption, and 500s on ("Conflict on tuple deletion"). The
# cursor only isolates a render from the sync thread; it does nothing about
# two renders racing each other, which is exactly the gap that opened when
# this lock was removed under the belief the cursor had made it redundant.
_RENDER_LOCK = threading.Lock()


def flags_from(request, project, con):
    """Query string over stored defaults, with the errors collected, not raised.

    urd's validators exit on bad input, which is right for a CLI and a 500 through
    a route. Each is caught, reported on the page, and falls back to that
    project's stored default.

    `con` is the caller's own cursor, inside its own transaction: applying a
    request's flags means writing them (window/epics/min_closed live in tables
    the chart SQL reads at query time, not as parameters report_html takes),
    and the transaction is what keeps that write from ever reaching another
    connection.
    """
    problems = []
    stored_window = urd.load_scope(con)["report_since"]
    since = request.args.get("since", stored_window)
    epics = request.args.getlist("exclude_epic")
    epics = [k.strip() for value in epics for k in value.split(",") if k.strip()]
    if not epics and "exclude_epic" not in request.args:
        epics = urd.stored_excluded_epics(con)
    raw_floor = request.args.get("min_closed")
    tiers = urd.stored_thresholds(con)

    try:
        urd.set_report_window(con, since or None)
    except SystemExit as exc:
        problems.append(str(exc))
        urd.set_report_window(con, stored_window)

    try:
        urd.set_excluded_epics(con, epics)
    except SystemExit as exc:
        problems.append(str(exc))

    floor = urd.stored_min_closed(con)
    if raw_floor is not None:
        try:
            floor = int(raw_floor)
            urd.set_min_closed(con, floor)
        except (SystemExit, ValueError) as exc:
            problems.append(f"--min-closed: {exc}")
            urd.set_min_closed(con, urd.stored_min_closed(con))

    try:
        tiers = urd.parse_thresholds(request.args.getlist("threshold"), base=tiers)
    except SystemExit as exc:
        problems.append(str(exc))

    # The job's own state, not a query-string marker: start_refresh can return
    # False for three different reasons (already running, no scope, the
    # database would not open), and collapsing all three onto one marker meant
    # this text was wrong two times out of three. Reading job.state directly
    # (projects.job_message, shared with webapp's notice pages) is accurate
    # for whichever reason applies, and also surfaces a failure that happened
    # after the redirect already sent the clicker back here, which used to be
    # reported nowhere at all.
    message = projects.job_message(project)
    if message:
        problems.append(message)

    return {"since": since, "epics": epics, "min_closed": floor,
            "tiers": tiers, "problems": problems}


def _controls(project, flags, others):
    switcher = " ".join(
        f'<a href="/{render.esc(p.slug)}/">{render.esc(p.slug)}</a>' for p in others
        if p.slug != project.slug
    )
    problems = "".join(
        f'<p class="warn">{render.esc(p)}</p>' for p in flags["problems"])
    return (
        f'<form method="get" action="/{render.esc(project.slug)}/" class="controls">'
        f'<label>since <input name="since" value="{render.esc(flags["since"] or "")}"'
        f' placeholder="YYYY-MM-DD"></label>'
        f'<label>min closed <input name="min_closed" type="number" min="1"'
        f' value="{flags["min_closed"]}"></label>'
        f'<label>exclude epic <input name="exclude_epic"'
        f' value="{render.esc(",".join(flags["epics"]))}"></label>'
        f'<label>threshold <input name="threshold" placeholder="default=0.5"></label>'
        f'<button type="submit">Apply</button></form>'
        f'<form method="post" action="/{render.esc(project.slug)}/refresh">'
        f'<button type="submit">Refresh</button></form>'
        f'{problems}'
        + (f"<p>other projects: {switcher}</p>" if switcher else "")
    )


@bp.get("/")
def root():
    registry = flask.current_app.config["REGISTRY"]
    configured = [p for p in registry.projects() if p.configured()]
    if not configured:
        return flask.redirect("/setup")
    return flask.redirect(f"/{configured[0].slug}/")


@bp.get("/<slug>/", strict_slashes=False)
def project(slug):
    registry = flask.current_app.config["REGISTRY"]
    found = webapp.slug_or_404(registry, slug)
    if found.con is None or not found.configured():
        return webapp.project_page(found)

    # Render on this request's own cursor, inside a transaction that is always
    # rolled back, and under _RENDER_LOCK (see its own comment for why both
    # exist). found.con is shared with the background sync thread, and
    # DuckDBPyConnection.execute returns the connection itself rather than a
    # separate result object, so reading `description`/`fetchall` off it after
    # a concurrent execute() reads state the sync thread already overwrote.
    # A cursor's own transaction is isolated from that: it sees a stable
    # snapshot regardless of what the sync thread commits meanwhile, and the
    # rollback discards this request's flag writes unconditionally, including
    # on a render that raises, which a bare apply/render/restore sequence
    # would not. None of that stops two overlapping renders from both writing
    # report_window's one row, which is what the lock is for.
    con = found.con.cursor()
    with _RENDER_LOCK:
        con.execute("BEGIN")
        try:
            if not webapp.report_ready(found, con):
                # No report to decorate: project_page's own notice already
                # offers whatever action applies (finish setup, refresh), and
                # splicing the controls form on top would add a second
                # Refresh button and a since/min-closed/exclude box that does
                # nothing without a report under it.
                return webapp.project_page(found, con=con)
            flags = flags_from(flask.request, found, con)
            page = webapp.project_page(found, flags["tiers"], con)
        finally:
            con.execute("ROLLBACK")

    controls = _controls(found, flags, registry.projects())
    # Injected after <body> so the controls precede the report without the report
    # needing to know they exist.
    return page.replace("<body>", "<body>" + controls, 1)
