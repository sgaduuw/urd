"""`/` and `/<slug>/`, and the flag controls.

Flags are read from the query string and never written back to sync_state. A
request that wrote its window would make two browser tabs fight over each other,
and the CLI stays the way a default is changed.
"""
import flask

import render
import urd
import webapp

bp = flask.Blueprint("report", __name__)


def flags_from(request, project):
    """Query string over stored defaults, with the errors collected, not raised.

    urd's validators exit on bad input, which is right for a CLI and a 500 through
    a route. Each is caught, reported on the page, and falls back to that
    project's stored default.
    """
    con = project.con
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

    if request.args.get("refused"):
        # Set by the refresh route when a sync is already running. It redirects
        # rather than rendering, so the marker rides in the query string; the
        # whole point of refusing instead of queueing is that the clicker learns.
        problems.append("A refresh is already running for this project.")

    return {"since": since, "epics": epics, "min_closed": floor,
            "tiers": tiers, "problems": problems}


def _controls(project, flags, others):
    switcher = " ".join(
        f'<a href="/{p.slug}/">{render.esc(p.slug)}</a>' for p in others
        if p.slug != project.slug
    )
    problems = "".join(
        f'<p class="warn">{render.esc(p)}</p>' for p in flags["problems"])
    return (
        f'<form method="get" action="/{project.slug}/" class="controls">'
        f'<label>since <input name="since" value="{render.esc(flags["since"] or "")}"'
        f' placeholder="YYYY-MM-DD"></label>'
        f'<label>min closed <input name="min_closed" type="number" min="1"'
        f' value="{flags["min_closed"]}"></label>'
        f'<label>exclude epic <input name="exclude_epic"'
        f' value="{render.esc(",".join(flags["epics"]))}"></label>'
        f'<label>threshold <input name="threshold" placeholder="default=0.5"></label>'
        f'<button type="submit">Apply</button></form>'
        f'<form method="post" action="/{project.slug}/refresh">'
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
    con = found.con
    # window/epics/min_closed live in tables the chart SQL reads at query time,
    # not as parameters report_html takes, so applying a request's flags means
    # writing them. Snapshotting here (before flags_from mutates anything) and
    # restoring after the render is what keeps that mutation request-scoped.
    stored_window = urd.load_scope(con)["report_since"]
    stored_epics = urd.stored_excluded_epics(con)
    stored_floor = urd.stored_min_closed(con)
    flags = flags_from(flask.request, found)
    page = webapp.project_page(found, flags["tiers"])
    urd.set_report_window(con, stored_window)
    urd.set_excluded_epics(con, stored_epics)
    urd.set_min_closed(con, stored_floor)
    controls = _controls(found, flags, registry.projects())
    # Injected after <body> so the controls precede the report without the report
    # needing to know they exist.
    return page.replace("<body>", "<body>" + controls, 1)
