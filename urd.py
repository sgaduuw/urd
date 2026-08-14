#!/usr/bin/env python3
"""urd: mirror one Jira project's ticket history into DuckDB and report on it.

Three verbs. `sync` is the only one that touches the network and the only one
that writes raw_issues. `derive` and `report` are offline and repeatable, which
is what makes changing a metric definition cheap.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import duckdb

KEYCHAIN_SERVICE = "urd"
PAGE_SIZE = 100
TIMEOUT_S = 30
TRANSPORT_ERROR_STATUS = 599  # not a real HTTP status; our marker for a failed connection
RETRY_STATUSES = (429, 500, 502, 503, 504, TRANSPORT_ERROR_STATUS)

DB_DEFAULT = "urd.duckdb"


def token(env=None):
    """Token from URD_TOKEN, else the macOS keychain. The email is not a secret
    and lives in sync_state, so only the token is stored here."""
    env = os.environ if env is None else env
    if env.get("URD_TOKEN"):
        return env["URD_TOKEN"]
    found = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode != 0 or not found.stdout.strip():
        raise SystemExit(
            "no API token. Either export URD_TOKEN, or store one once with:\n"
            f"  security add-generic-password -s {KEYCHAIN_SERVICE} -a <email> -w"
        )
    return found.stdout.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib copies the Authorization header to the new host,
    which would hand the API token to whoever the redirect points at. Jira Cloud's
    API does not redirect, so a redirect means the site is wrong."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"refusing redirect to {newurl}",
            headers,
            fp,
        )


_OPENER = urllib.request.build_opener(_NoRedirect())


class Jira:
    """Read only Jira Cloud client. GET requests only, by construction."""

    def __init__(self, site, email, token, opener=None):
        self.base = f"https://{site}/rest/api/3"
        self.auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._open = opener or self._urlopen

    @staticmethod
    def _urlopen(url, headers):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with _OPENER.open(request, timeout=TIMEOUT_S) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()
        except OSError as err:
            # URLError and TimeoutError are both OSError subclasses, so this one
            # clause covers timeouts, DNS and TLS failures. A dead connection is
            # retryable in exactly the way a 503 is, and mapping it onto a status
            # keeps the retry and the SystemExit contract in one place instead of
            # letting a traceback escape past sync's per-issue error handling.
            return TRANSPORT_ERROR_STATUS, str(err).encode()

    def get(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": f"Basic {self.auth}", "Accept": "application/json"}
        for attempt in (1, 2):
            status, body = self._open(url, headers)
            if status == 200:
                return json.loads(body or b"{}")
            if status in RETRY_STATUSES and attempt == 1:
                # ponytail: one flat retry, no backoff curve, and the (status, bytes)
                # opener contract discards headers so Retry-After cannot be honoured
                # at all. A backfill that trips a rate limit is meant to be resumed,
                # not waited out. Upgrade path: widen the opener contract to
                # (status, headers, bytes) and sleep for Retry-After.
                time.sleep(5)
                continue
            raise SystemExit(f"GET {url} returned {status}: {body[:200]!r}")
        raise SystemExit(f"GET {url} failed twice")

    def search(self, jql):
        """Yield (key, updated) for every issue matching jql, following pages."""
        params = {"jql": jql, "fields": "updated", "maxResults": PAGE_SIZE}
        while True:
            page = self.get("/search/jql", params)
            for issue in page.get("issues", []):
                yield issue["key"], issue["fields"]["updated"]
            nxt = page.get("nextPageToken")
            if page.get("isLast") or not nxt:
                return
            if nxt == params.get("nextPageToken"):
                # The same token twice means the server is not advancing. Failing
                # loudly beats a hung command: the caller consumes this generator
                # with list(), so a silent spin prints nothing at all.
                raise SystemExit(f"search paging stalled on the same page token: {jql}")
            params = dict(params, nextPageToken=nxt)

    def issue(self, key, fields):
        got = self.get(f"/issue/{key}", {"expand": "changelog", "fields": fields})
        log = got.get("changelog") or {}
        histories = log.get("histories", [])
        # Jira truncates an inline changelog. Losing the early history of a
        # long lived ticket would quietly corrupt every duration it appears in.
        while len(histories) < log.get("total", 0):
            page = self.get(
                f"/issue/{key}/changelog",
                {"startAt": len(histories), "maxResults": PAGE_SIZE},
            )
            values = page.get("values", [])
            if not values:
                raise SystemExit(
                    f"{key}: changelog reports {log.get('total')} entries but returned "
                    f"{len(histories)}; refusing to store a partial history"
                )
            histories.extend(values)
        got["changelog"] = {"histories": histories, "total": len(histories)}
        return got

    def fields(self):
        return self.get("/field")

    def statuses(self):
        return self.get("/status")


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
