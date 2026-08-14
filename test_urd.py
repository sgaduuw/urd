import json
import os
import pathlib
import tempfile
from datetime import datetime

import urd


def _tmpdb():
    return os.path.join(tempfile.mkdtemp(), "test.duckdb")


class FakeOpener:
    """Answers requests from a canned {path_with_query_substring: payload} map."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                return 200, json.dumps(payload).encode()
        raise AssertionError(f"unexpected request: {url}")


FIXTURES = pathlib.Path(__file__).parent / "tests" / "fixtures"


def load_fixtures(con, *names):
    """Insert fixtures as if sync had fetched them, plus the lookup rows sync
    would have populated from /field and /status."""
    for name in names:
        raw = (FIXTURES / f"{name}.json").read_text()
        issue = json.loads(raw)
        con.execute(
            "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
            [issue["key"], issue["fields"]["updated"], urd._now(), raw],
        )
    con.execute("INSERT INTO fields VALUES ('customfield_20001', 'Story Points', NULL)")
    con.execute("INSERT INTO fields VALUES ('customfield_20002', 'Sprint', NULL)")
    for status, category in (
        ("To Do", "new"), ("In Progress", "indeterminate"),
        ("Review", "indeterminate"), ("Done", "done"),
    ):
        con.execute("INSERT INTO statuses VALUES (?, ?)", [status, category])
    urd.save_scope(con, status_order="To Do,In Progress,Review,Done",
                   start_status="In Progress", review_status="Review")


def test_token_prefers_the_environment_over_the_keychain():
    assert urd.token({"URD_TOKEN": "from-env"}) == "from-env"


def test_search_follows_the_page_token_and_stops_on_is_last():
    page1 = {
        "issues": [{"key": "PROJ-1", "fields": {"updated": "2026-01-01T00:00:00.000+0000"}}],
        "nextPageToken": "tok2",
        "isLast": False,
    }
    page2 = {
        "issues": [{"key": "PROJ-2", "fields": {"updated": "2026-01-02T00:00:00.000+0000"}}],
        "isLast": True,
    }
    opener = FakeOpener({"nextPageToken=tok2": page2, "/search/jql": page1})
    jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
    assert [k for k, _ in jira.search("project = PROJ")] == ["PROJ-1", "PROJ-2"]
    assert len(opener.calls) == 2


def test_get_is_authenticated_with_basic_auth():
    seen = {}

    def opener(url, headers):
        seen.update(headers)
        return 200, b"{}"

    urd.Jira("example.atlassian.net", "a@b.c", "tok", opener=opener).get("/field")
    assert seen["Authorization"].startswith("Basic ")


def test_issue_pages_a_truncated_changelog():
    """A ticket with more history than one page must not lose its early entries."""
    truncated = {
        "key": "PROJ-9",
        "fields": {"updated": "2026-01-01T00:00:00.000+0000"},
        "changelog": {
            "total": 3,
            "maxResults": 2,
            "startAt": 0,
            "histories": [{"id": "1"}, {"id": "2"}],
        },
    }
    rest = {"total": 3, "maxResults": 2, "startAt": 2, "values": [{"id": "3"}]}
    opener = FakeOpener({"/changelog": rest, "/issue/PROJ-9": truncated})
    got = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener).issue(
        "PROJ-9", "summary"
    )
    assert [h["id"] for h in got["changelog"]["histories"]] == ["1", "2", "3"]


def test_transport_failures_retry_then_exit():
    """Transport failure (599 status) on first attempt should retry then succeed."""
    attempt = [0]

    def opener(url, headers):
        attempt[0] += 1
        if attempt[0] == 1:
            return urd.TRANSPORT_ERROR_STATUS, b"timeout"
        return 200, b"{}"

    real_sleep = urd.time.sleep
    urd.time.sleep = lambda seconds: None
    try:
        jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
        jira.get("/field")
        assert attempt[0] == 2
    finally:
        urd.time.sleep = real_sleep


def test_transport_failures_exit_on_second_attempt():
    """Transport failure (599 status) on both attempts should raise SystemExit."""
    def opener(url, headers):
        return urd.TRANSPORT_ERROR_STATUS, b"timeout"

    real_sleep = urd.time.sleep
    urd.time.sleep = lambda seconds: None
    try:
        jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
        try:
            jira.get("/field")
            raise AssertionError("expected SystemExit")
        except SystemExit as e:
            assert "GET" in str(e) and "599" in str(e)
    finally:
        urd.time.sleep = real_sleep


def test_search_detects_stalled_page_token():
    """If the server returns the same token, search should raise, not spin."""
    page = {
        "issues": [{"key": "PROJ-1", "fields": {"updated": "2026-01-01T00:00:00.000+0000"}}],
        "nextPageToken": "tok1",
        "isLast": False,
    }
    opener = FakeOpener({"/search/jql": page})
    jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
    try:
        list(jira.search("project = PROJ"))
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "stalled" in str(e).lower()


def test_truncated_changelog_raises_instead_of_corrupting():
    """A changelog that returns fewer entries than promised should raise."""
    truncated = {
        "key": "PROJ-9",
        "fields": {"updated": "2026-01-01T00:00:00.000+0000"},
        "changelog": {
            "total": 3,
            "maxResults": 2,
            "startAt": 0,
            "histories": [{"id": "1"}, {"id": "2"}],
        },
    }
    empty_page = {"total": 3, "maxResults": 2, "startAt": 2, "values": []}
    opener = FakeOpener({"/changelog": empty_page, "/issue/PROJ-9": truncated})
    jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
    try:
        jira.issue("PROJ-9", "summary")
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "PROJ-9" in str(e) and "partial history" in str(e).lower()


def test_malformed_response_maps_to_599():
    """HTTPException (malformed/truncated response) maps to 599 status code."""
    attempt = [0]

    def opener(url, headers):
        attempt[0] += 1
        if attempt[0] == 1:
            return urd.TRANSPORT_ERROR_STATUS, b"malformed"
        return 200, b"{}"

    real_sleep = urd.time.sleep
    urd.time.sleep = lambda seconds: None
    try:
        jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
        jira.get("/field")
        assert attempt[0] == 2
    finally:
        urd.time.sleep = real_sleep


def test_redirects_are_refused():
    """A redirect raises immediately with the target URL."""
    attempt = [0]

    def opener(url, headers):
        attempt[0] += 1
        raise SystemExit("refusing redirect to https://attacker.com; check --site")

    jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
    try:
        jira.get("/field")
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "refusing redirect" in str(e)
        assert "check --site" in str(e)
        assert attempt[0] == 1  # did not retry


def test_the_client_can_only_issue_get_requests():
    """Read-only is a design guarantee, not a preference, so pin it structurally.
    Every other test injects a fake opener, which leaves the real request builder
    untested."""
    source = (pathlib.Path(__file__).parent / "urd.py").read_text()
    assert 'method="GET"' in source
    assert not any(
        f'method="{verb}"' in source for verb in ("POST", "PUT", "PATCH", "DELETE")
    )
    assert (
        "urllib.request.urlopen(" not in source
    ), "requests must go through _OPENER, which refuses redirects"
    before_http_error = source.split("except urllib.error.HTTPError")[0]
    assert (
        "OSError" not in before_http_error
    ), "HTTPError is an OSError subclass and must be caught first"


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


def test_unchanged_issues_are_not_refetched():
    stored = {"PROJ-1": "2026-01-01T00:00:00.000+0000"}
    remote = [("PROJ-1", "2026-01-01T00:00:00.000+0000")]
    assert urd.keys_to_fetch(stored, remote) == []


def test_new_and_changed_issues_are_fetched():
    stored = {"PROJ-1": "2026-01-01T00:00:00.000+0000"}
    remote = [
        ("PROJ-1", "2026-02-01T00:00:00.000+0000"),
        ("PROJ-2", "2026-01-01T00:00:00.000+0000"),
    ]
    assert urd.keys_to_fetch(stored, remote) == ["PROJ-1", "PROJ-2"]


def test_jql_quotes_the_component_and_lists_projects():
    jql = urd.build_jql("PROJ,OTHER", "Team Name", "2026-01-01")
    assert 'project in (PROJ,OTHER)' in jql
    assert 'component in ("Team Name")' in jql
    assert 'updated >= "2026-01-01"' in jql


def test_jql_without_a_component_scopes_the_whole_project():
    assert "component" not in urd.build_jql("PROJ", None, "2026-01-01")


def test_a_failed_issue_is_recorded_and_the_rest_still_land():
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")

    class Flaky:
        def search(self, jql):
            yield "PROJ-1", "u1"
            yield "PROJ-2", "u2"

        def issue(self, key, fields):
            if key == "PROJ-1":
                raise SystemExit("boom")
            return {"key": key, "fields": {"updated": "u2"}}

        def fields(self):
            return []

        def statuses(self):
            return []

    urd.sync(con, Flaky())
    assert [r[0] for r in con.execute("SELECT key FROM raw_issues").fetchall()] == ["PROJ-2"]
    assert con.execute("SELECT key FROM sync_errors").fetchone()[0] == "PROJ-1"


def test_second_sync_does_not_refetch_unchanged_issues():
    """Verify the fetch rule's headline property: unchanged remote issues are not
    refetched on a second sync."""
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")

    issue_fetch_count = [0]

    class CountingJira:
        def search(self, jql):
            yield "PROJ-1", "u1"
            yield "PROJ-2", "u2"

        def issue(self, key, fields):
            issue_fetch_count[0] += 1
            return {"key": key, "fields": {"updated": "u1" if key == "PROJ-1" else "u2"}}

        def fields(self):
            return []

        def statuses(self):
            return []

    urd.sync(con, CountingJira())
    assert issue_fetch_count[0] == 2
    issue_fetch_count[0] = 0

    # Second sync with unchanged remote list should not refetch anything
    urd.sync(con, CountingJira())
    assert issue_fetch_count[0] == 0


def test_resumable_backfill_after_error():
    """After a failed issue, re-running fetches only the failed one and clears
    its error entry."""
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")

    class Flaky:
        def __init__(self, fail_on=None):
            self.fail_on = fail_on

        def search(self, jql):
            yield "PROJ-1", "u1"
            yield "PROJ-2", "u2"

        def issue(self, key, fields):
            if key == self.fail_on:
                raise SystemExit("boom")
            return {"key": key, "fields": {"updated": "u1" if key == "PROJ-1" else "u2"}}

        def fields(self):
            return []

        def statuses(self):
            return []

    # First sync: PROJ-1 fails
    urd.sync(con, Flaky(fail_on="PROJ-1"))
    assert [r[0] for r in con.execute("SELECT key FROM raw_issues").fetchall()] == ["PROJ-2"]
    assert con.execute("SELECT key FROM sync_errors").fetchone()[0] == "PROJ-1"

    # Second sync: PROJ-1 succeeds, error is cleared
    urd.sync(con, Flaky(fail_on=None))
    raw = sorted([r[0] for r in con.execute("SELECT key FROM raw_issues").fetchall()])
    assert raw == ["PROJ-1", "PROJ-2"]
    assert con.execute("SELECT count(*) FROM sync_errors").fetchone()[0] == 0


def test_bare_sync_after_first_run_keeps_config():
    """A bare `urd sync` after a first run with flags still knows the site,
    project and window."""
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")

    class FakeJira:
        def search(self, jql):
            # Verify the JQL includes the stored project and earliest_since
            assert "project in (PROJ)" in jql
            assert 'updated >= "2026-01-01"' in jql
            return []

        def fields(self):
            return []

        def statuses(self):
            return []

    urd.sync(con, FakeJira())
    # If we get here without assertion error, the test passed


def test_malformed_issue_response_is_recorded_not_fatal():
    """An issue response with no fields key (empty 200 body or malformed JSON)
    is recorded in sync_errors and the run continues."""
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")

    class MalformedResponseJira:
        def search(self, jql):
            yield "PROJ-1", "u1"
            yield "PROJ-2", "u2"

        def issue(self, key, fields):
            if key == "PROJ-1":
                # Simulate empty 200 response or malformed JSON that loses fields
                return {"key": key}
            return {"key": key, "fields": {"updated": "u2"}}

        def fields(self):
            return []

        def statuses(self):
            return []

    urd.sync(con, MalformedResponseJira())
    # PROJ-2 succeeds despite PROJ-1 failing
    assert [r[0] for r in con.execute("SELECT key FROM raw_issues").fetchall()] == ["PROJ-2"]
    # PROJ-1 is recorded in sync_errors
    assert con.execute("SELECT key FROM sync_errors").fetchone()[0] == "PROJ-1"
    # last_sync_at is still written
    assert urd.load_scope(con)["last_sync_at"] is not None


def test_json_array_issue_response_is_recorded_not_fatal():
    """A JSON array body from /issue/KEY (not a dict) is recorded in sync_errors
    and the run continues, not a traceback."""
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")

    class ArrayResponseOpener:
        def __call__(self, url, headers):
            if "/issue/PROJ-1" in url:
                # Return a JSON array instead of object
                return 200, json.dumps(["item1", "item2"]).encode()
            if "/issue/PROJ-2" in url:
                return 200, json.dumps({"key": "PROJ-2", "fields": {"updated": "u2"}}).encode()
            if "/search" in url:
                return 200, json.dumps({
                    "issues": [
                        {"key": "PROJ-1", "fields": {"updated": "u1"}},
                        {"key": "PROJ-2", "fields": {"updated": "u2"}},
                    ],
                    "isLast": True,
                }).encode()
            return 200, b"{}"

    jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=ArrayResponseOpener())

    class Syncer:
        def search(self, jql):
            yield "PROJ-1", "u1"
            yield "PROJ-2", "u2"

        def issue(self, key, fields):
            return jira.issue(key, fields)

        def fields(self):
            return []

        def statuses(self):
            return []

    urd.sync(con, Syncer())
    # PROJ-2 succeeds despite PROJ-1 returning an array
    assert [r[0] for r in con.execute("SELECT key FROM raw_issues").fetchall()] == ["PROJ-2"]
    # PROJ-1 is recorded in sync_errors
    error_row = con.execute("SELECT error FROM sync_errors WHERE key = 'PROJ-1'").fetchone()
    assert error_row is not None
    assert "expected a JSON object, got list" in error_row[0]
    # last_sync_at is still written
    assert urd.load_scope(con)["last_sync_at"] is not None


def test_lookups_and_sync_timestamp_are_written():
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")

    class LookupJira:
        def search(self, jql):
            return iter(())

        def issue(self, key, fields):
            raise AssertionError("nothing is in scope, so no issue should be fetched")

        def fields(self):
            return [{"id": "customfield_20001", "name": "Story Points"},
                    {"id": "customfield_20002", "name": "Sprint"}]

        def statuses(self):
            # The second one has no statusCategory, which the real API does for
            # some statuses and which the code has to store as NULL.
            return [{"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                    {"name": "Odd", "statusCategory": None}]

    urd.sync(con, LookupJira())
    field = con.execute("SELECT id FROM fields WHERE name = 'Story Points'").fetchone()[0]
    assert field == "customfield_20001"
    status = con.execute("SELECT category FROM statuses WHERE name = 'In Progress'").fetchone()[0]
    assert status == "indeterminate"
    odd = con.execute("SELECT category FROM statuses WHERE name = 'Odd'").fetchone()[0]
    assert odd is None
    assert urd.load_scope(con)["last_sync_at"] is not None


def test_sync_errors_are_pruned_for_keys_leaving_scope():
    """Keys that have left the scope are removed from sync_errors, but errors
    for keys still in scope survive the prune even if not refetched."""
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.atlassian.net", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")

    class FirstSync:
        def search(self, jql):
            yield "PROJ-1", "u1"
            yield "PROJ-2", "u2"
            yield "PROJ-3", "u3"

        def issue(self, key, fields):
            if key in ("PROJ-2", "PROJ-3"):
                raise SystemExit(f"{key} failed")
            return {"key": key, "fields": {"updated": "u1"}}

        def fields(self):
            return []

        def statuses(self):
            return []

    # First sync: PROJ-2 and PROJ-3 fail, PROJ-1 succeeds
    urd.sync(con, FirstSync())
    # Manually insert PROJ-2 and PROJ-3 into raw_issues so they won't be wanted
    # in the second sync (same timestamp means no fetch)
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET updated = excluded.updated",
        ["PROJ-2", "u2", urd._now(), "{}"],
    )
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET updated = excluded.updated",
        ["PROJ-3", "u3", urd._now(), "{}"],
    )
    # Verify errors exist
    errors = con.execute("SELECT key FROM sync_errors ORDER BY key").fetchall()
    assert [r[0] for r in errors] == ["PROJ-2", "PROJ-3"]

    # Second sync: PROJ-3 leaves scope, PROJ-2 and PROJ-1 stay with unchanged timestamps
    class SecondSync:
        def search(self, jql):
            yield "PROJ-1", "u1"  # unchanged
            yield "PROJ-2", "u2"  # unchanged, still in scope

        def issue(self, key, fields):
            # Nothing should be fetched; all are unchanged
            raise AssertionError(f"unexpected fetch of {key}")

        def fields(self):
            return []

        def statuses(self):
            return []

    urd.sync(con, SecondSync())
    # PROJ-3 error should be pruned (out of scope), PROJ-2 error should survive
    # (still in scope, not refetched)
    errors = con.execute("SELECT key FROM sync_errors ORDER BY key").fetchall()
    assert [r[0] for r in errors] == ["PROJ-2"]


def test_urd_email_is_used_when_no_flag_is_given():
    """Precedence is flag, then environment, then stored scope. This has to go
    through main(), or the test just re-computes the expression it is checking."""
    db = _tmpdb()
    con = urd.open_db(db)
    urd.save_scope(con, site="example.atlassian.net", email="stored@example.com",
                   project="PROJ", earliest_since="2026-01-01")
    con.close()

    seen = {}

    class CaptureJira:
        def __init__(self, site, email, token, opener=None):
            seen["email"] = email

        def search(self, jql):
            return iter(())

        def fields(self):
            return []

        def statuses(self):
            return []

    real_jira, real_token = urd.Jira, urd.token
    real_env = os.environ.get("URD_EMAIL")
    urd.Jira, urd.token = CaptureJira, lambda env=None: "token"
    os.environ["URD_EMAIL"] = "env@example.com"
    try:
        # Test 1: environment beats stored scope
        urd.main(["--db", db, "sync"])
        assert seen["email"] == "env@example.com"

        # Test 2: flag beats environment
        seen["email"] = None
        urd.main(["--db", db, "sync", "--email", "flag@example.com"])
        assert seen["email"] == "flag@example.com"
    finally:
        urd.Jira, urd.token = real_jira, real_token
        if real_env is None:
            os.environ.pop("URD_EMAIL", None)
        else:
            os.environ["URD_EMAIL"] = real_env


def test_resolve_field_finds_story_points_by_name():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    assert urd.resolve_field(con, "Story Points") == "customfield_20001"


def test_derive_issues_flattens_every_fixture():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "skipped_progress", "two_sprints")
    assert urd.derive_issues(con) == 3
    row = con.execute(
        "SELECT key, project, type, status, status_category, assignee_id, reporter_id, "
        "created, updated, resolved, story_points, timespent_s, parent, "
        "fix_versions, labels, components FROM issues WHERE key = 'PROJ-1'"
    ).fetchone()
    assert row == (
        "PROJ-1", "PROJ", "Story", "Done", "done", "acct-2", "acct-1",
        datetime(2026, 1, 5, 9, 0, 0), datetime(2026, 1, 20, 17, 0, 0),
        datetime(2026, 1, 20, 17, 0, 0), 5.0, 3600, "PROJ-100",
        ["R1"], ["infra"], ["TEAM"],
    )


def test_derive_issues_tolerates_missing_optional_fields():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "skipped_progress")
    urd.derive_issues(con)
    row = con.execute(
        "SELECT assignee_id, story_points, parent, fix_versions FROM issues WHERE key = 'PROJ-2'"
    ).fetchone()
    assert row == (None, None, None, [])


def test_people_are_discovered_from_the_data_not_a_roster():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "two_sprints")
    urd.derive_issues(con)
    names = {r[0] for r in con.execute("SELECT display_name FROM people").fetchall()}
    assert {"Alder", "Birch", "Cedar"} <= names


def test_null_rate_is_measured_for_optional_fields():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "skipped_progress")
    urd.derive_issues(con)
    rate = con.execute("SELECT null_rate FROM fields WHERE name = 'Story Points'").fetchone()[0]
    assert rate == 0.5


def test_resolve_field_returns_none_for_missing_field():
    """When the instance has no Story Points field, resolve_field returns None."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    # Delete the Story Points field to simulate it not existing on the instance
    con.execute("DELETE FROM fields WHERE name = 'Story Points'")
    assert urd.resolve_field(con, "Story Points") is None


def test_derive_issues_completes_with_no_story_points_field():
    """Even with no Story Points field, derive_issues must complete and leave
    story_points null on every row, not raise."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "skipped_progress")
    # Delete the Story Points field to simulate it not existing
    con.execute("DELETE FROM fields WHERE name = 'Story Points'")
    count = urd.derive_issues(con)
    assert count == 2
    rows = con.execute("SELECT story_points FROM issues").fetchall()
    assert all(r[0] is None for r in rows)


def test_derive_issues_is_idempotent():
    """Running derive_issues twice must leave the same row count, not double it."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "skipped_progress", "two_sprints")
    count1 = urd.derive_issues(con)
    count2 = urd.derive_issues(con)
    assert count1 == count2 == 3
    # Verify the final row count is still 3, not 6
    total_rows = con.execute("SELECT count(*) FROM issues").fetchone()[0]
    assert total_rows == 3


def test_stored_timestamps_do_not_depend_on_the_machines_timezone():
    """The issues table's TIMESTAMP columns are naive, so _ts must hand DuckDB a
    naive UTC value. An aware one gets shifted to local wall time, which makes any
    span crossing a DST change an hour wrong and makes CI disagree with a laptop."""
    con = urd.open_db(_tmpdb())
    con.execute("SET TimeZone='America/Los_Angeles'")
    load_fixtures(con, "reopened")
    urd.derive_issues(con)
    created = con.execute("SELECT created FROM issues WHERE key = 'PROJ-1'").fetchone()[0]
    # The fixture has "created": "2026-01-05T09:00:00.000+0000"
    # This should be stored as naive UTC 09:00:00, not shifted to local wall time
    # even when DuckDB's timezone is set to Los_Angeles
    assert created == datetime(2026, 1, 5, 9, 0, 0)


def test_ts_normalizes_all_input_formats():
    """Verify _ts handles +0000, Z, -0500, None, and empty string."""
    # Test 1: Standard Jira format with +0000
    ts1 = urd._ts("2026-01-05T09:00:00.000+0000")
    assert ts1 == datetime(2026, 1, 5, 9, 0, 0)

    # Test 2: Z format (Sprint field)
    ts2 = urd._ts("2026-01-05T09:00:00.000Z")
    assert ts2 == datetime(2026, 1, 5, 9, 0, 0)

    # Test 3: Negative offset that gives the same instant as +0000
    # 13:00:00-0500 = 18:00:00+0000 = 18:00:00 UTC
    ts3 = urd._ts("2026-01-05T13:00:00.000-0500")
    assert ts3 == datetime(2026, 1, 5, 18, 0, 0)

    # Test 4: None input
    ts4 = urd._ts(None)
    assert ts4 is None

    # Test 5: Empty string
    ts5 = urd._ts("")
    assert ts5 is None


def test_derive_issues_returns_zero_on_empty_input():
    """Running derive_issues on an empty raw_issues table must return 0, not raise."""
    con = urd.open_db(_tmpdb())
    con.execute("INSERT INTO fields VALUES ('customfield_20001', 'Story Points', NULL)")
    con.execute("INSERT INTO fields VALUES ('customfield_20002', 'Sprint', NULL)")
    count = urd.derive_issues(con)
    assert count == 0


def test_missing_assignee_and_reporter_does_not_abort():
    """An issue with no assignee must derive without aborting and write a null
    assignee_id. The fixture has a reporter, so the if people: guard is not tested here."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "skipped_progress")
    urd.derive_issues(con)
    # Verify the row was written with null assignee_id despite null assignee
    assert con.execute("SELECT assignee_id FROM issues WHERE key = 'PROJ-2'").fetchone()[0] is None


def test_missing_accountid_is_skipped():
    """A person object without accountId must not abort derive or leave the run
    half-done in the people table."""
    con = urd.open_db(_tmpdb())
    # Insert a fixture manually with a malformed person object
    raw = json.dumps({
        "key": "PROJ-4",
        "fields": {
            "summary": "Malformed person",
            "issuetype": {"name": "Story"},
            "status": {"name": "Done", "statusCategory": {"key": "done"}},
            "assignee": {"displayName": "NoId", "avatarUrl": "..."},
            "reporter": {"accountId": "acct-1", "displayName": "Alder"},
            "created": "2026-01-05T09:00:00.000+0000",
            "updated": "2026-01-05T09:00:00.000+0000",
            "resolutiondate": None,
            "timespent": None,
            "parent": None,
            "fixVersions": [],
            "labels": [],
            "components": [{"name": "TEAM"}],
            "customfield_20001": None,
            "customfield_20002": None,
        },
        "changelog": {"histories": []},
    })
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        ["PROJ-4", "2026-01-05T09:00:00.000+0000", urd._now(), raw],
    )
    con.execute("INSERT INTO fields VALUES ('customfield_20001', 'Story Points', NULL)")
    con.execute("INSERT INTO fields VALUES ('customfield_20002', 'Sprint', NULL)")
    urd.save_scope(con, status_order="To Do,Done", start_status="To Do", review_status="Done")

    # This must not raise, even though assignee has no accountId
    count = urd.derive_issues(con)
    assert count == 1
    # Verify the issue was written
    assert con.execute("SELECT count(*) FROM issues WHERE key = 'PROJ-4'").fetchone()[0] == 1
    # Verify only the valid person (Alder) was inserted
    people = sorted([r[0] for r in con.execute("SELECT display_name FROM people").fetchall()])
    assert people == ["Alder"]


def test_people_idempotence():
    """Running derive_issues twice must leave the people table unchanged, proving
    that CREATE OR REPLACE TABLE empties it each run and repopulates from the data."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "two_sprints")
    urd.derive_issues(con)
    count1 = con.execute("SELECT count(*) FROM people").fetchone()[0]
    urd.derive_issues(con)
    count2 = con.execute("SELECT count(*) FROM people").fetchone()[0]
    assert count1 == count2


def test_project_comes_from_the_key_not_a_constant():
    """--project takes a comma separated list, so rows can carry different project
    values and this column is what separates them. No fixture uses a second key
    prefix, so the constant-folding mutation survives without this."""
    con = urd.open_db(_tmpdb())
    con.execute("INSERT INTO raw_issues VALUES ('OTHER-1', 'u', ?, ?)",
                [urd._now(), json.dumps({"fields": {}})])
    urd.derive_issues(con)
    assert con.execute("SELECT project FROM issues").fetchone() == ("OTHER",)


def test_changes_covers_every_field_not_only_status():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    urd.derive_issues(con)
    urd.derive_changes(con)
    fields = {r[0] for r in con.execute("SELECT DISTINCT field FROM changes").fetchall()}
    assert fields == {"status", "assignee"}


def test_assignee_changes_keep_both_the_id_and_the_name():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    urd.derive_issues(con)
    urd.derive_changes(con)
    row = con.execute(
        "SELECT from_id, from_str, to_id, to_str FROM changes WHERE field = 'assignee'"
    ).fetchone()
    assert row == ("acct-1", "Alder", "acct-2", "Birch")


def test_status_durations_cover_the_whole_life_of_the_issue():
    """Every moment from creation to now sits in exactly one span, including the
    stretch before the first transition, which `transitions` alone cannot see.
    A sum cannot see a boundary error: every interior boundary appears once as
    a left_at and once as an entered, so the arithmetic cancels. Assert the
    exact figure, and assert every individual span, not just the total."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "skipped_progress", "two_sprints")
    urd.derive_issues(con)
    urd.derive_changes(con)

    # Exact figure, not a sum that hides boundary errors
    gaps = con.execute(
        """
        SELECT i.key,
               date_diff('second', i.created, now() AT TIME ZONE 'UTC')
                   - sum(date_diff('second', d.entered, d.left_at)) AS drift
        FROM issues i JOIN status_durations d USING (key)
        GROUP BY i.key, i.created HAVING drift <> 0
        """
    ).fetchall()
    assert gaps == []

    # No span may run backwards. This is what a sum hides.
    backwards = con.execute(
        "SELECT key, status, entered, left_at FROM status_durations WHERE left_at < entered"
    ).fetchall()
    assert backwards == []

    # Assert per-span for reopened.json, whose transitions are known
    reopened_spans = con.execute(
        """
        SELECT status, date_diff('second', entered, left_at) AS seconds
        FROM status_durations WHERE key = 'PROJ-1' ORDER BY entered
        """
    ).fetchall()
    # Hand-computed from fixture: Jan5-6 (To Do), 6-8 (IP), 8-9 (Rev), 9-15 (IP), 15-20.17 (Rev)
    expected_closed = [
        ("To Do", 86400),
        ("In Progress", 172800),
        ("Review", 86400),
        ("In Progress", 518400),
        ("Review", 460800),
    ]
    assert reopened_spans[:-1] == expected_closed, f"got {reopened_spans[:-1]}"
    assert reopened_spans[-1][0] == "Done"  # final span is open


def test_status_durations_columns_are_both_naive():
    """DuckDB's now() is TIMESTAMPTZ, and a UNION ALL would silently make left_at
    a different type from entered. That shifts every open span by the machine's
    offset while leaving the span sum exactly right, so no arithmetic assertion
    can see it: assert the type instead."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    urd.derive_issues(con)
    urd.derive_changes(con)
    types = {r[0]: r[1] for r in con.execute("DESCRIBE status_durations").fetchall()}
    assert types["entered"] == "TIMESTAMP", types["entered"]
    assert types["left_at"] == "TIMESTAMP", types["left_at"]

    # Assert the open span's left_at is within 2 seconds of now, not shifted by timezone
    open_span = con.execute(
        """
        SELECT date_diff('second', now() AT TIME ZONE 'UTC', left_at)
        FROM status_durations WHERE key = 'PROJ-1' AND status = 'Done'
        """
    ).fetchone()[0]
    assert abs(open_span) <= 2, f"open span left_at off by {open_span} seconds"


def test_the_first_span_is_the_status_the_issue_started_in():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    urd.derive_issues(con)
    urd.derive_changes(con)
    first = con.execute(
        "SELECT status FROM status_durations WHERE key = 'PROJ-1' ORDER BY entered LIMIT 1"
    ).fetchone()[0]
    assert first == "To Do"


def test_an_issue_that_never_moved_still_has_one_span():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    con.execute(
        "INSERT INTO raw_issues VALUES ('PROJ-4', 'u', ?, ?)",
        [urd._now(), json.dumps({"key": "PROJ-4", "fields": {
            "issuetype": {"name": "Task"},
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "created": "2026-03-01T09:00:00.000+0000",
            "updated": "2026-03-01T09:00:00.000+0000",
            "fixVersions": [], "labels": [], "components": []}})],
    )
    urd.derive_issues(con)
    urd.derive_changes(con)
    spans = con.execute("SELECT status FROM status_durations WHERE key = 'PROJ-4'").fetchall()
    assert spans == [("To Do",)]


def test_changelog_authors_join_people():
    con = urd.open_db(_tmpdb())
    # Insert an issue with a changelog author who is NOT an assignee or reporter
    con.execute(
        "INSERT INTO raw_issues VALUES ('PROJ-5', 'u', ?, ?)",
        [urd._now(), json.dumps({"key": "PROJ-5", "fields": {
            "issuetype": {"name": "Task"},
            "status": {"name": "Done", "statusCategory": {"key": "done"}},
            "assignee": None,
            "reporter": {"accountId": "acct-1", "displayName": "Alder"},
            "created": "2026-03-01T09:00:00.000+0000",
            "updated": "2026-03-02T09:00:00.000+0000",
            "fixVersions": [], "labels": [], "components": []},
            "changelog": {"histories": [{
                "created": "2026-03-02T09:00:00.000+0000",
                "author": {"accountId": "acct-99", "displayName": "Unknown"},
                "items": [{
                    "field": "status", "from": "1", "fromString": "To Do",
                    "to": "5", "toString": "Done"
                }]
            }]}
        })],
    )
    con.execute("INSERT INTO fields VALUES ('customfield_20001', 'Story Points', NULL)")
    con.execute("INSERT INTO fields VALUES ('customfield_20002', 'Sprint', NULL)")
    for status, category in (
        ("To Do", "new"), ("Done", "done"),
    ):
        con.execute("INSERT INTO statuses VALUES (?, ?)", [status, category])
    urd.save_scope(con, status_order="To Do,Done", start_status="To Do", review_status="Done")
    urd.derive_issues(con)
    urd.derive_changes(con)
    # Verify the changelog author (acct-99) was added to people even though
    # not assignee/reporter
    row_count = con.execute(
        "SELECT count(*) FROM people WHERE account_id = 'acct-99'"
    ).fetchone()[0]
    assert row_count == 1

    # Re-run derive_issues to verify people persists across functions
    urd.derive_issues(con)
    row_count_after = con.execute(
        "SELECT count(*) FROM people WHERE account_id = 'acct-99'"
    ).fetchone()[0]
    assert row_count_after == 1, "acct-99 should persist after re-derive_issues"

    # Verify acct-99 is still joinable from changes.author_id
    joined = con.execute(
        "SELECT count(*) FROM changes WHERE author_id = 'acct-99' "
        "AND EXISTS (SELECT 1 FROM people WHERE account_id = 'acct-99')"
    ).fetchone()[0]
    assert joined > 0, "acct-99 should be joinable from changes"


def test_same_timestamp_transitions_use_history_id_tiebreaker():
    """Two transitions at the same timestamp must not flip initial status."""
    con = urd.open_db(_tmpdb())
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "PROJ-5",
            "u",
            urd._now(),
            json.dumps({
                "key": "PROJ-5",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "status": {"name": "Done", "statusCategory": {"key": "done"}},
                    "created": "2026-03-01T09:00:00.000+0000",
                    "updated": "2026-03-02T09:00:00.000+0000",
                },
                "changelog": {
                    "histories": [
                        {
                            "id": "101",
                            "created": "2026-03-01T10:00:00.000+0000",
                            "author": {"accountId": "a2", "displayName": "B"},
                            "items": [{"field": "status", "from": "3", "fromString": "In Progress",
                                      "to": "5", "toString": "Done"}],
                        },
                        {
                            "id": "100",
                            "created": "2026-03-01T10:00:00.000+0000",
                            "author": {"accountId": "a1", "displayName": "A"},
                            "items": [{"field": "status", "from": "1", "fromString": "To Do",
                                      "to": "3", "toString": "In Progress"}],
                        },
                    ]
                },
            }),
        ],
    )
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20001", "Story Points"])
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20002", "Sprint"])
    for status, category in (("To Do", "new"), ("In Progress", "indeterminate"), ("Done", "done")):
        con.execute("INSERT INTO statuses VALUES (?, ?)", [status, category])
    urd.save_scope(
        con, status_order="To Do,In Progress,Done", start_status="In Progress", review_status="Done"
    )
    urd.derive_issues(con)
    urd.derive_changes(con)
    # Check the first status in the durations, which must be To Do
    first_status = con.execute(
        "SELECT status FROM status_durations WHERE key = 'PROJ-5' ORDER BY entered LIMIT 1"
    ).fetchone()[0]
    assert first_status == "To Do"
    # Check the open span status, which must be Done (not In Progress from wrong LEAD order)
    open_status = con.execute(
        "SELECT status FROM status_durations WHERE key = 'PROJ-5' ORDER BY left_at DESC LIMIT 1"
    ).fetchone()[0]
    assert open_status == "Done"


def test_first_transition_predating_created_does_not_produce_negative_span():
    """A transition timestamped before creation must clamp to creation."""
    con = urd.open_db(_tmpdb())
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "PROJ-6",
            "u",
            urd._now(),
            json.dumps({
                "key": "PROJ-6",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                    "created": "2026-01-10T09:00:00.000+0000",
                    "updated": "2026-01-20T09:00:00.000+0000",
                },
                "changelog": {
                    "histories": [
                        {
                            "id": "1",
                            "created": "2026-01-05T09:00:00.000+0000",
                            "author": {"accountId": "a1", "displayName": "A"},
                            "items": [{"field": "status", "from": "1", "fromString": "To Do",
                                      "to": "3", "toString": "In Progress"}],
                        },
                    ]
                },
            }),
        ],
    )
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20001", "Story Points"])
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20002", "Sprint"])
    for status, category in (("To Do", "new"), ("In Progress", "indeterminate")):
        con.execute("INSERT INTO statuses VALUES (?, ?)", [status, category])
    urd.save_scope(
        con, status_order="To Do,In Progress", start_status="In Progress",
        review_status="In Progress"
    )
    urd.derive_issues(con)
    urd.derive_changes(con)
    # All spans must be non-negative
    backwards = con.execute(
        "SELECT key, status, entered, left_at FROM status_durations "
        "WHERE key = 'PROJ-6' AND left_at < entered"
    ).fetchall()
    assert backwards == []
    # No span may start before the issue was created
    early = con.execute(
        "SELECT count(*) FROM status_durations d JOIN issues i USING (key) "
        "WHERE d.key = 'PROJ-6' AND d.entered < i.created"
    ).fetchone()[0]
    assert early == 0


def test_derive_changes_return_value_matches_rows_inserted():
    """derive_changes must return the count of rows inserted."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    urd.derive_issues(con)
    count = urd.derive_changes(con)
    actual_rows = con.execute("SELECT count(*) FROM changes").fetchone()[0]
    assert count == actual_rows


def test_derive_changes_handles_empty_changelog():
    """derive_changes must return 0 and not raise when an issue has no changelog."""
    con = urd.open_db(_tmpdb())
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "PROJ-7",
            "u",
            urd._now(),
            json.dumps({
                "key": "PROJ-7",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    "created": "2026-03-01T09:00:00.000+0000",
                    "updated": "2026-03-02T09:00:00.000+0000",
                }
            }),
        ],
    )
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20001", "Story Points"])
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20002", "Sprint"])
    for status, category in (("To Do", "new"),):
        con.execute("INSERT INTO statuses VALUES (?, ?)", [status, category])
    urd.save_scope(con, status_order="To Do", start_status="To Do", review_status="To Do")
    urd.derive_issues(con)
    count = urd.derive_changes(con)
    assert count == 0


def test_people_persists_across_derive_functions():
    """A changelog-only author must survive re-running derive_issues alone."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    urd.derive_issues(con)
    urd.derive_changes(con)
    # Verify the author is present and can be joined from changes
    count_before = con.execute("SELECT count(*) FROM people").fetchone()[0]
    assert count_before > 0

    # Re-run derive_issues to verify it does not drop people
    urd.derive_issues(con)
    count_after = con.execute("SELECT count(*) FROM people").fetchone()[0]
    assert count_after == count_before, "people table was not preserved across derives"

    # Verify people can still be joined from changes
    joined = con.execute(
        "SELECT count(*) FROM changes c JOIN people p ON c.author_id = p.account_id"
    ).fetchone()[0]
    assert joined > 0


def test_person_display_name_propagates_on_rename():
    """A renamed person must update their name on re-derive."""
    con = urd.open_db(_tmpdb())
    # Insert an issue with an author
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "TEST-1",
            "u",
            urd._now(),
            json.dumps({
                "key": "TEST-1",
                "fields": {
                    "issuetype": {"name": "Story"},
                    "status": {"name": "Done", "statusCategory": {"key": "done"}},
                    "created": "2026-03-01T09:00:00.000+0000",
                    "updated": "2026-03-02T09:00:00.000+0000",
                },
                "changelog": {
                    "histories": [{
                        "id": "100",
                        "created": "2026-03-01T10:00:00.000+0000",
                        "author": {"accountId": "renamed", "displayName": "Old Name"},
                        "items": [{"field": "status", "from": "1", "fromString": "To Do",
                                  "to": "5", "toString": "Done"}]
                    }]
                }
            }),
        ],
    )
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20001", "Story Points"])
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20002", "Sprint"])
    for status, category in [("To Do", "new"), ("Done", "done")]:
        con.execute("INSERT INTO statuses VALUES (?, ?)", [status, category])
    urd.save_scope(con, status_order="To Do,Done", start_status="To Do", review_status="Done")

    urd.derive_issues(con)
    urd.derive_changes(con)
    name_before = con.execute(
        "SELECT display_name FROM people WHERE account_id = 'renamed'"
    ).fetchone()[0]
    assert name_before == "Old Name"

    # Change the author's name in raw_issues and re-derive
    con.execute(
        "UPDATE raw_issues SET json = ? WHERE key = 'TEST-1'",
        [json.dumps({
            "key": "TEST-1",
            "fields": {
                "issuetype": {"name": "Story"},
                "status": {"name": "Done", "statusCategory": {"key": "done"}},
                "created": "2026-03-01T09:00:00.000+0000",
                "updated": "2026-03-02T09:00:00.000+0000",
            },
            "changelog": {
                "histories": [{
                    "id": "100",
                    "created": "2026-03-01T10:00:00.000+0000",
                    "author": {"accountId": "renamed", "displayName": "New Name"},
                    "items": [{"field": "status", "from": "1", "fromString": "To Do",
                              "to": "5", "toString": "Done"}]
                }]
            }
        })],
    )

    urd.derive_changes(con)
    name_after = con.execute(
        "SELECT display_name FROM people WHERE account_id = 'renamed'"
    ).fetchone()[0]
    assert name_after == "New Name", f"Expected 'New Name', got '{name_after}'"


def test_assignee_display_name_refresh_on_derive_issues():
    """A renamed assignee must update their name on re-derive_issues."""
    con = urd.open_db(_tmpdb())
    # Insert an issue with an assignee
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "ASSIGN-1",
            "u",
            urd._now(),
            json.dumps({
                "key": "ASSIGN-1",
                "fields": {
                    "issuetype": {"name": "Story"},
                    "status": {"name": "Done", "statusCategory": {"key": "done"}},
                    "assignee": {"accountId": "assignee-id", "displayName": "Old Assignee Name"},
                    "reporter": {"accountId": "reporter-id", "displayName": "Reporter"},
                    "created": "2026-03-01T09:00:00.000+0000",
                    "updated": "2026-03-02T09:00:00.000+0000",
                },
                "changelog": {"histories": []}
            }),
        ],
    )
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20001", "Story Points"])
    con.execute("INSERT INTO fields VALUES (?, ?, NULL)", ["customfield_20002", "Sprint"])
    for status, category in [("Done", "done")]:
        con.execute("INSERT INTO statuses VALUES (?, ?)", [status, category])
    urd.save_scope(con, status_order="Done", start_status="Done", review_status="Done")

    urd.derive_issues(con)
    name_before = con.execute(
        "SELECT display_name FROM people WHERE account_id = 'assignee-id'"
    ).fetchone()[0]
    assert name_before == "Old Assignee Name"

    # Change the assignee's name in raw_issues and re-derive
    con.execute(
        "UPDATE raw_issues SET json = ? WHERE key = 'ASSIGN-1'",
        [json.dumps({
            "key": "ASSIGN-1",
            "fields": {
                "issuetype": {"name": "Story"},
                "status": {"name": "Done", "statusCategory": {"key": "done"}},
                "assignee": {"accountId": "assignee-id", "displayName": "New Assignee Name"},
                "reporter": {"accountId": "reporter-id", "displayName": "Reporter"},
                "created": "2026-03-01T09:00:00.000+0000",
                "updated": "2026-03-02T09:00:00.000+0000",
            },
            "changelog": {"histories": []}
        })],
    )

    urd.derive_issues(con)
    name_after = con.execute(
        "SELECT display_name FROM people WHERE account_id = 'assignee-id'"
    ).fetchone()[0]
    assert name_after == "New Assignee Name", f"Expected 'New Assignee Name', got '{name_after}'"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
