#!/usr/bin/env python3
"""urd: mirror one Jira project's ticket history into DuckDB and report on it.

Three verbs. `sync` is the only one that touches the network and the only one
that writes raw_issues. `derive` and `report` are offline and repeatable, which
is what makes changing a metric definition cheap.
"""
import argparse
import base64
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import duckdb

import charts as chart_specs
import render

KEYCHAIN_SERVICE = "urd"
PAGE_SIZE = 100
TIMEOUT_S = 30
TRANSPORT_ERROR_STATUS = 599  # unassigned by IANA, used here as our marker for a failed connection
RETRY_STATUSES = (429, 500, 502, 503, 504, TRANSPORT_ERROR_STATUS)

DB_DEFAULT = "urd.duckdb"


def _now():
    """Naive UTC, to match the naive TIMESTAMP columns. Same reason as _ts: an
    aware value gets shifted to the machine's local wall time and the zone
    dropped, so the same database reads differently on a laptop and in CI."""
    return datetime.now(timezone.utc).replace(tzinfo=None)  # noqa: UP017


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
        raise SystemExit(f"refusing redirect to {newurl}; check --site")


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
        except (OSError, http.client.HTTPException) as err:
            # URLError and TimeoutError are both OSError subclasses, so this one
            # clause covers timeouts, DNS and TLS failures, and malformed or
            # truncated responses. A dead connection is retryable in exactly the
            # way a 503 is, and mapping it onto a status keeps the retry and the
            # SystemExit contract in one place instead of letting a traceback
            # escape past sync's per-issue error handling.
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
        if not isinstance(got, dict):
            raise SystemExit(f"{key}: expected a JSON object, got {type(got).__name__}")
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
    "fetched_fields",
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
    earliest_since VARCHAR, last_sync_at VARCHAR, fetched_fields VARCHAR
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
    # CREATE TABLE IF NOT EXISTS leaves an older database on its original columns,
    # so a column added later has to be alter'd in or every read of it fails on a
    # database that predates it. Idempotent, and cheap enough to run every open.
    for column in SCOPE_COLUMNS:
        con.execute(f"ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS {column} VARCHAR")
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


# worklog rides along unused: it is free on a request already being made, and a
# future logged-time chart then needs no refetch.
# ponytail: fetch it, derive nothing. Upgrade path is a worklogs table in derive.
BASE_FIELDS = (
    "summary", "issuetype", "status", "statuscategorychangedate", "priority",
    "labels", "components", "assignee", "reporter", "created", "updated",
    "resolutiondate", "resolution", "parent", "fixVersions", "timespent",
    "timeoriginalestimate", "worklog",
)

# Custom fields are per instance, so they are resolved by name at sync time and
# appended. They are NOT optional extras: derive reads story points and sprint
# memberships out of the fetched JSON, so a field missing here is a field that
# is permanently NULL downstream, with no error anywhere. That is not
# hypothetical. The first live run reported "Story Points: 100% empty" and zero
# sprint memberships against a project that had both, because this list held
# only built-in names, and five of the sixteen charts were dead by construction.
CUSTOM_FIELD_NAMES = ("Story Points", "Sprint")


def fetch_fields(con):
    """The field list for an issue GET: the built-ins plus whatever this instance
    calls the custom fields derive needs. Sorted so the string is stable, since a
    change in it is what triggers a refetch."""
    resolved = [resolve_field(con, name) for name in CUSTOM_FIELD_NAMES]
    return ",".join(list(BASE_FIELDS) + sorted(f for f in resolved if f))


def keys_to_fetch(stored, remote):
    """One rule, both directions: fetch a key we lack, or whose `updated` moved.

    This is why extending --since backwards costs only the issues not already
    held, with no window arithmetic to get wrong.
    """
    return [key for key, updated in remote if stored.get(key) != updated]


def build_jql(project, component, since):
    """Build a JQL query string. Both project and component are interpolated raw,
    so a stray double quote can break the query syntax. A comma in a real
    component name becomes a separator; if both fragments name real components,
    the query widens silently. Usually a fragment is not a real component and
    Jira returns a 400 to abort loudly. This is acceptable only because the sole
    input is the operator's own command line and the client is GET-only."""
    clauses = [f"project in ({project})"]
    if component:
        quoted = ",".join(f'"{c.strip()}"' for c in component.split(","))
        clauses.append(f"component in ({quoted})")
    clauses.append(f'updated >= "{since}"')
    return " AND ".join(clauses) + " ORDER BY key"


def sync(con, jira):
    scope = load_scope(con)
    if not scope["project"] or not scope["earliest_since"]:
        raise SystemExit("first run needs --site, --email, --project and --since")

    # Before anything is fetched, not after: the field list is built from the
    # `fields` table, so resolving it afterwards left a first run asking for the
    # built-ins only, forever.
    _refresh_lookups(con, jira)
    fields = fetch_fields(con)

    jql = build_jql(scope["project"], scope["component"], scope["earliest_since"])
    remote = list(jira.search(jql))
    # A key that has left the scope can never be retried, so its error would be
    # reported forever. `remote` is the authoritative list of what is in scope.
    con.execute("DELETE FROM sync_errors WHERE NOT list_contains(?::VARCHAR[], key)",
                [[key for key, _ in remote]])
    stored = dict(con.execute("SELECT key, updated FROM raw_issues").fetchall())
    if scope["fetched_fields"] != fields:
        # `updated` has not moved, so the usual rule would fetch nothing and every
        # cached issue would stay permanently missing the newly requested field.
        # Asking for more data has to invalidate the cache, or the bug this
        # replaces comes straight back the next time a field is added.
        if stored:
            # Also fires on a database predating this column, which is exactly when
            # the explanation is most wanted: otherwise an upgrade silently reports
            # "751 in scope, 751 to fetch" on a database that already holds all 751.
            print("field set changed, refetching everything")
        stored = {}
    wanted = keys_to_fetch(stored, remote)
    print(f"{len(remote)} in scope, {len(wanted)} to fetch")

    for n, key in enumerate(wanted, start=1):
        # ponytail: one HTTP request per changed issue. Daily delta is small and
        # a first backfill is a few hundred requests paid once. Upgrade path is
        # batching via the search endpoint's fields expansion.
        try:
            issue = jira.issue(key, fields)
            updated = issue["fields"]["updated"]
        except (SystemExit, KeyError, TypeError) as err:
            con.execute(
                'INSERT INTO sync_errors VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE '
                'SET "at" = excluded."at", error = excluded.error',
                [key, _now(), str(err)],
            )
            print(f"  {key}: {err}", file=sys.stderr)
            continue
        con.execute(
            "INSERT INTO raw_issues VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE "
            "SET updated = excluded.updated, fetched_at = excluded.fetched_at, "
            "json = excluded.json",
            [key, updated, _now(), json.dumps(issue)],
        )
        con.execute("DELETE FROM sync_errors WHERE key = ?", [key])
        if n % 50 == 0:
            print(f"  {n}/{len(wanted)}")

    save_scope(con, fetched_fields=fields, last_sync_at=_now().isoformat(timespec="seconds") + "Z")
    errors = con.execute("SELECT count(*) FROM sync_errors").fetchone()[0]
    print(f"synced. {errors} error(s) outstanding" if errors else "synced, no errors")
    return 0


def _refresh_lookups(con, jira):
    """Field ids and the status name to category map, both resolved by name so
    no instance specific id is ever compiled in."""
    con.execute("DELETE FROM fields")
    for field in jira.fields():
        con.execute("INSERT INTO fields VALUES (?, ?, NULL) ON CONFLICT (id) DO NOTHING",
                    [field["id"], field.get("name")])
    con.execute("DELETE FROM statuses")
    for status in jira.statuses():
        con.execute(
            "INSERT INTO statuses VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
            [status["name"], (status.get("statusCategory") or {}).get("key")],
        )


ISSUES_SCHEMA = """
CREATE OR REPLACE TABLE issues (
    key VARCHAR PRIMARY KEY, project VARCHAR, type VARCHAR, status VARCHAR,
    status_category VARCHAR, assignee_id VARCHAR, reporter_id VARCHAR,
    created TIMESTAMP, updated TIMESTAMP, resolved TIMESTAMP,
    story_points DOUBLE, timespent_s BIGINT, parent VARCHAR,
    fix_versions VARCHAR[], labels VARCHAR[], components VARCHAR[]
);
CREATE TABLE IF NOT EXISTS people (account_id VARCHAR PRIMARY KEY, display_name VARCHAR);
"""

CHANGES_SCHEMA = """
CREATE OR REPLACE TABLE changes (
    key VARCHAR, ts TIMESTAMP, field VARCHAR,
    from_id VARCHAR, from_str VARCHAR, to_id VARCHAR, to_str VARCHAR,
    author_id VARCHAR, history_id BIGINT
);
"""

SPRINTS_SCHEMA = """
CREATE OR REPLACE TABLE issue_sprints (
    key VARCHAR, sprint_id BIGINT, sprint_name VARCHAR, state VARCHAR,
    start TIMESTAMP, "end" TIMESTAMP, ordinal INTEGER
);
"""

# One general table beats two specific ones: status transitions are a view over
# it, and assignee history then comes free, which is what the handoff matrix
# needs.
VIEWS_CHANGES = """
CREATE OR REPLACE VIEW transitions AS
SELECT key, ts, from_str AS from_status, to_str AS to_status, author_id, history_id
FROM changes WHERE field = 'status';

CREATE OR REPLACE VIEW status_durations AS
WITH first_move AS (
    SELECT key, ts AS first_ts, from_status AS initial_status FROM (
        SELECT key, ts, from_status,
               row_number() OVER (PARTITION BY key ORDER BY ts, history_id) AS rn
        FROM transitions
    ) WHERE rn = 1
),
before_first AS (        -- creation up to the first transition, in the status it started in
    SELECT i.key, f.initial_status AS status, i.created AS entered, f.first_ts AS left_at
    FROM issues i JOIN first_move f USING (key)
    WHERE f.first_ts > i.created
),
after_each AS (          -- each transition up to the next, the last one still open
    SELECT t.key, t.to_status AS status,
           GREATEST(t.ts, i.created) AS entered,
           GREATEST(COALESCE(LEAD(t.ts) OVER (PARTITION BY t.key ORDER BY t.ts, t.history_id),
                             now() AT TIME ZONE 'UTC'), i.created) AS left_at
    FROM transitions t JOIN issues i ON i.key = t.key
),
never_moved AS (         -- no status changes at all: one span, creation to now
    SELECT key, status, created AS entered, now() AT TIME ZONE 'UTC' AS left_at
    FROM issues WHERE key NOT IN (SELECT key FROM transitions)
)
SELECT * FROM before_first
UNION ALL SELECT * FROM after_each
UNION ALL SELECT * FROM never_moved;
"""


def resolve_field(con, name):
    """Custom field ids differ per instance, so they are looked up by name and
    never compiled in. Returns None when the instance has no such field.

    ponytail: ORDER BY id LIMIT 1 orders string ids, so with two fields named
    "Story Points", customfield_10999 beats customfield_20001. The wrong pick is
    silent except for a null rate near 1.0. If both name a DOUBLE field, the pick
    is arbitrary; if both name incompatible types (e.g. text), the wrong one raises
    ConversionException on derive. Upgrade path: if duplicates ever occur, fail
    loudly per instance-specific logic or stash both and fail the derive.
    """
    row = con.execute("SELECT id FROM fields WHERE name = ? ORDER BY id LIMIT 1", [name]).fetchone()
    return row[0] if row else None


def _ts(value):
    """Jira sends '2026-01-05T09:00:00.000+0000' on issue fields and '...Z' on the
    Sprint field. Python 3.11's fromisoformat parses both, and this project targets
    3.11 (ruff.toml). Returns naive UTC, because the TIMESTAMP columns are naive:
    an aware value gets shifted to the machine's local wall time with the zone
    dropped, so the same database would read differently on a laptop and in CI,
    and any span crossing a DST change would come out an hour wrong.

    On 3.10 or earlier this raises ValueError on both shapes, which is the correct
    loud failure for an unsupported interpreter.

    ponytail: assumes Jira always sends an offset, which it does. A naive input
    would be treated as local time by astimezone. Upgrade path if that changes:
    reject a value with no offset rather than guessing one.
    """
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)  # noqa: UP017


def _names(items, attr="name"):
    return [i[attr] for i in (items or []) if i.get(attr)]


def derive_issues(con):
    con.execute(ISSUES_SCHEMA)
    points_field = resolve_field(con, "Story Points")
    rows, people, missing_points = [], {}, 0

    for key, raw in con.execute("SELECT key, json FROM raw_issues ORDER BY key").fetchall():
        f = json.loads(raw)["fields"]
        for who in (f.get("assignee"), f.get("reporter")):
            if who:
                acct = who.get("accountId")
                if acct:
                    people[acct] = who.get("displayName")
        points = f.get(points_field) if points_field else None
        missing_points += points is None
        rows.append(
            [
                key, key.split("-")[0], (f.get("issuetype") or {}).get("name"),
                (f.get("status") or {}).get("name"),
                ((f.get("status") or {}).get("statusCategory") or {}).get("key"),
                (f.get("assignee") or {}).get("accountId"),
                (f.get("reporter") or {}).get("accountId"),
                _ts(f.get("created")), _ts(f.get("updated")), _ts(f.get("resolutiondate")),
                points, f.get("timespent"), (f.get("parent") or {}).get("key"),
                _names(f.get("fixVersions")), f.get("labels") or [],
                _names(f.get("components")),
            ]
        )

    if rows:
        con.executemany(f"INSERT INTO issues VALUES ({','.join(['?'] * 16)})", rows)
    if people:
        # ponytail: last-writer wins on rename. For anyone who is both assignee and
        # changelog author, the author name wins because derive_changes runs after
        # derive_issues. Ordering is by issue key not fetched_at, so a stale name
        # can also win if an old issue mentions a person. Upgrade path: prefer the
        # row with newest fetched_at in both inserts.
        con.executemany(
            "INSERT INTO people VALUES (?, ?) "
            "ON CONFLICT (account_id) DO UPDATE SET display_name = excluded.display_name",
            list(people.items()))
    if points_field and rows:
        con.execute("UPDATE fields SET null_rate = ? WHERE id = ?",
                    [missing_points / len(rows), points_field])
    return len(rows)


def derive_sprints(con):
    """The Sprint field holds every sprint an issue has belonged to, in order, so
    carry-over is ordinal > 1. The changelog's Sprint items carry comma joined
    names rather than ids and are not needed.

    ponytail: assumes each array element is a dict with .get() methods. Legacy
    Greenhopper serialised Sprint values as strings, which would raise AttributeError.
    Jira Cloud returns dicts, so this is a house convention gap rather than a bug.
    Upgrade path: detect and reject string elements, or stash both and fail the derive.
    """
    con.execute(SPRINTS_SCHEMA)
    sprint_field = resolve_field(con, "Sprint")
    if not sprint_field:
        return 0
    rows = []
    for key, raw in con.execute("SELECT key, json FROM raw_issues ORDER BY key").fetchall():
        sprints = json.loads(raw)["fields"].get(sprint_field) or []
        for ordinal, sprint in enumerate(sprints, start=1):
            rows.append(
                [
                    key, sprint.get("id"), sprint.get("name"), sprint.get("state"),
                    _ts(sprint.get("startDate")), _ts(sprint.get("endDate")), ordinal,
                ]
            )
    if rows:
        con.executemany(f"INSERT INTO issue_sprints VALUES ({','.join(['?'] * 7)})", rows)
    return len(rows)


def derive_changes(con):
    con.execute(CHANGES_SCHEMA)
    rows, people = [], {}
    for key, raw in con.execute("SELECT key, json FROM raw_issues ORDER BY key").fetchall():
        issue = json.loads(raw)
        for history in (issue.get("changelog") or {}).get("histories", []):
            author = history.get("author") or {}
            if author.get("accountId"):
                people[author["accountId"]] = author.get("displayName")
            history_id_str = history.get("id")
            try:
                history_id = int(history_id_str) if history_id_str else None
            except (ValueError, TypeError):
                history_id = None
            for item in history.get("items", []):
                rows.append(
                    [
                        key, _ts(history.get("created")), item.get("field"),
                        item.get("from"), item.get("fromString"),
                        item.get("to"), item.get("toString"),
                        author.get("accountId"), history_id,
                    ]
                )
    if rows:
        con.executemany(f"INSERT INTO changes VALUES ({','.join(['?'] * 9)})", rows)
    if people:
        con.executemany(
            "INSERT INTO people VALUES (?, ?) "
            "ON CONFLICT (account_id) DO UPDATE SET display_name = excluded.display_name",
            list(people.items()))
    con.execute(VIEWS_CHANGES)
    return len(rows)


VIEWS_METRICS = """
CREATE OR REPLACE VIEW closures AS
SELECT t.key, t.ts, t.author_id
FROM transitions t JOIN statuses s ON s.name = t.to_status
WHERE s.category = 'done';
-- Modelling decision: one row per transition into a done status; a ticket reopened and
-- reclosed appears twice.

CREATE OR REPLACE VIEW cycle_times AS
SELECT i.key,
       min(t.ts) AS started,
       i.resolved,
       date_diff('minute', min(t.ts), i.resolved) / 1440.0 AS cycle_days
FROM issues i
JOIN transitions t ON t.key = i.key AND t.to_status = (SELECT start_status FROM sync_state)
WHERE i.resolved IS NOT NULL
GROUP BY i.key, i.resolved;
-- Modelling decision: cycle time runs from the first entry into start_status, so a ticket
-- reopened months later carries the whole gap.

CREATE OR REPLACE VIEW rework AS
-- A status not in status_order is excluded from rework entirely. The INNER JOINs enforce
-- that: a transition is rework only if both endpoints have known positions and the target
-- position is less (earlier in the workflow). A transition with any unknown endpoint is
-- dropped by the join and never considered.
-- ponytail: infer position from observed transition frequency to handle partially
-- configured workflows.
SELECT t.key, t.ts, t.from_status, t.to_status, t.author_id
FROM transitions t
INNER JOIN status_order sf ON sf.status = t.from_status
INNER JOIN status_order st ON st.status = t.to_status
WHERE st.pos < sf.pos;
"""


def derive(con, status_order, start_status, review_status):
    """Rebuild the derived tables and views from raw_issues.

    Offline and idempotent: this reads only what sync already fetched, so a
    changed metric definition costs a derive run rather than a refetch.

    The three metric views bind to their inputs at different times, which
    decides when a number can move under you:

      cycle_times reads sync_state directly, so it reflects a changed
        --start-status immediately, with no re-derive.
      rework reads the status_order table, which this function rebuilds, so it
        changes only when derive runs again.
      closures reads the statuses table, which sync rebuilds (not derive), so a
        status recategorised in Jira changes it on the next sync.
    """
    if not status_order or not start_status:
        raise SystemExit(
            "derive needs --status-order and --start-status on the first run, for example:\n"
            '  urd derive --status-order "To Do,In Progress,Review,Done" '
            '--start-status "In Progress" --review-status "Review"'
        )

    # Validate status_order before persisting: no duplicates, no empties
    statuses = [s.strip() for s in status_order.split(",")]
    if any(not s for s in statuses):
        raise SystemExit(
            "--status-order must be a non-empty comma-separated list with no "
            "empty items"
        )
    if len(statuses) != len(set(statuses)):
        raise SystemExit(f"--status-order contains duplicates: {status_order}")

    save_scope(con, status_order=status_order, start_status=start_status,
               review_status=review_status)

    con.execute("CREATE OR REPLACE TABLE status_order (status VARCHAR PRIMARY KEY, pos INTEGER)")
    con.executemany(
        "INSERT INTO status_order VALUES (?, ?)",
        [(s, i) for i, s in enumerate(statuses)],
    )

    issues = derive_issues(con)
    changes = derive_changes(con)
    sprints = derive_sprints(con)
    con.execute(VIEWS_METRICS)

    print(f"derived {issues} issues, {changes} changes, {sprints} sprint memberships")
    unknown = con.execute(
        "SELECT DISTINCT to_status FROM transitions "
        "WHERE to_status NOT IN (SELECT status FROM status_order) ORDER BY 1"
    ).fetchall()
    if unknown:
        print("statuses not in --status-order, so their transitions are excluded from rework: "
              + ", ".join(u[0] for u in unknown if u[0]))
    for field, rate in con.execute(
        "SELECT name, null_rate FROM fields WHERE null_rate IS NOT NULL"
    ).fetchall():
        print(f"{field}: {rate:.0%} empty")


def report(con, path="report.html"):
    scope = load_scope(con)
    header = {
        "project": scope["project"] or "unknown",
        "component": scope["component"],
        "since": scope["earliest_since"] or "unknown",
        "synced": scope["last_sync_at"] or "never",
        "errors": con.execute("SELECT count(*) FROM sync_errors").fetchone()[0],
        "issues": con.execute("SELECT count(*) FROM issues").fetchone()[0],
    }
    with open(path, "w") as fh:
        fh.write(render.page(header, render_sections(con)))
    print(f"wrote {path}")
    return 0


def run_chart(con, chart):
    """Run one spec and hand its rows to the renderer its kind names."""
    subtitle = chart.caption
    if chart.coverage:
        numerator, denominator = con.execute(chart.coverage).fetchone()
        numerator, denominator = numerator or 0, denominator or 0
        share = 0 if not denominator else numerator / denominator
        if share < chart.threshold:
            return render.coverage_strip(chart.title, numerator, denominator, chart.threshold)
        subtitle += f" ({numerator} of {denominator} tickets)"
    cursor = con.execute(chart.sql)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]
    return render.figure(chart, rows, subtitle, con)


def render_sections(con):
    return [
        (
            section,
            [run_chart(con, c) for c in chart_specs.CHARTS if c.section == section],
        )
        for section in chart_specs.SECTIONS
        if any(c.section == section for c in chart_specs.CHARTS)
    ]


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

    if args.verb == "sync":
        save_scope(con, site=args.site, email=args.email, project=args.project,
                   component=args.component, earliest_since=args.since)
        scope = load_scope(con)
        if not scope["site"]:
            raise SystemExit("first run needs --site")
        # Email resolution: CLI flag, then environment, then stored
        email = args.email or os.environ.get("URD_EMAIL") or scope["email"]
        if not email:
            raise SystemExit("first run needs --email (or set URD_EMAIL)")
        return sync(con, Jira(scope["site"], email, token()))

    if args.verb == "derive":
        scope = load_scope(con)
        derive(
            con,
            args.status_order or scope["status_order"],
            args.start_status or scope["start_status"],
            args.review_status or scope["review_status"],
        )
        return 0

    if args.verb == "report":
        return report(con)


if __name__ == "__main__":
    sys.exit(main())
