"""Fixtures shared by the serve test files.

Imported by absolute name, not relative, so these keep working whichever
directory the runner is invoked from.
"""
import os
import tempfile

import urd

SITE = "example.atlassian.net"
EMAIL = "a@b.c"
STATUSES = "To Do,In Progress,Review,Done"


def configured_db():
    """A database with a scope but nothing synced."""
    con = urd.open_db(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    urd.save_scope(con, site=SITE, email=EMAIL, project="PROJ", component="TEAM",
                   earliest_since="2026-01-01", status_order=STATUSES,
                   start_status="In Progress", review_status="Review")
    return con


def registry(volume=None):
    # projects and webapp are imported inside the fixtures, not at module scope.
    # These fixtures are written before either module exists, and a module-scope
    # import would stop this file loading at all, which would make the tests that
    # do not need a registry fail on an import rather than an assertion.
    import projects
    return projects.ProjectRegistry(volume or tempfile.mkdtemp())


def client(reg):
    import webapp
    app = webapp.create_app(reg)
    app.config["TESTING"] = True
    return app.test_client()


def scope(project, **over):
    fields = dict(site=SITE, email=EMAIL, project="PROJ", component="TEAM",
                  earliest_since="2026-01-01", status_order=STATUSES,
                  start_status="In Progress", review_status="Review")
    urd.save_scope(project.con, **{**fields, **over})
    return project


def synced(reg, slug="alpha"):
    """A project that has a scope, a sync timestamp and derived tables."""
    project = scope(reg.add(slug), last_sync_at="2026-08-01T00:00:00Z")
    urd.derive(project.con, STATUSES, "In Progress", "Review")
    return project
