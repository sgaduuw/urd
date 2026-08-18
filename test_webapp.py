import os
import pathlib
import re
import tempfile

import render
import test_helpers
import urd

_registry = test_helpers.registry
_client = test_helpers.client


def _configured_db():
    return test_helpers.configured_db()


def test_report_html_returns_what_report_writes():
    """One rendering path, not two. If these ever diverge, the served page and the
    archived file stop being the same report."""
    con = _configured_db()
    urd.derive(con, "To Do,In Progress,Review,Done", "In Progress", "Review")
    path = os.path.join(tempfile.mkdtemp(), "r.html")
    urd.report(con, path)
    with open(path) as fh:
        written = fh.read()
    assert urd.report_html(con) == written


def test_report_html_writes_no_file():
    """`report` defaults to writing report.html in the working directory. The
    server calls this thousands of times, so it must not touch the disk at all."""
    con = _configured_db()
    urd.derive(con, "To Do,In Progress,Review,Done", "In Progress", "Review")
    workdir = tempfile.mkdtemp()
    was = os.getcwd()
    os.chdir(workdir)
    try:
        urd.report_html(con)
        assert os.listdir(workdir) == [], os.listdir(workdir)
    finally:
        os.chdir(was)


def test_derive_creates_the_sprint_view_without_a_sprint_field():
    """VIEWS_SPRINT_ATTRIBUTION reads issue_sprints unconditionally, so a database
    that never synced a Sprint field must still get the view. None of the existing
    305 tests cover this branch: they all seed a Sprint field before calling derive."""
    con = _configured_db()
    urd.derive(con, "To Do,In Progress,Review,Done", "In Progress", "Review")
    assert con.execute("SELECT count(*) FROM issue_sprints").fetchone()[0] == 0


def test_a_notice_is_a_whole_page_not_a_fragment():
    """First-run states are pages a browser lands on, so they need the doctype and
    the stylesheet the report has, or they arrive unstyled."""
    out = render.notice("Nothing synced yet", ["Press Refresh."])
    assert out.startswith("<!doctype html>")
    assert "</html>" in out.strip()[-16:]
    assert "<style>" in out
    assert "Nothing synced yet" in out
    assert "Press Refresh." in out


def test_a_notice_escapes_what_it_is_given():
    out = render.notice("<script>x</script>", ["<b>bold</b>"])
    assert "<script>x</script>" not in out
    assert "&lt;script&gt;" in out
    assert "<b>bold</b>" not in out


def test_a_notice_can_offer_a_post_action():
    """Refresh has to be a POST, so a notice cannot offer it as a plain link."""
    out = render.notice("Never synced", ["Nothing here yet."],
                        actions=[("Refresh", "/alpha/refresh", "post")])
    assert 'method="post"' in out
    assert 'action="/alpha/refresh"' in out
    assert "Refresh" in out


def test_a_notice_fetches_nothing():
    out = render.notice("Title", ["Body"], actions=[("Go", "/x", "get")])
    without_anchors = re.sub(r"<a\b[^>]*>", "", out)
    for pattern in (r"\bsrc\s*=", r"@import", r"url\(", r"\bfetch\s*\("):
        assert not re.search(pattern, without_anchors), pattern


def test_no_projects_redirects_to_setup():
    response = _client(_registry()).get("/")
    assert response.status_code == 302
    assert "/setup" in response.headers["Location"]


def test_a_configured_project_is_redirected_to_from_the_root():
    registry = _registry()
    project = registry.add("alpha")
    urd.save_scope(project.con, site="example.atlassian.net", email="a@b.c",
                   project="PROJ", earliest_since="2026-01-01")
    response = _client(registry).get("/")
    assert response.status_code == 302
    assert "/alpha/" in response.headers["Location"]


def test_an_unknown_slug_is_404_not_500():
    assert _client(_registry()).get("/nope/").status_code == 404


def test_a_project_that_never_synced_gets_a_notice_with_refresh():
    registry = _registry()
    project = registry.add("alpha")
    urd.save_scope(project.con, site="example.atlassian.net", email="a@b.c",
                   project="PROJ", earliest_since="2026-01-01")
    body = _client(registry).get("/alpha/").get_data(as_text=True)
    assert "never synced" in body.lower()
    assert 'action="/alpha/refresh"' in body
    assert "<svg" not in body, "no chart should be drawn from an empty database"


def test_a_broken_database_says_so_rather_than_500ing():
    volume = tempfile.mkdtemp()
    pathlib.Path(volume, "bad.duckdb").write_text("not a database")
    response = _client(_registry(volume)).get("/bad/")
    assert response.status_code == 200
    assert "could not" in response.get_data(as_text=True).lower()


def test_a_synced_project_renders_the_report():
    registry = _registry()
    project = registry.add("alpha")
    urd.save_scope(project.con, site="example.atlassian.net", email="a@b.c",
                   project="PROJ", earliest_since="2026-01-01",
                   status_order="To Do,In Progress,Review,Done",
                   start_status="In Progress", review_status="Review",
                   last_sync_at="2026-08-01T00:00:00Z")
    urd.derive(project.con, "To Do,In Progress,Review,Done", "In Progress", "Review")
    body = _client(registry).get("/alpha/").get_data(as_text=True)
    assert body.startswith("<!doctype html>")
    assert "flow report" in body


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
