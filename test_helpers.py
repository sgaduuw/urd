"""Fixtures shared by the serve test files.

Imported by absolute name, not relative, so these keep working whichever
directory the runner is invoked from.
"""
import os
import tempfile
import urllib.request

import urd


# Every test file that touches a Jira-shaped scope imports this module for its
# fixtures, so the network guard belongs here rather than in just the one file
# that happened to need it first: a future test that forgets to mock
# wizard.validate or urd.Jira would otherwise send a stranger the token of
# whoever cloned this repository, and nothing short of reading its output
# would show that it happened.
def _refuse_network(self, req, *args, **kwargs):
    url = req.full_url if hasattr(req, "full_url") else req
    raise AssertionError(f"tests must not touch the network: {url!r}")


urllib.request.OpenerDirector.open = _refuse_network

# .invalid is reserved by RFC 6761: nobody can ever register a name under it,
# unlike "example.atlassian.net", which is a real, registrable Atlassian site
# slug that happens to look like a placeholder.
SITE = "example.invalid"
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


def client(reg, sec_fetch_site="same-origin"):
    """A test client for the given registry.

    Defaults every request's Sec-Fetch-Site to same-origin, what a real
    browser sends for a page this app served, since webapp.py's POST guard now
    checks that header and the test client does not simulate it on its own.
    Without this default, every existing POST test would have to know that
    header exists just to keep working, for a guard it is not testing.

    Pass sec_fetch_site=None for the one test simulating a browser old enough
    to omit the header entirely; a single call still overrides per request via
    headers=... to exercise a specific value such as cross-site.
    """
    import webapp
    app = webapp.create_app(reg)
    app.config["TESTING"] = True
    test_client = app.test_client()
    if sec_fetch_site is not None:
        test_client.environ_base = {"HTTP_SEC_FETCH_SITE": sec_fetch_site}
    return test_client


def synced(reg, slug="alpha"):
    """A project that has a scope, a sync timestamp and derived tables."""
    project = reg.add(slug)
    urd.save_scope(project.con, site=SITE, email=EMAIL, project="PROJ", component="TEAM",
                   earliest_since="2026-01-01", status_order=STATUSES,
                   start_status="In Progress", review_status="Review",
                   last_sync_at="2026-08-01T00:00:00Z")
    urd.derive(project.con, STATUSES, "In Progress", "Review")
    return project
