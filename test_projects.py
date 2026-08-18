import os
import pathlib
import tempfile
import threading
import time

import projects
import urd


def _volume():
    return tempfile.mkdtemp()


def test_an_empty_volume_has_no_projects():
    assert projects.ProjectRegistry(_volume()).projects() == []


def test_each_database_file_becomes_one_project():
    volume = _volume()
    for slug in ("alpha", "beta"):
        urd.open_db(os.path.join(volume, f"{slug}.duckdb")).close()
    registry = projects.ProjectRegistry(volume)
    assert [p.slug for p in registry.projects()] == ["alpha", "beta"]
    assert registry.get("alpha").slug == "alpha"
    assert registry.get("missing") is None


def test_a_project_without_scope_is_not_configured():
    volume = _volume()
    urd.open_db(os.path.join(volume, "alpha.duckdb")).close()
    assert projects.ProjectRegistry(volume).get("alpha").configured() is False


def test_a_project_with_scope_is_configured():
    volume = _volume()
    con = urd.open_db(os.path.join(volume, "alpha.duckdb"))
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")
    con.close()
    assert projects.ProjectRegistry(volume).get("alpha").configured() is True


def test_a_project_missing_the_project_field_is_not_configured():
    """Isolates the project term: site and earliest_since are both set, so only
    project is missing. Setting fewer than two of the other fields would leave
    this indistinguishable from a mutant that drops a different term."""
    volume = _volume()
    con = urd.open_db(os.path.join(volume, "alpha.duckdb"))
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c",
                   earliest_since="2026-01-01")
    con.close()
    assert projects.ProjectRegistry(volume).get("alpha").configured() is False


def test_a_project_missing_earliest_since_is_not_configured():
    """Isolates the earliest_since term: site and project are both set, so only
    earliest_since is missing."""
    volume = _volume()
    con = urd.open_db(os.path.join(volume, "alpha.duckdb"))
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ")
    con.close()
    assert projects.ProjectRegistry(volume).get("alpha").configured() is False


def test_a_project_missing_the_site_field_is_not_configured():
    """Isolates the site term: project and earliest_since are both set, so only
    site is missing."""
    volume = _volume()
    con = urd.open_db(os.path.join(volume, "alpha.duckdb"))
    urd.save_scope(con, email="a@b.c", project="PROJ", earliest_since="2026-01-01")
    con.close()
    assert projects.ProjectRegistry(volume).get("alpha").configured() is False


def test_a_broken_file_is_listed_rather_than_crashing_startup():
    """One unreadable database must not take out the other projects, or a single
    bad volume entry makes the whole instance unreachable."""
    volume = _volume()
    urd.open_db(os.path.join(volume, "good.duckdb")).close()
    pathlib.Path(volume, "bad.duckdb").write_text("this is not a database")
    registry = projects.ProjectRegistry(volume)
    assert {p.slug for p in registry.projects()} == {"good", "bad"}
    assert registry.get("good").error is None
    assert registry.get("bad").error, "a broken file must carry its reason"
    assert registry.get("bad").con is None


def test_a_request_sees_the_pre_write_snapshot():
    """The measurement the whole concurrency model rests on: a cursor read during
    an open write transaction returns the old data rather than blocking. If this
    fails, Refresh cannot run while pages are served."""
    volume = _volume()
    registry = projects.ProjectRegistry(volume)
    project = registry.add("alpha")
    project.con.execute("CREATE TABLE t (i INTEGER)")
    project.con.execute("INSERT INTO t VALUES (1)")
    project.con.execute("BEGIN")
    project.con.execute("INSERT INTO t VALUES (2)")
    assert project.con.cursor().execute("SELECT count(*) FROM t").fetchone()[0] == 1
    project.con.execute("COMMIT")
    assert project.con.cursor().execute("SELECT count(*) FROM t").fetchone()[0] == 2


def test_add_creates_a_database_and_returns_it():
    registry = projects.ProjectRegistry(_volume())
    project = registry.add("gamma")
    assert project.slug == "gamma"
    assert os.path.exists(project.path)
    assert registry.get("gamma") is project


def test_add_is_idempotent_for_an_existing_slug():
    """A second add() for the same slug must return the same Project rather than
    build a new one: a new Project opens a new connection, and opening a second
    connection to a DuckDB file already held open is exactly what one Project per
    file exists to prevent."""
    registry = projects.ProjectRegistry(_volume())
    assert registry.add("gamma") is registry.add("gamma")


def test_a_slug_must_match_the_allowed_charset():
    """Slugs reach this from a URL and a form field, so anything outside the
    declared charset (lowercase, digits, hyphens) has to be refused rather than
    resolved, whether it is an attempt to escape the volume or just a stray
    character re.match would let slide, such as a trailing newline."""
    registry = projects.ProjectRegistry(_volume())
    for bad in ("../escape", "a/b", "", ".", "with space", "UPPER", "gamma\n"):
        try:
            registry.add(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} was accepted as a slug")


def _configured(registry, slug="alpha"):
    project = registry.add(slug)
    urd.save_scope(project.con, site="example.atlassian.net", email="a@b.c",
                   project="PROJ", earliest_since="2026-01-01",
                   status_order="To Do,In Progress,Review,Done",
                   start_status="In Progress", review_status="Review")
    return project


class _FakeJira:
    def __init__(self, block=None):
        self.block = block

    def search(self, jql):
        if self.block:
            self.block.wait(5)
        yield "PROJ-1", "u1"

    def issue(self, key, fields):
        # "updated" has to be a real timestamp, not the "u1" change marker used
        # above: derive() parses it with datetime.fromisoformat.
        return {"key": key, "fields": {"updated": "2026-01-06T09:00:00.000+0000",
                                       "created": "2026-01-05T09:00:00.000+0000",
                                       "status": {"name": "To Do",
                                                  "statusCategory": {"key": "new"}}}}

    def fields(self):
        return []

    def statuses(self):
        return [{"name": "Done", "statusCategory": {"key": "done"}}]


def _wait_idle(project, seconds=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if project.job.state != "running":
            return project.job.state
        time.sleep(0.01)
    raise AssertionError("job never finished")


def test_a_refresh_syncs_then_derives_and_returns_to_idle():
    registry = projects.ProjectRegistry(_volume())
    project = _configured(registry)
    assert projects.start_refresh(project, jira_factory=lambda scope: _FakeJira()) is True
    assert _wait_idle(project) == "idle", project.job.message
    assert project.con.cursor().execute(
        "SELECT count(*) FROM raw_issues").fetchone()[0] == 1


def test_a_second_refresh_while_one_runs_is_refused():
    """Not queued and not run twice: two clicks would have two threads writing
    the same database."""
    registry = projects.ProjectRegistry(_volume())
    project = _configured(registry)
    gate = threading.Event()
    assert projects.start_refresh(
        project, jira_factory=lambda scope: _FakeJira(block=gate)) is True
    assert projects.start_refresh(
        project, jira_factory=lambda scope: _FakeJira()) is False
    gate.set()
    _wait_idle(project)


def test_a_failure_leaves_the_reason_on_the_job():
    registry = projects.ProjectRegistry(_volume())
    project = _configured(registry)

    def explode(scope):
        raise SystemExit("no API token")

    assert projects.start_refresh(project, jira_factory=explode) is True
    assert _wait_idle(project) == "failed"
    assert "token" in project.job.message


def test_an_unconfigured_project_cannot_be_refreshed():
    registry = projects.ProjectRegistry(_volume())
    project = registry.add("alpha")
    assert projects.start_refresh(project, jira_factory=lambda scope: _FakeJira()) is False
    assert project.job.state == "failed"
    assert "scope" in project.job.message.lower()


def test_pages_can_still_be_read_while_a_refresh_runs():
    """The reason the whole design works. If this fails, Refresh has to take the
    server offline while it runs."""
    registry = projects.ProjectRegistry(_volume())
    project = _configured(registry)
    gate = threading.Event()
    projects.start_refresh(project, jira_factory=lambda scope: _FakeJira(block=gate))
    rows = project.con.cursor().execute("SELECT count(*) FROM raw_issues").fetchone()[0]
    assert rows == 0, "the snapshot should predate the running sync"
    gate.set()
    _wait_idle(project)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
