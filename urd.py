#!/usr/bin/env python3
"""urd: mirror one Jira project's ticket history into DuckDB and report on it.

Three verbs. `sync` is the only one that touches the network and the only one
that writes raw_issues. `derive` and `report` are offline and repeatable, which
is what makes changing a metric definition cheap.
"""
import argparse
import sys

import duckdb

DB_DEFAULT = "urd.duckdb"

# One row, upserted. Settings live with the scope because there is no second
# thing to configure, and two tables holding one row each is one table too many.
SCOPE_COLUMNS = (
    "site",
    "email",
    "project",
    "component",
    "status_order",
    "start_status",
    "review_status",
    "earliest_since",
    "last_sync_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_issues (
    key        VARCHAR PRIMARY KEY,
    updated    VARCHAR NOT NULL,   -- the exact string Jira returned, compared verbatim
    fetched_at TIMESTAMP NOT NULL,
    json       VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_state (
    site VARCHAR, email VARCHAR, project VARCHAR, component VARCHAR,
    status_order VARCHAR, start_status VARCHAR, review_status VARCHAR,
    earliest_since VARCHAR, last_sync_at VARCHAR
);
CREATE TABLE IF NOT EXISTS sync_errors (
    -- "at" is quoted: DuckDB classifies it as a type_function keyword, and an
    -- unquoted column of that name fails to parse.
    key VARCHAR PRIMARY KEY, "at" TIMESTAMP, error VARCHAR
);
CREATE TABLE IF NOT EXISTS fields (
    id VARCHAR PRIMARY KEY, name VARCHAR, null_rate DOUBLE
);
CREATE TABLE IF NOT EXISTS statuses (
    name VARCHAR PRIMARY KEY, category VARCHAR
);
"""


def open_db(path=DB_DEFAULT):
    con = duckdb.connect(path)
    con.execute(SCHEMA)
    if con.execute("SELECT count(*) FROM sync_state").fetchone()[0] == 0:
        con.execute(f"INSERT INTO sync_state VALUES ({','.join(['NULL'] * len(SCOPE_COLUMNS))})")
    return con


def save_scope(con, **kwargs):
    """Update only the named columns, leaving the rest as they were."""
    unknown = set(kwargs) - set(SCOPE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown scope keys: {sorted(unknown)}")
    given = {k: v for k, v in kwargs.items() if v is not None}
    if not given:
        return
    assignments = ", ".join(f"{k} = ?" for k in given)
    con.execute(f"UPDATE sync_state SET {assignments}", list(given.values()))


def load_scope(con):
    row = con.execute(f"SELECT {', '.join(SCOPE_COLUMNS)} FROM sync_state").fetchone()
    return dict(zip(SCOPE_COLUMNS, row, strict=True))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="urd", description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DB_DEFAULT)
    sub = parser.add_subparsers(dest="verb", required=True)

    p_sync = sub.add_parser("sync", help="fetch changed issues into raw_issues")
    p_sync.add_argument("--site", help="e.g. example.atlassian.net")
    p_sync.add_argument("--email", help="Atlassian account email; not a secret")
    p_sync.add_argument("--project", help="project key, or a comma separated list")
    p_sync.add_argument("--component", help="component name, or a comma separated list")
    p_sync.add_argument("--since", help="YYYY-MM-DD, bounds on `updated`")

    p_derive = sub.add_parser("derive", help="rebuild the derived tables from raw_issues")
    p_derive.add_argument("--status-order", help="statuses in workflow order, comma separated")
    p_derive.add_argument("--start-status", help="the status at which cycle time starts")
    p_derive.add_argument("--review-status", help="the status reviewers move work out of")

    sub.add_parser("report", help="write report.html from the derived tables")

    p_sql = sub.add_parser("sql", help="run a query against the database")
    p_sql.add_argument("query")

    args = parser.parse_args(argv)
    con = open_db(args.db)

    if args.verb == "sql":
        # ponytail: three lines instead of a dependency on the duckdb CLI. Upgrade
        # path is `duckdb urd.duckdb` if anyone wants a real REPL.
        for row in con.execute(args.query).fetchall():
            print("\t".join("" if v is None else str(v) for v in row))
        return 0

    print(f"{args.verb}: not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
