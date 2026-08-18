import os
import pathlib
import re
import sys
import tempfile

import werkzeug.exceptions

import projects as projects_mod
import render
import test_helpers
import urd
import webapp

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


def test_slug_or_404_returns_the_project_for_a_known_slug():
    registry = _registry()
    project = registry.add("alpha")
    assert webapp.slug_or_404(registry, "alpha") is project


def test_slug_or_404_raises_not_found_for_an_unknown_slug():
    try:
        webapp.slug_or_404(_registry(), "nope")
        raise AssertionError("expected NotFound")
    except werkzeug.exceptions.NotFound:
        pass


def test_the_404_handler_lists_configured_projects():
    """The handler is webapp's own, not Flask's default page, and it names what
    is actually configured so a wrong URL still points somewhere useful."""
    registry = _registry()
    registry.add("alpha")
    response = _client(registry).get("/does-not-exist")
    assert response.status_code == 404
    assert "alpha" in response.get_data(as_text=True)


def test_a_project_with_no_scope_gets_a_notice_to_finish_setup():
    registry = _registry()
    project = registry.add("alpha")
    body = webapp.project_page(project)
    assert "no scope" in body.lower()
    assert 'href="/setup"' in body


def test_a_project_that_never_synced_gets_a_notice_with_refresh():
    registry = _registry()
    project = registry.add("alpha")
    urd.save_scope(project.con, site="example.atlassian.net", email="a@b.c",
                   project="PROJ", earliest_since="2026-01-01")
    body = webapp.project_page(project)
    assert "never synced" in body.lower()
    assert 'action="/alpha/refresh"' in body
    assert "<svg" not in body, "no chart should be drawn from an empty database"


def test_a_broken_database_says_so_rather_than_500ing():
    volume = tempfile.mkdtemp()
    pathlib.Path(volume, "bad.duckdb").write_text("not a database")
    project = _registry(volume).get("bad")
    assert "could not" in webapp.project_page(project).lower()


def test_a_synced_project_renders_the_report():
    project = test_helpers.synced(_registry())
    body = webapp.project_page(project)
    assert body.startswith("<!doctype html>")
    assert "flow report" in body


def test_serve_defaults_to_loopback():
    """An unauthenticated report must not reach a network by default."""
    parser = urd.build_parser()
    args = parser.parse_args(["serve"])
    assert args.host == "127.0.0.1"
    assert args.port == 8731


def test_serve_takes_its_volume_from_the_environment():
    parser = urd.build_parser()
    assert parser.parse_args(["serve", "--volume", "/tmp/x"]).volume == "/tmp/x"


def test_the_environment_seeds_only_the_first_project():
    """A stale compose file must not silently rescope a configured database."""
    volume = tempfile.mkdtemp()
    registry = projects_mod.ProjectRegistry(volume)
    project = registry.add("alpha")
    urd.save_scope(project.con, site="kept.example.net", email="a@b.c",
                   project="KEPT", earliest_since="2026-01-01")
    urd.seed_from_env(registry, {"URD_SITE": "other.example.net",
                                 "URD_PROJECT": "OTHER",
                                 "URD_EMAIL": "b@c.d",
                                 "URD_SINCE": "2020-01-01"})
    assert urd.load_scope(project.con)["project"] == "KEPT"


def test_the_environment_creates_a_first_project_when_the_volume_is_empty():
    registry = projects_mod.ProjectRegistry(tempfile.mkdtemp())
    urd.seed_from_env(registry, {"URD_SITE": "example.atlassian.net",
                                 "URD_PROJECT": "PROJ", "URD_EMAIL": "a@b.c",
                                 "URD_SINCE": "2026-01-01"})
    project = registry.get("proj")
    assert project is not None
    assert urd.load_scope(project.con)["project"] == "PROJ"


def test_an_incomplete_environment_seeds_nothing():
    registry = projects_mod.ProjectRegistry(tempfile.mkdtemp())
    urd.seed_from_env(registry, {"URD_SITE": "example.atlassian.net"})
    assert registry.projects() == []


def test_the_cli_reports_a_held_lock_as_such():
    """A second process gets DuckDB's raw lock error otherwise, which names no
    cause and suggests no fix."""
    import subprocess
    path = os.path.join(tempfile.mkdtemp(), "held.duckdb")
    held = urd.open_db(path)
    held.execute("BEGIN")
    held.execute("CREATE TABLE t (i INTEGER)")
    try:
        done = subprocess.run(
            [sys.executable, "urd.py", "--db", path, "report"],
            capture_output=True, text=True, timeout=60)
    finally:
        held.execute("ROLLBACK")
    combined = done.stdout + done.stderr
    assert done.returncode != 0
    assert "another urd is holding it" in combined, combined


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
