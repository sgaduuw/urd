"""The Flask app: wiring, and the states that are not a chart.

Routes live in three blueprints rather than here, so three implementers can work
on them without touching one file. Keep that split: collapsing them back into this
module makes every route change serial.
"""
import importlib.util

import flask

import render
import urd


def slug_or_404(registry, slug):
    project = registry.get(slug)
    if project is None:
        flask.abort(404)
    return project


def create_app(registry):
    app = flask.Flask(__name__)
    app.config["REGISTRY"] = registry

    # Each blueprint arrives in its own task, so a module that is not written yet
    # must not stop the app from starting. find_spec first, rather than catching
    # ImportError around the import: a blueprint that exists but imports a bad
    # name would otherwise vanish silently and its routes would 404, which reads
    # as a routing bug rather than the typo it is.
    for module in ("views_report", "views_jobs", "views_wizard"):
        if importlib.util.find_spec(module) is None:
            continue
        app.register_blueprint(importlib.import_module(module).bp)

    @app.errorhandler(404)
    def not_found(_):
        return render.notice(
            "No such project",
            ["That project is not in this volume.",
             "Configured projects: " + (", ".join(p.slug for p in registry.projects())
                                        or "none yet")],
            actions=[("Start over", "/", "get")],
        ), 404

    return app


def project_page(project, tiers=None):
    """The report, or a notice explaining why there is not one yet.

    Every state here is a page rather than an exception, because these are all
    reachable by someone whose only interface is a browser tab.
    """
    if project.con is None:
        return render.notice(
            f"{project.slug}: this database could not be opened",
            [project.error or "unknown error",
             "The file is in the volume but DuckDB refused it."],
        )
    if not project.configured():
        return render.notice(
            f"{project.slug}: no scope yet",
            ["This database has no project, site or window configured.",
             "Finish setup and then refresh."],
            actions=[("Set up", "/setup", "get")],
        )
    scope = urd.load_scope(project.con)
    if not scope["last_sync_at"]:
        return render.notice(
            f"{project.slug}: never synced",
            [f"Configured for {scope['project']} since {scope['earliest_since']}.",
             "Nothing has been fetched yet."],
            actions=[("Refresh", f"/{project.slug}/refresh", "post")],
        )
    if project.con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'issues'"
    ).fetchone()[0] == 0:
        # Reachable only by a CLI user who synced without deriving; the Refresh
        # button always does both.
        return render.notice(
            f"{project.slug}: synced but not derived",
            ["Raw issues are present but the derived tables are not.",
             "Refresh runs both steps."],
            actions=[("Refresh", f"/{project.slug}/refresh", "post")],
        )
    return urd.report_html(project.con, tiers)
