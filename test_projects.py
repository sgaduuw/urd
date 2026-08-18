import os
import pathlib
import tempfile

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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
