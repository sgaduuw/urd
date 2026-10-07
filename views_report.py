"""`/` and `/<slug>/`, and the flag controls.

Flags are read from the query string and applied inside this request's own
cursor, in a transaction that is always rolled back: the writes never reach
sync_state, so two browser tabs never fight over each other's window, and
Save as default is the way a default is changed.
"""
import threading
import urllib.parse

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


def flags_from(args, project, con):
    """Query string over stored defaults, with the errors collected, not raised.

    urd's validators exit on bad input, which is right for a CLI and a 500 through
    a route. Each is caught, reported on the page, and falls back to that
    project's stored default.

    `con` is the caller's own cursor, inside its own transaction: applying a
    request's flags means writing them (the window, the epics and the
    components live in tables the chart SQL reads at query time, not as
    parameters report_html takes),
    and the transaction is what keeps that write from ever reaching another
    connection.
    """
    problems = []
    stored_window = urd.load_scope(con)["report_since"]
    since = args.get("since", stored_window)
    epics = args.getlist("exclude_epic")
    epics = [k.strip() for value in epics for k in value.split(",") if k.strip()]
    if not epics and "exclude_epic" not in args:
        epics = urd.stored_excluded_epics(con)
    components = args.getlist("component")
    if not components and "component" not in args:
        components = urd.stored_report_components(con)
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

    # No validator to fail: a checkbox cannot offer a name the mirror does not
    # hold, and a hand-typed one that matches nothing empties the report under
    # a header that names it.
    urd.set_report_components(con, components)

    try:
        tiers = urd.parse_thresholds(args.getlist("threshold"), base=tiers)
    except SystemExit as exc:
        problems.append(str(exc))

    return {"since": since, "epics": epics, "components": components,
            "offered": urd.components_present(con),
            "tiers": tiers, "problems": problems}


def _component_boxes(flags):
    """Nothing at all when the mirror holds no components: there is then no
    slice to choose between, and an empty control only invites the question."""
    if not flags["offered"]:
        return ""
    boxes = "".join(
        f'<label><input type="checkbox" name="component"'
        f' value="{render.esc(name)}"'
        f'{" checked" if name in flags["components"] else ""}>'
        f' {render.esc(name)}</label>'
        for name in flags["offered"]
    )
    # An all-unticked form sends no component key, which would read as "keep the
    # stored slice". The empty value is dropped by set_report_components and makes
    # "no box ticked" mean every component, for Apply and Save as default alike.
    return (f'<span class="boxes">component'
            f'<input type="hidden" name="component" value="">{boxes}</span>')


def _section(args):
    section = args.get("section", "attention")
    return section if section in urd.SECTION_TITLES else "attention"


def _filters_query(args):
    """The current filters as a query string, without the tab, so a tab switch
    keeps them. Multi-valued keys keep every value."""
    return urllib.parse.urlencode(
        [(k, v) for k, v in args.items(multi=True) if k != "section"])


def _tabs(slug, active, query):
    base = f"/{urllib.parse.quote(slug)}"
    links = []
    for key, title in urd.SECTION_TABS:
        page_url = f"{base}/?" + (f"{query}&" if query else "") + f"section={key}"
        fragment_url = f"{base}/sections/{key}" + (f"?{query}" if query else "")
        current = ' aria-current="page"' if key == active else ""
        links.append(
            f'<a href="{render.esc(page_url)}" hx-get="{render.esc(fragment_url)}"'
            f' hx-target="#report" hx-push-url="{render.esc(page_url)}"{current}>'
            f"{render.esc(title)}</a>")
    return f'<nav class="tabs" aria-label="Report sections">{"".join(links)}</nav>'


def _controls(project, flags, others, section):
    switcher = " ".join(
        f'<a href="/{render.esc(p.slug)}/">{render.esc(p.slug)}</a>' for p in others
        if p.slug != project.slug
    )
    problems = "".join(
        f'<p class="warn">{render.esc(p)}</p>' for p in flags["problems"])
    return (
        f'<p><a href="/{render.esc(project.slug)}/capacity/">Plan capacity</a></p>'
        f'<form method="get" action="/{render.esc(project.slug)}/" class="controls">'
        f'<label>since <input name="since" value="{render.esc(flags["since"] or "")}"'
        f' placeholder="YYYY-MM-DD"></label>'
        f'<label>exclude epic <input name="exclude_epic"'
        f' value="{render.esc(",".join(flags["epics"]))}"></label>'
        f'<label>threshold <input name="threshold" placeholder="default=0.40"></label>'
        f'{_component_boxes(flags)}'
        f'<input type="hidden" name="section" value="{render.esc(section)}">'
        f'<button type="submit">Apply</button>'
        f'<button type="submit" formmethod="post"'
        f' formaction="/{render.esc(project.slug)}/defaults">Save as default</button></form>'
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
    section = _section(flask.request.args)
    con = found.con.cursor()
    with _RENDER_LOCK:
        con.execute("BEGIN")
        try:
            if not webapp.report_ready(found, con):
                # No report to decorate: project_page's own notice already
                # offers whatever action applies (finish setup, refresh), and
                # splicing the controls form on top would add a second
                # Refresh button and a since/exclude box that does
                # nothing without a report under it.
                return webapp.project_page(found, con=con)
            flags = flags_from(flask.request.args, found, con)
            # The job's own state, not a query-string marker: start_refresh can return
            # False for three different reasons (already running, no scope, the
            # database would not open), and collapsing all three onto one marker meant
            # this text was wrong two times out of three. Reading job.state directly
            # (projects.job_message, shared with webapp's notice pages) is accurate
            # for whichever reason applies, and also surfaces a failure that happened
            # after the redirect already sent the clicker back here, which used to be
            # reported nowhere at all.
            message = projects.job_message(found)
            if message:
                flags["problems"].append(message)
            tabs = _tabs(slug, section, _filters_query(flask.request.args))
            page = webapp.project_page(found, flags["tiers"], con, section, tabs)
        finally:
            con.execute("ROLLBACK")

    controls = _controls(found, flags, registry.projects(), section)
    # Injected after <body> so the controls precede the report without the report
    # needing to know they exist.
    return page.replace("<body>", "<body>" + controls, 1)


@bp.get("/<slug>/sections/<section>")
def section_fragment(slug, section):
    registry = flask.current_app.config["REGISTRY"]
    found = webapp.slug_or_404(registry, slug)
    if section not in urd.SECTION_TITLES or found.con is None or not found.configured():
        flask.abort(404)
    con = found.con.cursor()
    with _RENDER_LOCK:
        con.execute("BEGIN")
        try:
            if not webapp.report_ready(found, con):
                flask.abort(404)
            flags = flags_from(flask.request.args, found, con)
            tabs = _tabs(slug, section, _filters_query(flask.request.args))
            return urd.report_body(con, flags["tiers"], section, tabs)
        finally:
            con.execute("ROLLBACK")


@bp.post("/<slug>/defaults")
def save_defaults(slug):
    """The one write path for report defaults.
    Every value is applied or none is."""
    registry = flask.current_app.config["REGISTRY"]
    found = webapp.slug_or_404(registry, slug)
    if found.con is None or not found.configured():
        flask.abort(404)
    form = flask.request.form
    con = found.con.cursor()
    with _RENDER_LOCK:
        con.execute("BEGIN")
        try:
            flags = flags_from(form, found, con)
            saved = not flags["problems"]
            if saved:
                urd.save_scope(con, thresholds=urd.format_thresholds(flags["tiers"]))
            con.execute("COMMIT" if saved else "ROLLBACK")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    if not saved:
        return flask.redirect(f"/{slug}/?{urllib.parse.urlencode(list(form.items(multi=True)))}")
    return flask.redirect(f"/{slug}/?section={_section(form)}")
