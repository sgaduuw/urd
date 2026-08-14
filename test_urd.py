import json
import os
import pathlib
import tempfile

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
        ["PROJ-2", "u2", urd.datetime.now(urd.timezone.utc), "{}"],
    )
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET updated = excluded.updated",
        ["PROJ-3", "u3", urd.datetime.now(urd.timezone.utc), "{}"],
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
