"""Refresh and status.

Refresh is POST only. It changes things, and a browser may prefetch a link.
Status is JSON so the page can poll it while a sync runs without re-rendering
twenty charts every second.
"""
import flask

import projects
import webapp

bp = flask.Blueprint("jobs", __name__)


@bp.post("/<slug>/refresh")
def refresh(slug):
    registry = flask.current_app.config["REGISTRY"]
    project = webapp.slug_or_404(registry, slug)
    if not projects.start_refresh(project):
        # A refusal rides back in the query string rather than through
        # flask.flash, which needs a secret key and a template to render into,
        # and this app has neither. Task 7's flags_from turns it into a message.
        return flask.redirect(f"/{slug}/?refused=1")
    return flask.redirect(f"/{slug}/")


@bp.get("/<slug>/status")
def status(slug):
    registry = flask.current_app.config["REGISTRY"]
    project = webapp.slug_or_404(registry, slug)
    return flask.jsonify(project.job.as_dict())
