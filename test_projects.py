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


def test_a_slug_cannot_escape_the_volume():
    """Slugs reach this from a URL and a form field, so a traversal attempt has to
    be refused rather than resolved."""
    registry = projects.ProjectRegistry(_volume())
    for bad in ("../escape", "a/b", "", ".", "with space", "UPPER"):
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
