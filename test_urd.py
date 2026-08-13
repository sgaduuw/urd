import os
import tempfile

import urd


def _tmpdb():
    return os.path.join(tempfile.mkdtemp(), "test.duckdb")


def test_schema_is_created_on_open():
    con = urd.open_db(_tmpdb())
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert {"raw_issues", "sync_state", "sync_errors", "fields", "statuses"} <= tables


def test_scope_round_trips():
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", project="PROJ", component="TEAM")
    assert urd.load_scope(con)["project"] == "PROJ"


def test_saving_scope_partially_keeps_the_rest():
    """A later `urd sync --since` must not wipe the site it was told once."""
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", project="PROJ")
    urd.save_scope(con, earliest_since="2026-01-01")
    scope = urd.load_scope(con)
    assert scope["site"] == "example.atlassian.net"
    assert scope["earliest_since"] == "2026-01-01"


def test_scope_starts_empty_rather_than_missing():
    assert urd.load_scope(urd.open_db(_tmpdb()))["site"] is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
