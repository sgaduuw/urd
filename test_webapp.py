import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import time

import duckdb
import werkzeug.exceptions

import projects as projects_mod
import render
import test_helpers
import urd
import webapp


def test_report_html_returns_what_report_writes():
    """One rendering path, not two. If these ever diverge, the served page and the
    archived file stop being the same report."""
    con = test_helpers.configured_db()
    urd.derive(con, "To Do,In Progress,Review,Done", "In Progress", "Review")
    path = os.path.join(tempfile.mkdtemp(), "r.html")
    urd.report(con, path)
    with open(path) as fh:
        written = fh.read()
    assert urd.report_html(con) == written


def test_report_html_writes_no_file():
    """`report` defaults to writing report.html in the working directory. The
    server calls this thousands of times, so it must not touch the disk at all."""
    con = test_helpers.configured_db()
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
    con = test_helpers.configured_db()
    # This test's whole point depends on the fixture having no resolved Sprint
    # field yet; asserting that here means a future change to configured_db()
    # that starts seeding one fails this test loudly instead of leaving it
    # green while silently no longer covering the branch it names.
    assert urd.resolve_field(con, "Sprint") is None
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
    registry = test_helpers.registry()
    project = registry.add("alpha")
    assert webapp.slug_or_404(registry, "alpha") is project


def test_slug_or_404_raises_not_found_for_an_unknown_slug():
    try:
        webapp.slug_or_404(test_helpers.registry(), "nope")
        raise AssertionError("expected NotFound")
    except werkzeug.exceptions.NotFound:
        pass


def test_the_404_handler_lists_configured_projects():
    """The handler is webapp's own, not Flask's default page, and it names what
    is actually configured so a wrong URL still points somewhere useful."""
    registry = test_helpers.registry()
    registry.add("alpha")
    response = test_helpers.client(registry).get("/does-not-exist")
    assert response.status_code == 404
    assert "alpha" in response.get_data(as_text=True)


def test_a_project_with_no_scope_gets_a_notice_to_finish_setup():
    registry = test_helpers.registry()
    project = registry.add("alpha")
    body = webapp.project_page(project)
    assert "no scope" in body.lower()
    assert 'href="/setup"' in body


def test_a_project_that_never_synced_gets_a_notice_with_refresh():
    registry = test_helpers.registry()
    project = registry.add("alpha")
    urd.save_scope(project.con, site="example.atlassian.net", email="a@b.c",
                   project="PROJ", earliest_since="2026-01-01")
    body = webapp.project_page(project)
    assert "never synced" in body.lower()
    assert 'action="/alpha/refresh"' in body
    assert "<svg" not in body, "no chart should be drawn from an empty database"


def test_a_failed_refresh_shows_its_message_on_the_never_synced_notice():
    """The "never synced" notice is the only page a project that has never
    completed a sync ever shows, and it offers the same Refresh button the
    full report does; flags_from's job-message logic only runs on the full
    report path, so a failure here would otherwise be invisible exactly like
    the bug this whole item exists to fix, just for a different first-run
    state instead of a later one."""
    registry = test_helpers.registry()
    project = registry.add("alpha")
    urd.save_scope(project.con, site="example.atlassian.net", email="a@b.c",
                   project="PROJ", earliest_since="2026-01-01")
    project.job.state = "failed"
    project.job.message = "no API token"
    body = webapp.project_page(project)
    assert "never synced" in body.lower()
    assert "no API token" in body


def test_a_broken_database_says_so_rather_than_500ing():
    volume = tempfile.mkdtemp()
    pathlib.Path(volume, "bad.duckdb").write_text("not a database")
    project = test_helpers.registry(volume).get("bad")
    assert "could not" in webapp.project_page(project).lower()


def test_a_synced_project_renders_the_report():
    project = test_helpers.synced(test_helpers.registry())
    body = webapp.project_page(project)
    assert body.startswith("<!doctype html>")
    assert "flow report" in body


def test_a_cross_site_post_is_refused():
    """Loopback binding does not stop a cross-origin form POST; any page open
    in the same browser could otherwise trigger a sync or reconfigure via
    /setup."""
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    response = test_helpers.client(registry).post(
        f"/{project.slug}/refresh", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


def test_an_ordinary_same_origin_post_still_works():
    """start_refresh is mocked, as its sibling in test_views_jobs.py does:
    unmocked, the default jira_factory calls urd.token() for real on a
    background thread, stopped only by the network guard rather than by this
    test never needing a credential in the first place."""
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    original = projects_mod.start_refresh
    projects_mod.start_refresh = lambda project, jira_factory=None: True
    try:
        response = test_helpers.client(registry).post(f"/{project.slug}/refresh")
    finally:
        projects_mod.start_refresh = original
    assert response.status_code == 302


def test_a_foreign_host_header_is_refused():
    """Catches DNS rebinding: a name that resolves to loopback but is not one
    of the hosts a legitimate browser tab for this app would carry."""
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    response = test_helpers.client(registry).get(
        f"/{project.slug}/", headers={"Host": "evil.example"})
    assert response.status_code == 403


def test_an_ipv6_loopback_host_header_is_accepted():
    """A bracketed IPv6 Host ("[::1]:8731") split on the first ":" gives "[",
    which the allowlist could never contain; that dead entry let every IPv6
    loopback request 403 rather than the network-rebinding attempt the check
    exists for."""
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    for host in ("[::1]:8731", "[::1]"):
        response = test_helpers.client(registry).get(
            f"/{project.slug}/", headers={"Host": host})
        assert response.status_code == 200, host


def test_a_system_exit_from_a_route_becomes_a_500_not_a_dropped_connection():
    """SystemExit is not an Exception, so Flask's own error handler never sees
    it; without the WSGI-level guard this crashes silently on the threaded dev
    server (threading.excepthook drops a SystemExit with no response logged)."""
    registry = test_helpers.registry()
    app = webapp.create_app(registry)
    app.config["TESTING"] = True

    @app.get("/boom-system-exit")
    def _boom():
        raise SystemExit("simulated operational failure")

    response = app.test_client().get("/boom-system-exit")
    assert response.status_code == 500
    assert "simulated operational failure" in response.get_data(as_text=True)


def test_an_ordinary_exception_from_a_route_is_a_notice_not_a_bare_500():
    registry = test_helpers.registry()
    app = webapp.create_app(registry)
    app.config["TESTING"] = True

    @app.get("/boom-value-error")
    def _boom():
        raise ValueError("kaboom")

    response = app.test_client().get("/boom-value-error")
    assert response.status_code == 500
    body = response.get_data(as_text=True)
    # Not just "kaboom" appearing somewhere: that alone would also pass for a
    # bare traceback dump. Pin that this is actually render.notice's page.
    assert body.startswith("<!doctype html>")
    assert "<h1>Something went wrong</h1>" in body
    assert "kaboom" in body


class _BulkFakeJira:
    """A second refresh's worth of fake issues, sized to match the
    reproduction in .superpowers/sdd/2026-08-18-urd-web/final-fixes.md (a live
    120-issue refresh): enough writes that a render loop on another thread has
    a real chance of landing mid-sync and mid-derive, not just at the very
    end, where nothing is racing anything."""

    def __init__(self, count=120):
        self.keys = [f"PROJ-{i}" for i in range(1, count + 1)]

    def search(self, jql):
        yield from ((key, "u1") for key in self.keys)

    def issue(self, key, fields):
        # "updated"/"created" have to be real timestamps, not the "u1" change
        # marker above: derive() parses them with datetime.fromisoformat.
        return {"key": key, "fields": {
            "updated": "2026-01-06T09:00:00.000+0000",
            "created": "2026-01-05T09:00:00.000+0000",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
        }}

    def fields(self):
        return []

    def statuses(self):
        return [{"name": "Done", "statusCategory": {"key": "done"}}]


def test_pages_can_still_be_read_while_a_refresh_runs():
    """The reason the whole design works, and the defect a whole-branch review
    found that thirteen rounds of task-scoped review did not: every request
    used to read `found.con` directly, and DuckDBPyConnection.execute()
    returns the connection itself rather than a separate result object, so a
    chart's `cursor.description`/`fetchall()` could read state the sync
    thread's next execute() had already overwritten (ValueError: zip()
    argument 2 is shorter than argument 1).

    Drives the served path itself, a GET through the test client, not a
    hand-made cursor: a hand-made cursor is exactly what the design spec's own
    concurrency test called, which is why it passed on code that could not
    actually serve a page during a sync. See test_projects.py for the
    connection-level property this builds on.
    """
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)  # already has one derive behind it
    client = test_helpers.client(registry)

    assert projects_mod.start_refresh(
        project, jira_factory=lambda scope: _BulkFakeJira()) is True

    outcomes = []
    saw_running = []
    stop = threading.Event()

    def render_loop():
        while not stop.is_set():
            # Recorded before the request, not after: job.state can flip to
            # "idle" while the render itself is in flight, and the point is
            # to know a render was attempted while a sync was genuinely
            # running, not merely that the job hadn't finished yet when this
            # loop last checked.
            saw_running.append(project.job.state == "running")
            try:
                outcomes.append(client.get(f"/{project.slug}/").status_code)
            except Exception as exc:      # noqa: BLE001 - recording, not handling
                # Without a registered error handler, Flask's test client
                # re-raises an unhandled view exception directly rather than
                # turning it into a response; caught here so a crash mid-loop
                # is recorded as a failed outcome instead of silently ending
                # the thread with nothing in `outcomes` at all.
                outcomes.append(exc)

    renderer = threading.Thread(target=render_loop, daemon=True)
    renderer.start()
    deadline = time.time() + 10
    while project.job.state == "running" and time.time() < deadline:
        time.sleep(0.002)
    stop.set()
    renderer.join(5)

    assert project.job.state == "idle", project.job.message
    assert outcomes, "the render loop never ran"
    # Without this, a fast machine that finishes the refresh before the loop
    # gets going leaves a green test that raced nothing: every render would
    # have hit an already-idle project, which this fix is not needed for.
    assert any(saw_running), "no render overlapped a running refresh; this proves nothing"
    failures = [o for o in outcomes if o != 200]
    assert not failures, (
        f"{len(failures)} of {len(outcomes)} renders failed: {failures[:5]}")


def test_two_concurrent_renders_on_one_project_do_not_conflict():
    """Restoring _RENDER_LOCK's actual job: the per-request cursor and
    transaction stop a render from reading state a concurrent *sync*
    overwrote (corruption), but do nothing about two renders racing each
    other. flags_from writes on every render regardless of whether anything
    actually changed (urd.set_report_window does DELETE FROM report_window
    on a one-row table even when the value is unchanged), so two overlapping
    request transactions on that row are a deterministic write-write
    conflict, not the corruption the cursor guards against. Reproduced live
    against the real app, two threads, an otherwise idle server: about 97% of
    3750 requests came back 500 with "Conflict on tuple deletion". Reachable
    by one person on a laptop: a reload while a ~194ms render is in flight, a
    double-clicked Apply, or the same project open in two tabs.

    No refresh needed here at all: the conflict is between two renders, not
    between a render and a sync, which is what makes this a different defect
    from the one test_pages_can_still_be_read_while_a_refresh_runs pins.
    """
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    app = webapp.create_app(registry)
    app.config["TESTING"] = True

    per_thread = 60
    outcomes = []

    def hammer():
        client = app.test_client()
        for _ in range(per_thread):
            outcomes.append(client.get(f"/{project.slug}/").status_code)

    threads = [threading.Thread(target=hammer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(outcomes) == 2 * per_thread
    failures = [o for o in outcomes if o != 200]
    assert not failures, (
        f"{len(failures)} of {len(outcomes)} renders failed with a non-200")


def test_project_page_with_no_cursor_survives_a_concurrent_refresh():
    """The near-miss that almost reopened the Critical during verification:
    calling webapp.project_page(project) directly, with no con, used to fall
    back to project.con itself, the same shared connection the sync thread
    writes to, so this call pattern raced exactly like the unfixed served
    route did (measured: 2 succeeded, 16 raised the same zip() error). The
    only production caller that omits con (views_report.py's early return for
    an unconfigured/unopenable project) returns a notice with no chart SQL,
    so nothing was actually exposed, but the default was still the dangerous
    one: safe only because every current caller happens to be right, not
    because the function can't be called wrong. project_page now makes its
    own cursor when none is given, so this is safe by construction instead of
    by convention.
    """
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)  # already has one derive behind it

    assert projects_mod.start_refresh(
        project, jira_factory=lambda scope: _BulkFakeJira()) is True

    outcomes = []
    stop = threading.Event()

    def render_loop():
        while not stop.is_set():
            try:
                webapp.project_page(project)
                outcomes.append(True)
            except Exception as exc:      # noqa: BLE001 - recording, not handling
                outcomes.append(exc)

    renderer = threading.Thread(target=render_loop, daemon=True)
    renderer.start()
    deadline = time.time() + 10
    while project.job.state == "running" and time.time() < deadline:
        time.sleep(0.002)
    stop.set()
    renderer.join(5)

    assert project.job.state == "idle", project.job.message
    assert outcomes, "the render loop never ran"
    failures = [o for o in outcomes if o is not True]
    assert not failures, f"{len(failures)} of {len(outcomes)} renders failed: {failures[:5]}"


def test_pages_render_throughout_a_derive():
    """derive_issues drops and recreates the `issues` view without the
    `abandoned` column, which derive() only adds afterward, then recreates the
    view again; fifteen chart queries filter on that column. A reader on
    another cursor in the gap between those two points would see a view with
    no such column. Wrapping the whole rebuild in one BEGIN/COMMIT is what
    makes a cursor see either the complete old schema or the complete new
    one, never the gap.

    Re-derives repeatedly against the same raw_issues, rather than a real sync
    between passes, so the loop is fast enough to hit the gap (if there were
    one) many times inside this test's own timeout.
    """
    con = urd.open_db(os.path.join(tempfile.mkdtemp(), "derive-race.duckdb"))
    urd.save_scope(con, site=test_helpers.SITE, email=test_helpers.EMAIL,
                   project="PROJ", earliest_since="2026-01-01")
    issue_json = json.dumps({
        "fields": {
            "summary": "x", "issuetype": {"name": "Task"},
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "created": "2026-01-01T00:00:00.000+0000",
            "updated": "2026-01-01T00:00:00.000+0000",
        },
        "changelog": {"histories": []},
    })
    rows = [(f"PROJ-{i}", "u1", urd._now(), issue_json) for i in range(1, 301)]
    con.executemany("INSERT INTO raw_issues VALUES (?, ?, ?, ?)", rows)
    urd.derive(con, "To Do,In Progress,Review,Done", "In Progress", "Review")

    outcomes = []
    stop = threading.Event()

    def rederive_loop():
        while not stop.is_set():
            urd.derive(con, "To Do,In Progress,Review,Done", "In Progress", "Review")

    def read_loop():
        reader = con.cursor()
        while not stop.is_set():
            try:
                urd.report_html(reader)
                outcomes.append(True)
            except Exception as exc:      # noqa: BLE001 - recording, not handling
                outcomes.append(exc)

    writer = threading.Thread(target=rederive_loop, daemon=True)
    reader_thread = threading.Thread(target=read_loop, daemon=True)
    writer.start()
    reader_thread.start()
    time.sleep(1.5)
    stop.set()
    writer.join(5)
    reader_thread.join(5)

    failures = [o for o in outcomes if o is not True]
    assert outcomes, "the reader never ran"
    assert not failures, f"{len(failures)} of {len(outcomes)} reads raised: {failures[:3]}"


def test_serve_defaults_to_loopback():
    """An unauthenticated report must not reach a network by default."""
    parser = urd.build_parser()
    args = parser.parse_args(["serve"])
    assert args.host == "127.0.0.1"
    assert args.port == 8731


def test_serve_volume_flag_wins_over_the_environment():
    """URD_VOLUME is set to a different path than the flag, so a pass here proves
    the flag overrides the environment rather than merely matching a default that
    happens to equal the flag's value.

    The default is captured once, inside build_parser (the os.environ.get call
    runs at add_argument time, not at parse_args time), so URD_VOLUME has to be
    set before build_parser() runs, or this test would pass for the wrong reason."""
    was = os.environ.get("URD_VOLUME")
    os.environ["URD_VOLUME"] = "/tmp/from-env"
    try:
        parser = urd.build_parser()
    finally:
        if was is None:
            os.environ.pop("URD_VOLUME", None)
        else:
            os.environ["URD_VOLUME"] = was
    assert parser.parse_args(["serve", "--volume", "/tmp/x"]).volume == "/tmp/x"


def test_serve_volume_defaults_from_the_environment():
    """Same ordering constraint as above: URD_VOLUME must be set before
    build_parser() runs, since that call is what reads it."""
    was = os.environ.get("URD_VOLUME")
    os.environ["URD_VOLUME"] = "/tmp/from-env"
    try:
        parser = urd.build_parser()
    finally:
        if was is None:
            os.environ.pop("URD_VOLUME", None)
        else:
            os.environ["URD_VOLUME"] = was
    assert parser.parse_args(["serve"]).volume == "/tmp/from-env"


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


def test_seed_from_env_missing_site_seeds_nothing():
    """Isolates the site term: project, email and since are all set, so only site
    is missing. Setting fewer than three of the other fields would leave this
    indistinguishable from a mutant that drops a different term."""
    registry = projects_mod.ProjectRegistry(tempfile.mkdtemp())
    urd.seed_from_env(registry, {"URD_PROJECT": "PROJ", "URD_EMAIL": "a@b.c",
                                 "URD_SINCE": "2026-01-01"})
    assert registry.projects() == []


def test_seed_from_env_missing_project_seeds_nothing():
    """Isolates the project term: site, email and since are all set, so only
    project is missing."""
    registry = projects_mod.ProjectRegistry(tempfile.mkdtemp())
    urd.seed_from_env(registry, {"URD_SITE": "example.atlassian.net",
                                 "URD_EMAIL": "a@b.c", "URD_SINCE": "2026-01-01"})
    assert registry.projects() == []


def test_seed_from_env_missing_email_seeds_nothing():
    """Isolates the email term: site, project and since are all set, so only
    email is missing."""
    registry = projects_mod.ProjectRegistry(tempfile.mkdtemp())
    urd.seed_from_env(registry, {"URD_SITE": "example.atlassian.net",
                                 "URD_PROJECT": "PROJ", "URD_SINCE": "2026-01-01"})
    assert registry.projects() == []


def test_seed_from_env_missing_since_seeds_nothing():
    """Isolates the since term: site, project and email are all set, so only
    since is missing."""
    registry = projects_mod.ProjectRegistry(tempfile.mkdtemp())
    urd.seed_from_env(registry, {"URD_SITE": "example.atlassian.net",
                                 "URD_PROJECT": "PROJ", "URD_EMAIL": "a@b.c"})
    assert registry.projects() == []


def test_a_punctuation_only_project_is_skipped_not_crashed():
    """URD_PROJECT="," is non-empty, so it survives the emptiness check, but
    project.split(",")[0] reduces it to "", which registry.add refuses with
    ValueError. That is exactly the class of stale or hand-edited compose file
    this function's own docstring anticipates: the server must still start,
    landing on /setup, rather than crash-loop on a traceback."""
    registry = projects_mod.ProjectRegistry(tempfile.mkdtemp())
    urd.seed_from_env(registry, {"URD_SITE": "example.atlassian.net",
                                 "URD_PROJECT": ",", "URD_EMAIL": "a@b.c",
                                 "URD_SINCE": "2026-01-01"})
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


def test_a_bad_path_is_not_reported_as_a_held_lock():
    """IOException is DuckDB's general filesystem error, not lock-specific: a
    typo'd path raises it too. This is the load-bearing test of the pair: without
    it, a future edit could widen the friendly branch back to every IOException
    and nothing here would notice."""
    import subprocess
    bad_path = os.path.join(tempfile.mkdtemp(), "no-such-dir", "x.duckdb")
    done = subprocess.run(
        [sys.executable, "urd.py", "--db", bad_path, "report"],
        capture_output=True, text=True, timeout=60)
    combined = done.stdout + done.stderr
    assert done.returncode != 0
    assert "another urd is holding it" not in combined, combined
    assert "No such file" in combined, combined


def test_a_block_related_ioexception_does_not_collide_with_the_lock_phrase():
    """Pins the discriminator against the substring it must not match: "block"
    contains "lock", and DuckDB's storage is organised in blocks, so a corrupted
    or short block read is a plausible IOException that must not be misdiagnosed
    as a running server. Constructed directly rather than by corrupting a real
    database, since the point is the string match, not reproducing storage
    corruption."""
    real_open_db = urd.open_db

    def fake_open_db(path):
        raise duckdb.IOException("IO Error: could not read block 3 of file, short read")

    urd.open_db = fake_open_db
    try:
        try:
            urd.main(["--db", "irrelevant.duckdb", "report"])
            raise AssertionError("expected SystemExit")
        except SystemExit as exc:
            message = str(exc)
    finally:
        urd.open_db = real_open_db
    assert "another urd is holding it" not in message, message
    assert "could not read block" in message, message


def test_project_slug_lowercases_and_takes_the_first_key():
    assert urd.project_slug("PROJ") == "proj"
    assert urd.project_slug("PROJ,OTHER") == "proj"
    assert urd.project_slug("  PROJ  ") == "proj"


def test_project_slug_replaces_what_the_charset_refuses():
    """The registry accepts [a-z0-9][a-z0-9-]* only, so anything else has to
    become a hyphen rather than reaching the filesystem."""
    assert urd.project_slug("MY PROJ") == "my-proj"
    assert urd.project_slug("A_B") == "a-b"


def test_project_slug_can_return_something_the_registry_rejects():
    """Deliberately not validated here: seed_from_env already catches the
    ValueError and the wizard shows it on the page, and two validators for one
    rule is how they drift."""
    assert urd.project_slug(",") == ""
    assert urd.project_slug("!!!") == "---"


def test_seed_from_env_and_the_wizard_derive_the_same_slug():
    """One function, so the environment path and the form cannot disagree about
    what a project key becomes on disk. This is the assertion that keeps them
    together; without it the two could drift silently."""
    volume = tempfile.mkdtemp()
    registry = projects_mod.ProjectRegistry(volume)
    urd.seed_from_env(registry, {"URD_SITE": "example.invalid",
                                 "URD_PROJECT": "PROJ,OTHER",
                                 "URD_EMAIL": "a@b.c",
                                 "URD_SINCE": "2026-01-01"})
    seeded = [p.slug for p in registry.projects()]
    assert seeded == [urd.project_slug("PROJ,OTHER")], seeded


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
