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
import re
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
    try:
        found = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        # check=False only stops a non-zero exit from raising; the binary not
        # existing at all (every non-macOS host, including the container) is a
        # separate failure subprocess.run raises regardless. Without this, the
        # exact case the wizard exists to catch in seconds (URD_TOKEN unset) is
        # a 500 on Linux instead of the same friendly message as below.
        found = None
    if found is None or found.returncode != 0 or not found.stdout.strip():
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

    def project_statuses(self, project):
        """Statuses in one project's workflow, grouped by issue type.

        Needs no admin rights, unlike /workflow/search, which is the endpoint
        that would return the transition graph. Used to tell a status the project
        still uses from one that only appears in history.
        """
        return self.get(f"/project/{urllib.parse.quote(project)}/statuses")


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
    "thresholds",
    "abandoned_status",
    "report_since",
    "excluded_epics",
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
    earliest_since VARCHAR, last_sync_at VARCHAR, fetched_fields VARCHAR,
    thresholds VARCHAR, abandoned_status VARCHAR, report_since VARCHAR,
    excluded_epics VARCHAR
);
CREATE TABLE IF NOT EXISTS excluded_epics (
    -- Epics whose whole subtree is left out of the report. Set at report time, so
    -- flipping it costs a report run rather than a re-derive.
    key VARCHAR PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS report_window (
    -- One row. The date every chart measures from, as a real DATE so a typo is a
    -- parse error here rather than a chart quietly covering nothing. Unset is
    -- stored as a date old enough to include everything, which keeps the charts
    -- free of a COALESCE they would all have to remember.
    since DATE
);
CREATE TABLE IF NOT EXISTS sync_errors (
    -- "at" is quoted: DuckDB classifies it as a type_function keyword, and an
    -- unquoted column of that name fails to parse.
    key VARCHAR PRIMARY KEY, "at" TIMESTAMP, error VARCHAR
);
CREATE TABLE IF NOT EXISTS fields (
    id VARCHAR PRIMARY KEY, name VARCHAR, null_rate DOUBLE
);
CREATE TABLE IF NOT EXISTS workflow_statuses (
    -- Statuses the project's CURRENT workflow contains, as opposed to every
    -- status seen in its history. Empty means unknown, which is treated as "all
    -- of them": the call needs no admin but can still fail, and an older
    -- database has no rows here at all.
    -- ponytail: the API returns these per issue type and this flattens them.
    -- Upgrade path is a second column when a chart needs per-type workflows.
    status VARCHAR PRIMARY KEY
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
    if con.execute("SELECT count(*) FROM report_window").fetchone()[0] == 0:
        con.execute("INSERT INTO report_window VALUES (?)", [UNBOUNDED])
    con.execute(WINDOW_MACRO)
    if con.execute("SELECT count(*) FROM sync_state").fetchone()[0] == 0:
        con.execute(f"INSERT INTO sync_state VALUES ({','.join(['NULL'] * len(SCOPE_COLUMNS))})")
    return con


# A ticket counts if it was created or resolved inside the window; an event counts
# if it happened inside it. A macro rather than a repeated subquery, because it
# appears in every chart and reads as what it means. It resolves report_window at
# query time, so changing the window needs no re-derive.
#
# NULL never passes: an unresolved ticket is not "resolved in the window", so an
# issue-level filter has to be in_window(created) OR in_window(resolved).
WINDOW_MACRO = (
    "CREATE OR REPLACE MACRO in_window(ts) AS "
    "ts >= (SELECT since FROM report_window)"
)

UNBOUNDED = "1900-01-01"


def set_excluded_epics(con, keys):
    """Leave an epic and everything under it out of every chart.

    A trash-bin epic with a hundred abandoned children skews any total it appears
    in, and there is no way to tell it from a real one in the data.
    """
    cleaned = [k.strip() for k in keys if k and k.strip()]
    con.execute("DELETE FROM excluded_epics")
    if cleaned:
        con.executemany("INSERT INTO excluded_epics VALUES (?) ON CONFLICT DO NOTHING",
                        [(k,) for k in cleaned])
    # A direct UPDATE, not save_scope: that function skips None by design, so it
    # can set a value and never clear one. Without this, removing the last
    # exclusion would leave the stored list behind and the header would keep
    # naming an epic that is back in the report.
    con.execute("UPDATE sync_state SET excluded_epics = ?", [",".join(cleaned) or None])
    return cleaned


def stored_excluded_epics(con):
    stored = load_scope(con)["excluded_epics"]
    return [k for k in (stored or "").split(",") if k]


def set_report_window(con, since):
    """Set the date every chart measures from. None means everything."""
    if since is not None:
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise SystemExit(f"--since wants YYYY-MM-DD, got {since!r}") from None
    con.execute("DELETE FROM report_window")
    con.execute("INSERT INTO report_window VALUES (?)", [since or UNBOUNDED])
    con.execute(WINDOW_MACRO)
    # Same reason as the exclusion list: clearing the window has to clear what the
    # header reads, or a whole-history report keeps claiming a window.
    con.execute("UPDATE sync_state SET report_since = ?", [since])
    return since


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

    # /myself is the only call here that a rejected token fails. An unauthenticated
    # caller still gets 200 from /field and a 200 with an empty page from
    # /search/jql, so without this a revoked token syncs nothing, stamps
    # last_sync_at and prints "synced, no errors". getattr for the reason
    # _refresh_workflow_statuses uses it: the test doubles implement only the
    # calls they exercise.
    if getattr(jira, "get", None):
        jira.get("/myself")

    # Before anything is fetched, not after: the field list is built from the
    # `fields` table, so resolving it afterwards left a first run asking for the
    # built-ins only, forever.
    _refresh_lookups(con, jira, scope["project"])
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


def _refresh_lookups(con, jira, project=None):
    """Field ids and the status name to category map, both resolved by name so
    no instance specific id is ever compiled in."""
    con.execute("DELETE FROM fields")
    for field in jira.fields():
        con.execute("INSERT INTO fields VALUES (?, ?, NULL) ON CONFLICT (id) DO NOTHING",
                    [field["id"], field.get("name")])
    _refresh_workflow_statuses(con, jira, project)
    con.execute("DELETE FROM statuses")
    for status in jira.statuses():
        con.execute(
            "INSERT INTO statuses VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
            [status["name"], (status.get("statusCategory") or {}).get("key")],
        )


def _refresh_workflow_statuses(con, jira, project):
    """Record which statuses each project in scope currently uses.

    Failure is not fatal and is not silent either. The listing degrades to
    treating every observed status as current, which is what it did before this
    existed, so a missing permission costs a hint rather than a run.
    """
    getter = getattr(jira, "project_statuses", None)
    if not project or getter is None:
        # Explicit rather than a swallowed AttributeError: several test doubles
        # implement only the calls they exercise, and a real client that somehow
        # lacked this should not look like a permissions problem.
        return
    names = set()
    for key in (k.strip() for k in project.split(",")):
        if not key:
            continue
        try:
            for issue_type in getter(key):
                names.update(s.get("name") for s in issue_type.get("statuses", []))
        except SystemExit as err:
            print(f"could not read {key}'s workflow statuses ({err}); "
                  "every observed status will be treated as current", file=sys.stderr)
            return
    con.execute("DELETE FROM workflow_statuses")
    rows = [(n,) for n in sorted(x for x in names if x)]
    if rows:
        con.executemany("INSERT INTO workflow_statuses VALUES (?)", rows)


ISSUES_SCHEMA = """
CREATE OR REPLACE TABLE issues_all (
    key VARCHAR PRIMARY KEY, project VARCHAR, type VARCHAR,
    -- Fetched since the first sync and stored only now. A key alone does not say
    -- which ticket it is, and the title is already sitting in raw_issues, so this
    -- costs a derive rather than a refetch. Positioned to match the row order in
    -- derive_issues, which builds it straight after issuetype.
    summary VARCHAR,
    status VARCHAR,
    status_category VARCHAR, assignee_id VARCHAR, reporter_id VARCHAR,
    created TIMESTAMP, updated TIMESTAMP, resolved TIMESTAMP,
    story_points DOUBLE, timespent_s BIGINT, parent VARCHAR,
    fix_versions VARCHAR[], labels VARCHAR[], components VARCHAR[]
);
CREATE TABLE IF NOT EXISTS people (account_id VARCHAR PRIMARY KEY, display_name VARCHAR);
"""

CHANGES_SCHEMA = """
CREATE OR REPLACE TABLE changes_all (
    key VARCHAR, ts TIMESTAMP, field VARCHAR,
    from_id VARCHAR, from_str VARCHAR, to_id VARCHAR, to_str VARCHAR,
    author_id VARCHAR, history_id BIGINT
);
"""

SPRINTS_SCHEMA = """
CREATE OR REPLACE TABLE issue_sprints_all (
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
        # A zero is an unestimated ticket, not an estimate of zero, so it counts
        # as missing here too. Otherwise this prints 0% empty on a field that
        # 69% of tickets never had filled in.
        missing_points += not points
        rows.append(
            [
                key, key.split("-")[0], (f.get("issuetype") or {}).get("name"),
                f.get("summary"),
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
        con.executemany(f"INSERT INTO issues_all VALUES ({','.join(['?'] * 17)})", rows)
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
    # Created here so derive_issues is usable on its own, and again in derive()
    # after the abandoned column is added: a SELECT * view does not pick up a
    # column that appeared after it was defined.
    con.execute(VIEWS_SCOPE_ISSUES)
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
    # Created unconditionally, like VIEWS_SCOPE_ISSUES in derive_issues: a database
    # that has a scope but was never synced has no Sprint field to resolve, yet
    # VIEWS_SPRINT_ATTRIBUTION reads this view regardless of whether any sprint
    # data exists. Without this, report_html on a fresh, unsynced database fails
    # with "issue_sprints does not exist" rather than rendering an empty report.
    con.execute(VIEWS_SCOPE_SPRINTS)
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
        con.executemany(f"INSERT INTO issue_sprints_all VALUES ({','.join(['?'] * 7)})", rows)
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
        con.executemany(f"INSERT INTO changes_all VALUES ({','.join(['?'] * 9)})", rows)
    if people:
        con.executemany(
            "INSERT INTO people VALUES (?, ?) "
            "ON CONFLICT (account_id) DO UPDATE SET display_name = excluded.display_name",
            list(people.items()))

    # The scope view first: transitions reads `changes`, which is a view over
    # changes_all and does not exist until this runs.
    con.execute(VIEWS_SCOPE_CHANGES)
    con.execute(VIEWS_CHANGES)
    return len(rows)


# Attribute a ticket mutation to the sprint that was RUNNING when it happened,
# rather than to a calendar week or to the ticket's own sprint. Two steps, and
# nothing is guessed:
#
#   1. If the ticket belongs to exactly one of the sprints running at that
#      moment, that is the one. On the first real project this settles 56% of the
#      ambiguous cases, which exist because two boards (a team's and a
#      neighbouring one's) run parallel sprints over the same fortnight.
#   2. Otherwise, if exactly one sprint was running, that is the one.
#   3. Otherwise the mutation is unattributed. Two sprints running and the ticket
#      in neither has no answer, and picking one would put work in a sprint it had
#      nothing to do with.
#
# About two thirds of mutations attribute, so anything built on this carries a
# coverage query rather than implying it covers everything.
MUTATION_SPRINT_SQL = """
WITH cand AS (
    SELECT m.key, m.ts, m.kind, s.sprint_id, s.sprint_name, s.start,
           (mem.key IS NOT NULL) AS member
    FROM mutations m
    JOIN sprint_windows s ON m.ts >= s.start AND m.ts < s."end"
    LEFT JOIN (SELECT DISTINCT key, sprint_id FROM issue_sprints) mem
           ON mem.key = m.key AND mem.sprint_id = s.sprint_id
),
ranked AS (
    SELECT *, count(*) OVER w AS candidates,
              count(*) FILTER (WHERE member) OVER w AS members
    FROM cand WINDOW w AS (PARTITION BY key, ts, kind)
)
SELECT key, ts, kind, sprint_id, sprint_name, start AS sprint_start
FROM ranked
WHERE (members = 1 AND member) OR (members = 0 AND candidates = 1)
"""

# Every chart reads issues, changes and issue_sprints. Filtering there rather
# than in nineteen queries means a chart added later inherits the exclusion, and
# there is no chart that can forget it. The base tables keep an _all suffix and
# nothing outside derive touches them.
#
# Filtering `issues` alone would be worse than not filtering: closures and
# durations come from `changes`, so an excluded ticket would vanish from every
# ticket-based chart while still driving every event-based one.
# Split so each derive_* function can create the views over its own table and
# stay self-sufficient. Doing it all at the end of derive() looked tidier and
# broke thirteen tests that call derive_changes on its own and then read
# status_durations, which is the contract those functions have always had.
VIEWS_SCOPE_ISSUES = """
CREATE OR REPLACE VIEW excluded_tickets AS
-- The epics themselves and everything parented to them. Reads issues_all, since
-- `issues` is the view this feeds.
SELECT key FROM excluded_epics
UNION
SELECT i.key FROM issues_all i JOIN excluded_epics e ON i.parent = e.key;

CREATE OR REPLACE VIEW issues AS
SELECT * FROM issues_all WHERE key NOT IN (SELECT key FROM excluded_tickets);
"""

VIEWS_SCOPE_CHANGES = """
CREATE OR REPLACE VIEW changes AS
SELECT * FROM changes_all WHERE key NOT IN (SELECT key FROM excluded_tickets);
"""

VIEWS_SCOPE_SPRINTS = """
CREATE OR REPLACE VIEW issue_sprints AS
SELECT * FROM issue_sprints_all WHERE key NOT IN (SELECT key FROM excluded_tickets);
"""

VIEWS_SPRINT_ATTRIBUTION = f"""
CREATE OR REPLACE VIEW sprint_windows AS
-- DISTINCT because issue_sprints holds one row per membership, not per sprint.
SELECT DISTINCT sprint_id, sprint_name, start, "end" FROM issue_sprints
WHERE start IS NOT NULL AND "end" IS NOT NULL;

CREATE OR REPLACE VIEW mutations AS
-- Every change to a ticket, plus its creation, which is a mutation the changelog
-- does not record.
SELECT key, created AS ts, 'created' AS kind FROM issues
UNION ALL
SELECT key, ts, field AS kind FROM changes;

CREATE OR REPLACE VIEW mutation_sprint AS {MUTATION_SPRINT_SQL};
"""

VIEWS_METRICS = """
CREATE OR REPLACE VIEW closures AS
-- `abandoned` separates a ticket that was dropped from one that shipped. Both
-- are closures and both stay countable here; only their meaning differs, which
-- is why this is a column rather than a filter. Keyed on the status the
-- transition moved INTO, not on the issue's current resolution: 28 of 552
-- closures in the first real project belong to tickets that closed more than
-- once, and a current-state field cannot say which of those events was which.
SELECT t.key, t.ts, t.author_id,
       t.to_status IN (SELECT status FROM abandoned_status) AS abandoned
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


_CATEGORY_RANK = {"new": 0, "indeterminate": 1, "done": 2}


def observed_statuses(con):
    """Every status the fetched data actually mentions, in workflow-ish order.

    Reads raw_issues directly rather than the transitions view, because the one
    moment this is most needed is a first run: derive refuses to start without
    --status-order, so it has built no views, and the operator has no other way
    to enumerate the statuses they are being asked to order.

    Ordered by status category first, then by how long work typically takes to
    reach the status. The second is a heuristic and can be wrong: a parking
    status such as Blocked sits in the `new` category here yet is entered
    mid-flow, so it sorts earlier than it belongs. It is meant to produce a line
    worth editing, not one worth trusting.
    """
    categories = dict(con.execute("SELECT name, category FROM statuses").fetchall())
    # Empty means unknown, and unknown must not mark every status retired.
    workflow = {r[0] for r in con.execute("SELECT status FROM workflow_statuses").fetchall()}
    seen = {}
    for (raw,) in con.execute("SELECT json FROM raw_issues").fetchall():
        issue = json.loads(raw)
        created = _ts((issue.get("fields") or {}).get("created"))
        current = ((issue.get("fields") or {}).get("status") or {}).get("name")
        if current:
            seen.setdefault(current, {"entries": 0, "delays": [], "current": 0})
            seen[current]["current"] += 1
        moves = sorted(
            (
                (_ts(history.get("created")), item)
                for history in (issue.get("changelog") or {}).get("histories", [])
                for item in history.get("items", [])
                if item.get("field") == "status"
            ),
            key=lambda m: (m[0] is None, m[0]),
        )
        arrived = set()
        for index, (when, item) in enumerate(moves):
            name = item.get("toString")
            if name:
                stat = seen.setdefault(name, {"entries": 0, "delays": [], "current": 0})
                stat["entries"] += 1
                # Only this ticket's FIRST arrival contributes to the median. A
                # status re-entered after rework is reached late, and counting
                # every arrival drags its median past a status reached once
                # early: on the real project that inverted In Progress and
                # the review status, which the transition counts separate 506 to 39.
                if created and when and name not in arrived:
                    stat["delays"].append((when - created).total_seconds() / 86400.0)
                    arrived.add(name)
            # A ticket's opening status is only ever a `fromString`: work leaves it
            # and never enters it, so collecting toString alone loses it entirely,
            # and it is the one status the operator most needs listed first.
            if index == 0 and item.get("fromString"):
                opening = seen.setdefault(
                    item["fromString"], {"entries": 0, "delays": [], "current": 0})
                opening["delays"].append(0.0)
    rows = []
    for name, stat in seen.items():
        delays = sorted(stat["delays"])
        median = delays[len(delays) // 2] if delays else 0.0
        rows.append({
            "status": name,
            "category": categories.get(name),
            "current": stat["current"],
            "entries": stat["entries"],
            "median_days": median,
            "in_workflow": (name in workflow) if workflow else True,
        })
    rows.sort(key=lambda r: (_CATEGORY_RANK.get(r["category"], 1), r["median_days"], r["status"]))
    return rows


def format_statuses(rows):
    """The listing plus a --status-order line to paste and edit."""
    if not rows:
        return "no statuses found in the fetched data; run sync first"
    out = ["statuses found in the fetched data:",
           f"  {'category':<14} {'status':<28} {'now':>5} {'entered':>8} "
           f"{'median day':>11}  in workflow"]
    for r in rows:
        out.append(f"  {str(r['category'] or '?'):<14} {r['status']:<28} "
                   f"{r['current']:>5} {r['entries']:>8} {r['median_days']:>11.1f}"
                   f"  {'yes' if r['in_workflow'] else 'retired'}")
    current = [r for r in rows if r["in_workflow"]]
    flow = [r["status"] for r in current if r["category"] != "done"]
    done = [r["status"] for r in current if r["category"] == "done"]
    retired = len(rows) - len(current)
    out.append("")
    out.append("ordered by category, then by how long work takes to reach each one.")
    if retired:
        out.append(f"{retired} status(es) are not in the project's current workflow and are "
                   "left out of the line below; they remain real history.")
    out.append("a parking status such as Blocked sorts earlier than it belongs; edit before use:")
    out.append(f'  --status-order "{",".join(flow + done)}"')
    return "\n".join(out)


# issues, changes and issue_sprints were base tables before they became
# scope-filtered views over an _all table. CREATE OR REPLACE VIEW will not replace
# a table, so without this derive fails outright on any database built by an
# earlier version, which is every database anyone actually has. Dropping is safe:
# derive rebuilds all three from raw_issues on the same run.
_SCOPE_VIEWS = ("issues", "changes", "issue_sprints")


def _migrate_scope_views(con):
    legacy = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_name IN "
        f"({', '.join(repr(v) for v in _SCOPE_VIEWS)})").fetchall()]
    for name in legacy:
        con.execute(f"DROP TABLE {name}")
    return legacy


def derive(con, status_order, start_status, review_status, abandoned_status=None):
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
            "derive needs --status-order and --start-status on the first run.\n\n"
            + format_statuses(observed_statuses(con))
            + '\n  --start-status "In Progress" --review-status "Review"'
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

    # Validated against the statuses table, which sync populates: a name that is
    # not a done-category status would quietly remove work from the delivered
    # line while leaving it open elsewhere, which is worse than a plain typo.
    abandoned = [a.strip() for a in (abandoned_status or "").split(",") if a.strip()]
    if abandoned:
        known = {r[0] for r in con.execute(
            "SELECT name FROM statuses WHERE category = 'done'").fetchall()}
        stray = [a for a in abandoned if a not in known]
        if stray and known:
            raise SystemExit(
                f"--abandoned-status names {', '.join(stray)}, which is not a "
                f"done-category status. Candidates: {', '.join(sorted(known))}"
            )

    # Everything from here on is one transaction. derive_issues drops and
    # recreates the `issues` view with the old column list, then this function
    # adds `abandoned` to issues_all and recreates the view again; a reader on
    # another cursor between those two points sees a view with no `abandoned`
    # column while every chart that filters on it is still live. BEGIN/COMMIT
    # makes the whole rebuild atomic, so a concurrent reader sees either the
    # complete old schema or the complete new one, never the gap between them.
    # A lock would serialize this against every reader instead of just hiding
    # the gap from them, and item 1 removes the only lock this could have used
    # anyway. sync is the long phase and stays outside this entirely, which is
    # what keeps pages serving while it runs.
    con.execute("BEGIN")
    try:
        save_scope(con, status_order=status_order, start_status=start_status,
                   review_status=review_status, abandoned_status=abandoned_status)
        con.execute("CREATE OR REPLACE TABLE abandoned_status (status VARCHAR PRIMARY KEY)")
        if abandoned:
            # Guarded: DuckDB's executemany rejects an empty parameter list
            # outright rather than doing nothing, and no abandoned status is
            # the default case.
            con.executemany("INSERT INTO abandoned_status VALUES (?)",
                            [(a,) for a in abandoned])

        con.execute(
            "CREATE OR REPLACE TABLE status_order (status VARCHAR PRIMARY KEY, pos INTEGER)")
        con.executemany(
            "INSERT INTO status_order VALUES (?, ?)",
            [(s, i) for i, s in enumerate(statuses)],
        )

        _migrate_scope_views(con)
        issues = derive_issues(con)
        # Current-state twin of closures.abandoned, for the charts that ask
        # `status_category = 'done'` rather than counting closure events. Added
        # here rather than in ISSUES_SCHEMA so the insert keeps its positional
        # column list; CREATE OR REPLACE drops it again on every derive anyway.
        con.execute("ALTER TABLE issues_all ADD COLUMN IF NOT EXISTS abandoned BOOLEAN")
        con.execute(
            "UPDATE issues_all SET abandoned = status IN (SELECT status FROM abandoned_status)")
        con.execute(VIEWS_SCOPE_ISSUES)
        changes = derive_changes(con)
        sprints = derive_sprints(con)
        con.execute(VIEWS_METRICS)
        con.execute(VIEWS_SPRINT_ATTRIBUTION)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise

    print(f"derived {issues} issues, {changes} changes, {sprints} sprint memberships")
    unknown = con.execute(
        "SELECT DISTINCT to_status FROM transitions "
        "WHERE to_status NOT IN (SELECT status FROM status_order) ORDER BY 1"
    ).fetchall()
    if unknown:
        print("statuses not in --status-order, so their transitions are excluded from rework: "
              + ", ".join(u[0] for u in unknown if u[0]))
    print(format_statuses(observed_statuses(con)))
    for field, rate in con.execute(
        "SELECT name, null_rate FROM fields WHERE null_rate IS NOT NULL"
    ).fetchall():
        print(f"{field}: {rate:.0%} empty")


def report_html(con, tiers=None):
    """The report as a string. `report` writes this to a file.

    One rendering path, not two: the served page and the archived file are the
    same bytes, so a chart cannot look different depending on how it was asked
    for.
    """
    scope = load_scope(con)
    header = {
        "project": scope["project"] or "unknown",
        "component": scope["component"],
        "since": scope["earliest_since"] or "unknown",
        "synced": scope["last_sync_at"] or "never",
        "window": scope["report_since"],
        "excluded": stored_excluded_epics(con),
        "exempt": [c.title for c in chart_specs.CHARTS
                   if c.key in chart_specs.WINDOW_EXEMPT],
        "errors": con.execute("SELECT count(*) FROM sync_errors").fetchone()[0],
        "issues": con.execute("SELECT count(*) FROM issues").fetchone()[0],
    }
    return render.page(header, render_sections(con, tiers))


def report(con, path="report.html", tiers=None):
    with open(path, "w") as fh:
        fh.write(report_html(con, tiers))
    print(f"wrote {path}")
    return 0


def parse_thresholds(pairs, base=None):
    """`tier=share` strings into a {tier: float} map, over `base`.

    Every failure mode exits rather than being skipped. A mistyped tier that is
    silently dropped is the worst outcome available here: the operator sees a
    clean run and believes the number changed.
    """
    tiers = dict(base or chart_specs.THRESHOLDS)
    for pair in pairs or ():
        name, sep, raw = str(pair).partition("=")
        if not sep or not name:
            raise SystemExit(f"--threshold wants tier=share, got {pair!r}")
        if name not in chart_specs.THRESHOLDS:
            raise SystemExit(
                f"unknown threshold tier {name!r}; "
                f"have {', '.join(sorted(chart_specs.THRESHOLDS))}"
            )
        try:
            share = float(raw)
        except ValueError:
            raise SystemExit(f"--threshold {name}: {raw!r} is not a number") from None
        if not 0 <= share <= 1:
            raise SystemExit(f"--threshold {name}: {share} is not a share between 0 and 1")
        tiers[name] = share
    return tiers


def format_thresholds(tiers):
    """The stored form, and the same `tier=share` shape the flag takes, so the
    column can be read back through parse_thresholds without a second parser."""
    return ",".join(f"{name}={share}" for name, share in sorted(tiers.items()))


def stored_thresholds(con):
    stored = load_scope(con)["thresholds"]
    return parse_thresholds(stored.split(",") if stored else [])


def run_chart(con, chart, tiers=None):
    """Run one spec and hand its rows to the renderer its kind names."""
    tiers = chart_specs.THRESHOLDS if tiers is None else tiers
    subtitle = chart.caption
    if chart.coverage:
        numerator, denominator = con.execute(chart.coverage).fetchone()
        numerator, denominator = numerator or 0, denominator or 0
        share = 0 if not denominator else numerator / denominator
        limit = tiers[chart.tier]
        if share < limit:
            return render.coverage_strip(chart.title, numerator, denominator, limit,
                                         unit=chart.options.get("unit", "tickets"))
        subtitle += (f" ({numerator} of {denominator} "
                     f"{chart.options.get('unit', 'tickets')})")
    cursor = con.execute(chart.sql)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]
    # Built from the synced site, never compiled in: the same report against a
    # different instance has to link to that instance.
    site = load_scope(con)["site"]
    link_base = f"https://{site}/browse/" if site else None
    return render.figure(chart, rows, subtitle, con, link_base)


def render_sections(con, tiers=None):
    return [
        (
            section,
            [run_chart(con, c, tiers) for c in chart_specs.CHARTS if c.section == section],
        )
        for section in chart_specs.SECTIONS
        if any(c.section == section for c in chart_specs.CHARTS)
    ]


def build_parser():
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
    p_derive.add_argument(
        "--abandoned-status",
        help="done-category statuses meaning dropped rather than delivered, "
             "comma separated. Counted separately, never as delivery.")

    p_report = sub.add_parser("report", help="write report.html from the derived tables")
    p_report.add_argument(
        "--threshold", action="append", metavar="TIER=SHARE",
        help="minimum coverage before a chart is replaced by a strip, e.g. "
             "points=0.4. Repeatable, remembered between runs.")
    p_report.add_argument(
        "--exclude-epic", action="append", metavar="KEY",
        help="leave this epic and every ticket under it out of every chart. "
             "Repeatable, remembered between runs; pass an empty value to clear.")
    p_report.add_argument(
        "--since", metavar="YYYY-MM-DD",
        help="the date every chart measures from. Remembered between runs; "
             "pass 1900-01-01 to go back to everything.")

    p_sql = sub.add_parser("sql", help="run a query against the database")
    p_sql.add_argument("query")

    p_serve = sub.add_parser("serve", help="serve the report over HTTP")
    # 127.0.0.1, not 0.0.0.0. The report is unauthenticated: anyone who reaches
    # the port reads every ticket title and can start a sync. Exposing it has to
    # be a deliberate flag rather than the default.
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8731)
    p_serve.add_argument("--volume", default=os.environ.get("URD_VOLUME", "urd-data"))
    return parser


# Every key seed_from_env reads. compose.yaml must pass through exactly this
# set, or a key it forgets can never be set from the environment at all; that
# gap is what once shipped a container that could sync but never derive, since
# derive refuses without status_order. test_container.py checks compose.yaml
# against this same list, so the two cannot drift apart silently again.
SEED_ENV_KEYS = ("URD_SITE", "URD_EMAIL", "URD_PROJECT", "URD_COMPONENT", "URD_SINCE",
                 "URD_STATUS_ORDER", "URD_START_STATUS", "URD_REVIEW_STATUS")


def project_slug(project):
    """The database filename and URL segment a project key becomes.

    Shared with the setup form rather than duplicated: the slug is what the file
    is called and what the URL says, so two derivations would mean the
    environment path and the form disagreeing about where a project lives.

    Not validated here. It can return a string ProjectRegistry.add refuses, and
    that refusal is the one place the charset is enforced.
    """
    return re.sub(r"[^a-z0-9-]", "-", (project or "").split(",")[0].strip().lower())


def seed_from_env(registry, env=None):
    """Create the first project from the environment, and only the first.

    A configured database wins over a disagreeing environment: restarting with a
    stale compose file must not silently rescope someone's data. With any project
    already present this does nothing at all.
    """
    env = os.environ if env is None else env
    if registry.projects():
        return None
    site, project, email, since = (env.get("URD_SITE"), env.get("URD_PROJECT"),
                                   env.get("URD_EMAIL"), env.get("URD_SINCE"))
    if not (site and project and email and since):
        return None
    slug = project_slug(project)
    try:
        created = registry.add(slug)
    except ValueError:
        # A value that survives the emptiness check above can still reduce to an
        # unusable slug (",", "!!!"). A stale or hand-edited compose file is
        # exactly the kind of input that does this, and starting with nothing
        # seeded beats a crash loop: land on /setup instead, where a person can
        # fix it by hand.
        print(f"URD_PROJECT={project!r} does not make a usable project key "
              f"(need [a-z0-9][a-z0-9-]*, got {slug!r}); not seeding, use /setup",
              file=sys.stderr)
        return None
    save_scope(created.con, site=site, email=email, project=project,
               component=env.get("URD_COMPONENT") or None, earliest_since=since,
               status_order=env.get("URD_STATUS_ORDER") or None,
               start_status=env.get("URD_START_STATUS") or None,
               review_status=env.get("URD_REVIEW_STATUS") or None)
    return created


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verb == "serve":
        import projects
        import webapp
        registry = projects.ProjectRegistry(args.volume)
        seed_from_env(registry)
        print(f"urd serving {len(registry.projects())} project(s) on "
              f"http://{args.host}:{args.port}")
        webapp.create_app(registry).run(host=args.host, port=args.port)
        return 0

    try:
        con = open_db(args.db)
    except duckdb.IOException as exc:
        # IOException is DuckDB's general filesystem error, not a lock-specific
        # one: a bad path raises it too. Only "Conflicting lock is held", which is
        # what a running `urd serve` looks like from a second process, gets the
        # friendly phrasing; anything else (a typo'd path, for instance) would be
        # misdiagnosed as a server that isn't actually running. Match the full
        # phrase, not "lock" alone: DuckDB's storage is organised in blocks, and
        # "block" contains "lock" as a substring.
        if "conflicting lock" in str(exc).lower():
            sys.exit(f"cannot open {args.db}: another urd is holding it "
                     f"(stop `urd serve` first)\n  {exc}")
        sys.exit(f"cannot open {args.db}: {exc}")

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
            args.abandoned_status or scope["abandoned_status"],
        )
        return 0

    if args.verb == "report":
        scope = load_scope(con)
        set_report_window(con, args.since or scope["report_since"])
        # An empty --exclude-epic clears the list, which is why this is not just
        # `args.exclude_epic or stored`: passing "" has to mean something.
        if args.exclude_epic is not None:
            set_excluded_epics(con, args.exclude_epic)
        else:
            set_excluded_epics(con, stored_excluded_epics(con))
        tiers = parse_thresholds(args.threshold, base=stored_thresholds(con))
        save_scope(con, thresholds=format_thresholds(tiers))
        return report(con, tiers=tiers)


if __name__ == "__main__":
    sys.exit(main())
