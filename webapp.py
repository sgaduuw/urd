"""The Flask app: wiring, and the states that are not a chart.

Routes live in three blueprints rather than here, so three implementers can work
on them without touching one file. Keep that split: collapsing them back into this
module makes every route change serial.
"""
import flask
import werkzeug.exceptions

import projects
import render
import urd


def slug_or_404(registry, slug):
    project = registry.get(slug)
    if project is None:
        flask.abort(404)
    return project


def _error_page(exc):
    return render.notice(
        "Something went wrong",
        [str(exc) or type(exc).__name__],
        actions=[("Start over", "/", "get")],
    )


def create_app(registry):
    app = flask.Flask(__name__)
    app.config["REGISTRY"] = registry

    # Loopback binding does not stop a cross-origin form POST: any page open in
    # the same browser can POST /<slug>/refresh or /setup against this port.
    # Sec-Fetch-Site catches that; the Host check catches the same thing from a
    # DNS name that resolves to loopback (rebinding), which Sec-Fetch-Site does
    # not see since the browser still calls that same-origin.
    @app.before_request
    def _same_origin_only():
        if (flask.request.method == "POST"
                and flask.request.headers.get("Sec-Fetch-Site") == "cross-site"):
            flask.abort(403)
        # An IPv6 literal Host is bracketed ("[::1]:8731" or "[::1]"), so a
        # plain split(":")[0] returns "[" and the [::1] allowlist entry could
        # never match; strip the port after the closing bracket instead.
        host = flask.request.host or ""
        host = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            flask.abort(403)

    # Imported here, not at module scope: views_report and views_jobs both
    # import webapp themselves, and a plain top-level import on both sides
    # would need one of the two modules to finish loading before the other
    # starts. Deferring to call time (create_app always runs after every
    # module has finished loading) sidesteps that without either side having
    # to import lazily inside a function body.
    import views_jobs
    import views_report
    import views_wizard
    app.register_blueprint(views_report.bp)
    app.register_blueprint(views_jobs.bp)
    app.register_blueprint(views_wizard.bp)

    @app.errorhandler(404)
    def not_found(_):
        return render.notice(
            "No such project",
            ["That project is not in this volume.",
             "Configured projects: " + (", ".join(p.slug for p in registry.projects())
                                        or "none yet")],
            actions=[("Start over", "/", "get")],
        ), 404

    @app.errorhandler(Exception)
    def server_error(exc):
        # flask.abort(403/404/...) raises an HTTPException, which is an
        # Exception, so a bare `except Exception` catches those too; without
        # this check every abort() in the app (the 403s above, the 404 handled
        # separately below) would be swallowed into a generic 500 instead of
        # reaching Flask's own handling for that status code.
        if isinstance(exc, werkzeug.exceptions.HTTPException):
            return exc
        return _error_page(exc), 500

    # SystemExit is how urd reports an operational failure (a missing token, a
    # bad --status-order), and it is not an Exception, so the handler above
    # never sees it: Flask's own dispatch only catches Exception, and the dev
    # server is threaded, where threading.excepthook silently drops a
    # SystemExit escaping a worker thread with no response and no log line at
    # all. Wrapping the WSGI entry point is the one place that sees every
    # request regardless of which blueprint raised, without needing a second
    # copy of this in every route.
    dispatch = app.wsgi_app

    def wsgi_app(environ, start_response):
        try:
            return dispatch(environ, start_response)
        except SystemExit as exc:
            body = _error_page(exc).encode()
            start_response(
                "500 INTERNAL SERVER ERROR",
                [("Content-Type", "text/html; charset=utf-8"),
                 ("Content-Length", str(len(body)))],
            )
            return [body]

    app.wsgi_app = wsgi_app
    return app


def _has_issues_view(con):
    return con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'issues'"
    ).fetchone()[0] > 0


def report_ready(project, con=None):
    """True once project_page would render the report itself rather than a
    notice about why there isn't one yet.

    Exists so a caller that decorates the page (views_report's controls form)
    can skip that decoration when there is no report under it to decorate.
    """
    if project.con is None or not project.configured():
        return False
    con = project.con.cursor() if con is None else con
    scope = urd.load_scope(con)
    return bool(scope["last_sync_at"]) and _has_issues_view(con)


def project_page(project, tiers=None, con=None):
    """The report, or a notice explaining why there is not one yet.

    Every state here is a page rather than an exception, because these are all
    reachable by someone whose only interface is a browser tab.

    `con` is the connection to render from; the served route passes its own
    request cursor so this reads from the same transaction flags_from wrote
    its flags into. A caller with no cursor to pass gets one made here.
    """
    if project.con is None:
        return render.notice(
            f"{project.slug}: this database could not be opened",
            [project.error or "unknown error",
             "The file is in the volume but DuckDB refused it."],
        )
    con = project.con.cursor() if con is None else con
    if not project.configured():
        return render.notice(
            f"{project.slug}: no scope yet",
            ["This database has no project, site or window configured.",
             "Finish setup and then refresh."],
            actions=[("Set up", "/setup", "get")],
        )
    scope = urd.load_scope(con)
    if not scope["last_sync_at"]:
        lines = [f"Configured for {scope['project']} since {scope['earliest_since']}.",
                 "Nothing has been fetched yet."]
        # Both notice states below offer the same Refresh button the full
        # report does, so a failed or in-progress attempt has to be visible
        # here too: this is the only page a project that has never
        # successfully synced ever shows.
        message = projects.job_message(project)
        if message:
            lines.append(message)
        return render.notice(
            f"{project.slug}: never synced", lines,
            actions=[("Refresh", f"/{project.slug}/refresh", "post")],
        )
    if not _has_issues_view(con):
        # Reachable only by a CLI user who synced without deriving; the Refresh
        # button always does both.
        lines = ["Raw issues are present but the derived tables are not.",
                 "Refresh runs both steps."]
        message = projects.job_message(project)
        if message:
            lines.append(message)
        return render.notice(
            f"{project.slug}: synced but not derived", lines,
            actions=[("Refresh", f"/{project.slug}/refresh", "post")],
        )
    return urd.report_html(con, tiers)
