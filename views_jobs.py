"""Refresh.

POST only: it changes things, and a browser may prefetch a link. Whether it
started, or why it did not, is read back off project.job the next time the
report page renders (views_report.flags_from), not through a query-string
marker or a JSON endpoint nothing polls.
"""
import flask

import projects
import webapp

bp = flask.Blueprint("jobs", __name__)


@bp.post("/<slug>/refresh")
def refresh(slug):
    registry = flask.current_app.config["REGISTRY"]
    project = webapp.slug_or_404(registry, slug)
    projects.start_refresh(project)
    return flask.redirect(f"/{slug}/")
