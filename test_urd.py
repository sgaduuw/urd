import decimal
import json
import os
import pathlib
import re
import tempfile
from datetime import datetime

import charts as chart_specs
import render
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


def _derived(*fixtures):
    con = urd.open_db(_tmpdb())
    load_fixtures(con, *fixtures)
    scope = urd.load_scope(con)
    urd.derive(con, scope["status_order"], scope["start_status"], scope["review_status"])
    return con


def test_rework_counts_only_backward_transitions():
    """PROJ-1 goes Review back to In Progress exactly once."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    assert con.execute("SELECT count(*) FROM rework").fetchone()[0] == 1
    assert con.execute("SELECT key, from_status, to_status FROM rework").fetchone() == (
        "PROJ-1", "Review", "In Progress",
    )


def test_statuses_outside_the_order_are_excluded_from_rework():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    urd.derive(con, "To Do,In Progress,Done", "In Progress", "Review")
    unknown = con.execute(
        "SELECT DISTINCT to_status FROM transitions t "
        "WHERE to_status NOT IN (SELECT status FROM status_order)"
    ).fetchall()
    assert unknown == [("Review",)]
    # Transitions touching an unlisted status are excluded from rework, so any transition
    # involving Review is dropped by the INNER JOIN and not counted as rework.
    assert con.execute("SELECT count(*) FROM rework").fetchone()[0] == 0


def test_cycle_time_starts_at_the_start_status():
    """PROJ-1 entered In Progress on the 6th and resolved on the 20th."""
    con = _derived("reopened")
    query = "SELECT started, resolved, cycle_days FROM cycle_times WHERE key = 'PROJ-1'"
    row = con.execute(query).fetchone()
    started, resolved, days = row
    # Verify it started on Jan 6 (not Jan 9, which would be if we used max instead of min)
    assert started == datetime(2026, 1, 6, 9, 0, 0), f"Expected start Jan 6, got {started}"
    # Verify the cycle days are about 14.33 days (from Jan 6 to Jan 20 at 17:00)
    assert 14.0 < days < 14.5, f"Expected cycle_days ~14.33, got {days}"


def test_a_ticket_that_never_started_is_absent_from_cycle_times():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    keys = {r[0] for r in con.execute("SELECT key FROM cycle_times").fetchall()}
    assert keys == {"PROJ-1"}


def test_closures_are_transitions_into_a_done_category_status():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    keys = sorted(r[0] for r in con.execute("SELECT key FROM closures").fetchall())
    assert keys == ["PROJ-1", "PROJ-2"]


def test_derive_is_idempotent():
    con = _derived("reopened", "two_sprints")
    issues_before = con.execute("SELECT count(*) FROM issues").fetchone()[0]
    changes_before = con.execute("SELECT count(*) FROM changes").fetchone()[0]
    scope = urd.load_scope(con)
    urd.derive(con, scope["status_order"], scope["start_status"], scope["review_status"])
    issues_after = con.execute("SELECT count(*) FROM issues").fetchone()[0]
    changes_after = con.execute("SELECT count(*) FROM changes").fetchone()[0]
    assert issues_before == issues_after == 2, (
        f"issues count changed: {issues_before} -> {issues_after}"
    )
    assert changes_before == changes_after, (
        f"changes count changed: {changes_before} -> {changes_after}"
    )


def test_cycle_times_respects_configured_start_status():
    """Changing start_status must change which transitions count as the cycle start."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    # Derive with Review as start_status instead of In Progress
    urd.derive(con, "To Do,In Progress,Review,Done", "Review", "Done")
    row = con.execute("SELECT started, cycle_days FROM cycle_times WHERE key = 'PROJ-1'").fetchone()
    assert row is not None, "Expected cycle_times row for PROJ-1 with Review start_status"
    started, days = row
    # PROJ-1 first enters Review on Jan 8, resolves on Jan 20
    assert started == datetime(2026, 1, 8, 9, 0, 0), f"Expected start Jan 8, got {started}"
    # Cycle time should be from Jan 8 to Jan 20 at 17:00, about 12.33 days
    assert 12.0 < days < 13.0, f"Expected cycle_days ~12.33, got {days}"


def test_derive_reports_unknown_statuses_to_output():
    """Derive must report unknown statuses when they are found."""
    import io
    import sys as sys_module

    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")

    # Capture stdout
    old_stdout = sys_module.stdout
    sys_module.stdout = io.StringIO()

    try:
        urd.derive(con, "To Do,In Progress,Done", "In Progress", "Review")
        output = sys_module.stdout.getvalue()
    finally:
        sys_module.stdout = old_stdout

    # The output must mention that unknown statuses are excluded
    assert "statuses not in --status-order" in output, (
        f"Expected unknown status report in output, got: {output}"
    )
    assert "Review" in output, f"Expected 'Review' in output, got: {output}"
    assert "excluded" in output, f"Expected 'excluded' not 'ranked' in: {output}"


def test_main_wiring_honors_derive_arguments():
    """main() must pass derive arguments through and respect precedence over stored values."""
    db = _tmpdb()
    con = urd.open_db(db)
    load_fixtures(con, "reopened")
    con.close()

    # Call main with explicit arguments where at least status_order differs from fixtures
    # Fixtures store: status_order="To Do,In Progress,Review,Done" with start_status="In Progress"
    # We pass: status_order="To Do,Review,Done,In Progress" (reordered) with start_status="Done"
    result = urd.main(
        [
            "--db", db, "derive",
            "--status-order", "To Do,Review,Done,In Progress",
            "--start-status", "Done",
            "--review-status", "Review"
        ]
    )
    assert result == 0, "main() derive should return 0 on success"

    # Verify the config was saved with the provided values, not the fixtures' values
    con = urd.open_db(db)
    scope = urd.load_scope(con)
    assert scope["status_order"] == "To Do,Review,Done,In Progress", (
        f"--status-order flag should override fixtures; got {scope['status_order']}"
    )
    assert scope["start_status"] == "Done", (
        f"--start-status flag should override fixtures; got {scope['start_status']}"
    )
    assert scope["review_status"] == "Review", (
        f"--review-status flag should override fixtures; got {scope['review_status']}"
    )
    con.close()


def test_main_derive_uses_stored_config_when_no_arguments():
    """main() must use stored config when no derive flags are given, and must actually run."""
    db = _tmpdb()
    con = urd.open_db(db)
    load_fixtures(con, "reopened")
    con.close()

    # First call: set a specific config that differs from fixtures
    # Fixtures store: status_order="To Do,In Progress,Review,Done" with start_status="In Progress"
    # We set: status_order="To Do,Done,Review,In Progress" with start_status="Done"
    urd.main(
        [
            "--db", db, "derive",
            "--status-order", "To Do,Done,Review,In Progress",
            "--start-status", "Done",
            "--review-status", "Done"
        ]
    )

    # Between calls, drop the changes table to prove the second call rebuilds it
    con = urd.open_db(db)
    con.execute("DROP TABLE changes")
    con.close()

    # Second call with no flags should reuse stored config and rebuild changes table
    result = urd.main(["--db", db, "derive"])
    assert result == 0, "main() derive should succeed with stored config"

    # Verify derive actually ran by checking that changes table exists and is populated
    con = urd.open_db(db)
    changes_exists = con.execute(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='changes')"
    ).fetchone()[0]
    assert changes_exists, "derive should have rebuilt the changes table"
    changes_count = con.execute("SELECT count(*) FROM changes").fetchone()[0]
    assert changes_count > 0, "derive should have populated changes table"

    # Verify the config remained unchanged (not reset by second call)
    scope = urd.load_scope(con)
    assert scope["status_order"] == "To Do,Done,Review,In Progress", (
        f"Stored status_order should persist; got {scope['status_order']}"
    )
    assert scope["start_status"] == "Done", (
        f"Stored start_status should persist; got {scope['start_status']}"
    )
    assert scope["review_status"] == "Done", (
        f"Stored review_status should persist; got {scope['review_status']}"
    )
    con.close()


def test_metric_views_have_correct_timestamp_types():
    """All timestamp columns in metric views must be TIMESTAMP, not TIMESTAMPTZ."""
    con = _derived("reopened", "skipped_progress", "two_sprints")

    for view_name in ("closures", "cycle_times", "rework"):
        types = {name: dtype for name, dtype, *_ in con.execute(
            f"DESCRIBE {view_name}"
        ).fetchall()}

        if view_name == "closures":
            assert types["ts"] == "TIMESTAMP", (
                f"{view_name}.ts should be TIMESTAMP, got {types['ts']}"
            )
        elif view_name == "cycle_times":
            assert types["started"] == "TIMESTAMP", (
                f"{view_name}.started should be TIMESTAMP, got {types['started']}"
            )
        elif view_name == "rework":
            assert types["ts"] == "TIMESTAMP", (
                f"{view_name}.ts should be TIMESTAMP, got {types['ts']}"
            )


def test_no_bare_sql_now_anywhere():
    """DuckDB's now() is TIMESTAMPTZ. Column-type tests only see columns a view
    exposes, so a now() in a WHERE filter or a COALESCE fallback is invisible to
    them, and that is how Task 5's Critical entered. Require every SQL now() to
    be immediately followed by the UTC cast: a cast elsewhere on the line does
    not make an earlier occurrence safe."""
    source = (pathlib.Path(__file__).parent / "urd.py").read_text()
    # Find all bare now() not immediately followed by AT TIME ZONE 'UTC'
    # Negative lookahead (?!\s*AT TIME ZONE 'UTC') checks what follows right after
    bad = [m.start() for m in re.finditer(
        r"\bnow\(\)(?!\s*AT TIME ZONE 'UTC')",
        source
    )]
    # Also check for current_timestamp without the cast
    bad += [m.start() for m in re.finditer(r"\bcurrent_timestamp\b", source)]
    assert bad == [], [source[max(0, i - 60):i + 40] for i in bad]


def test_derive_rejects_duplicate_statuses():
    """Duplicate statuses in --status-order must be rejected before persistence."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    original_order = urd.load_scope(con)["status_order"]

    try:
        urd.derive(con, "To Do,To Do,In Progress,Done", "In Progress", "Done")
        raise AssertionError("Expected SystemExit for duplicate statuses")
    except SystemExit as e:
        assert "duplicate" in str(e).lower(), f"Error should mention duplicates: {e}"

    # Verify the bad config was NOT persisted - config unchanged
    scope = urd.load_scope(con)
    assert scope["status_order"] == original_order, "Bad config should not overwrite saved config"


def test_derive_rejects_empty_statuses():
    """Empty items in --status-order must be rejected before persistence."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    original_order = urd.load_scope(con)["status_order"]

    try:
        urd.derive(con, "To Do,,In Progress,Done", "In Progress", "Done")
        raise AssertionError("Expected SystemExit for empty status")
    except SystemExit as e:
        assert "empty" in str(e).lower(), f"Error should mention empty items: {e}"

    # Verify the bad config was NOT persisted - config unchanged
    scope = urd.load_scope(con)
    assert scope["status_order"] == original_order, "Bad config should not overwrite saved config"


def test_closures_author_id_has_values():
    """closures.author_id must have values, not be droppable. Task 9 consumes it."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    author_ids = con.execute("SELECT DISTINCT author_id FROM closures").fetchall()
    assert len(author_ids) > 0, "closures.author_id should contain author IDs"
    assert author_ids[0][0] is not None, "closures.author_id should not be all NULL"


def test_rework_author_id_has_values():
    """rework.author_id must have values, not be droppable. Task 9 consumes it."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    author_ids = con.execute("SELECT DISTINCT author_id FROM rework").fetchall()
    assert len(author_ids) > 0, "rework.author_id should contain author IDs"
    assert author_ids[0][0] is not None, "rework.author_id should not be all NULL"


def test_cycle_times_resolved_has_values():
    """cycle_times.resolved must have values, not be droppable. Task 9 consumes it."""
    con = _derived("reopened")
    resolved = con.execute("SELECT DISTINCT resolved FROM cycle_times").fetchall()
    assert len(resolved) > 0, "cycle_times.resolved should contain timestamps"
    assert resolved[0][0] is not None, "cycle_times.resolved should not be all NULL"


def test_closures_ts_values_are_real_timestamps():
    """closures.ts values must be real transition timestamps, not constants."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    ts_values = con.execute("SELECT DISTINCT ts FROM closures ORDER BY ts").fetchall()
    assert len(ts_values) >= 2, "Should have multiple distinct timestamps"
    # Timestamps should span a range, not all be identical
    min_ts, max_ts = ts_values[0][0], ts_values[-1][0]
    assert min_ts != max_ts, "closures.ts should have varying values, not a constant"


def test_rework_ts_values_are_real_timestamps():
    """rework.ts values must be real transition timestamps, not constants."""
    con = _derived("reopened")
    ts_values = con.execute("SELECT ts FROM rework WHERE key = 'PROJ-1'").fetchall()
    assert len(ts_values) == 1, "reopened fixture has exactly one rework transition"
    # PROJ-1 has one rework: Review -> In Progress on Jan 9 at 09:00:00
    ts = ts_values[0][0]
    assert ts == datetime(2026, 1, 9, 9, 0, 0), (
        f"rework.ts should be the transition timestamp (Jan 9 09:00), not a constant; got {ts}"
    )


def test_derive_without_required_arguments_raises():
    """derive must validate both status_order and start_status before running."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")

    # Test missing status_order (empty string is falsy)
    try:
        urd.derive(con, "", "In Progress", "Review")
        raise AssertionError("Expected SystemExit for missing status_order")
    except SystemExit as e:
        error_str = str(e).lower()
        assert "status-order" in error_str or "start-status" in error_str, (
            f"Expected error mentioning status-order or start-status; got {error_str}"
        )

    # Test missing start_status (empty string is falsy)
    try:
        urd.derive(con, "To Do,In Progress,Done", "", "Review")
        raise AssertionError("Expected SystemExit for missing start_status")
    except SystemExit as e:
        assert "start-status" in str(e).lower(), (
            f"Expected error mentioning start-status; got {str(e)}"
        )


def test_derive_prints_correct_counts():
    """The printed counts must reflect actual row counts, not be hardcoded."""
    import io
    import sys as sys_module

    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")

    # Capture stdout to check printed counts
    old_stdout = sys_module.stdout
    sys_module.stdout = io.StringIO()

    try:
        urd.derive(con, "To Do,In Progress,Review,Done", "In Progress", "Review")
        output = sys_module.stdout.getvalue()
    finally:
        sys_module.stdout = old_stdout

    # Verify the printed counts match actual row counts
    issues_count = con.execute("SELECT count(*) FROM issues").fetchone()[0]
    changes_count = con.execute("SELECT count(*) FROM changes").fetchone()[0]
    sprints_count = con.execute("SELECT count(*) FROM issue_sprints").fetchone()[0]

    assert f"derived {issues_count} issues" in output, (
        f"Output should say '{issues_count} issues', got: {output}"
    )
    assert f"{changes_count} changes" in output, (
        f"Output should say '{changes_count} changes', got: {output}"
    )
    assert f"{sprints_count} sprint memberships" in output, (
        f"Output should say '{sprints_count} sprint memberships', got: {output}"
    )


def test_status_list_whitespace_is_stripped():
    """Status names in --status-order must be stripped of whitespace."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")

    urd.derive(con, "  To Do  , In Progress , Done ", "In Progress", "Done")
    # Verify the statuses were inserted with whitespace stripped
    statuses = set(r[0] for r in con.execute(
        "SELECT status FROM status_order"
    ).fetchall())
    assert statuses == {"To Do", "In Progress", "Done"}, (
        f"Whitespace should be stripped; got {statuses}"
    )


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


def test_sprint_membership_is_ordered_so_carry_over_is_visible():
    """Ordinal must follow array position, not id or name order. The fixture has
    array [id=7 name=SprintB, id=3 name=SprintA] so ordinal must be [1, 2], not
    id order [3, 7] or name order [A, B]. Also reads start/end to verify both
    columns carry values (not null or swapped). This catches mutations that set
    ordinal to sprint.get("id"), sort by name, or confuse the date columns."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "skipped_progress", "two_sprints")
    urd.derive_issues(con)
    assert urd.derive_sprints(con) == 3
    rows = con.execute(
        "SELECT sprint_id, sprint_name, state, start, \"end\", ordinal "
        "FROM issue_sprints WHERE key = 'PROJ-3' ORDER BY ordinal"
    ).fetchall()
    # Array [id=7 name=B closed Jan5-19, id=3 name=A closed Jan19-Feb2]
    # Ordinal [1, 2] follows array, not id [3, 7] or name sort [A, B]
    assert rows == [
        (7, "Sprint B", "closed", datetime(2026, 1, 5, 9, 0, 0), datetime(2026, 1, 19, 9, 0, 0), 1),
        (3, "Sprint A", "closed", datetime(2026, 1, 19, 9, 0, 0), datetime(2026, 2, 2, 9, 0, 0), 2),
    ]


def test_carried_over_issues_are_exactly_those_with_a_second_sprint():
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "two_sprints")
    urd.derive_issues(con)
    urd.derive_sprints(con)
    carried = con.execute(
        "SELECT key FROM issue_sprints GROUP BY key HAVING max(ordinal) > 1"
    ).fetchall()
    assert carried == [("PROJ-3",)]


def test_sprint_field_null_contributes_no_rows():
    """An issue with Sprint field explicitly null (not missing, not empty)."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "skipped_progress")
    urd.derive_issues(con)
    assert urd.derive_sprints(con) == 0


def test_sprint_field_missing_contributes_no_rows():
    """An issue whose Sprint field is absent from the issue JSON, even though the
    instance has a Sprint field (resolve_field finds it). The .get() fallback returns
    an empty array, which contributes no rows."""
    con = urd.open_db(_tmpdb())
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "PROJ-MISSING",
            "u",
            urd._now(),
            json.dumps({
                "key": "PROJ-MISSING",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    "created": "2026-03-01T09:00:00.000+0000",
                    "updated": "2026-03-01T09:00:00.000+0000",
                }
            }),
        ],
    )
    con.execute("INSERT INTO fields VALUES ('customfield_20001', 'Story Points', NULL)")
    con.execute("INSERT INTO fields VALUES ('customfield_20002', 'Sprint', NULL)")
    con.execute("INSERT INTO statuses VALUES ('To Do', 'new')")
    urd.save_scope(con, status_order="To Do", start_status="To Do", review_status="To Do")
    urd.derive_issues(con)
    assert urd.derive_sprints(con) == 0


def test_sprint_field_empty_array_contributes_no_rows():
    """An issue with Sprint field as an empty array."""
    con = urd.open_db(_tmpdb())
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "PROJ-EMPTY",
            "u",
            urd._now(),
            json.dumps({
                "key": "PROJ-EMPTY",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    "created": "2026-03-01T09:00:00.000+0000",
                    "updated": "2026-03-01T09:00:00.000+0000",
                    "customfield_20002": [],
                }
            }),
        ],
    )
    con.execute("INSERT INTO fields VALUES ('customfield_20001', 'Story Points', NULL)")
    con.execute("INSERT INTO fields VALUES ('customfield_20002', 'Sprint', NULL)")
    con.execute("INSERT INTO statuses VALUES ('To Do', 'new')")
    urd.save_scope(con, status_order="To Do", start_status="To Do", review_status="To Do")
    urd.derive_issues(con)
    assert urd.derive_sprints(con) == 0


def test_derive_sprints_is_idempotent():
    """Running derive_sprints twice must return the same count, not double the rows."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "two_sprints")
    urd.derive_issues(con)
    count1 = urd.derive_sprints(con)
    count2 = urd.derive_sprints(con)
    assert count1 == count2 == 2
    # Verify final row count is 2, not 4
    total = con.execute("SELECT count(*) FROM issue_sprints").fetchone()[0]
    assert total == 2


def test_issue_sprints_timestamp_columns_are_naively_utc():
    """The start and end columns must be TIMESTAMP (naive UTC), not TIMESTAMPTZ.
    An aware column would shift to local wall time, making the same database read
    differently on a laptop and in CI, and breaking range joins in Task 13."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "two_sprints")
    urd.derive_issues(con)
    urd.derive_sprints(con)
    # Assert the column types are TIMESTAMP, not TIMESTAMPTZ
    types = {name: dtype for name, dtype, *_ in con.execute("DESCRIBE issue_sprints").fetchall()}
    assert types["start"] == "TIMESTAMP", f"start should be TIMESTAMP, got {types['start']}"
    assert types["end"] == "TIMESTAMP", f"end should be TIMESTAMP, got {types['end']}"


def test_issue_sprints_timestamps_do_not_shift_with_machine_timezone():
    """Verify that Z-form sprint dates land as naive UTC and don't shift when the
    DuckDB timezone is changed. A TIMESTAMPTZ column would shift to local wall time."""
    con = urd.open_db(_tmpdb())
    con.execute("SET TimeZone='America/Los_Angeles'")
    load_fixtures(con, "two_sprints")
    urd.derive_issues(con)
    urd.derive_sprints(con)
    start = con.execute(
        "SELECT start FROM issue_sprints WHERE key = 'PROJ-3' ORDER BY ordinal LIMIT 1"
    ).fetchone()[0]
    # Fixture has "startDate": "2026-01-05T09:00:00.000Z" for the first sprint
    # This should be stored as naive UTC 09:00:00, not shifted to Los_Angeles wall time
    expected = datetime(2026, 1, 5, 9, 0, 0)
    assert start == expected, f"Expected {expected}, got {start}"


def test_sprint_field_is_resolved_by_name_not_hardcoded_by_id():
    """Register Sprint under a different field id and verify derive_sprints still finds it.
    This proves resolve_field("Sprint") is called, not hardcoded to a specific id."""
    con = urd.open_db(_tmpdb())
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "PROJ-DIFF-ID",
            "u",
            urd._now(),
            json.dumps({
                "key": "PROJ-DIFF-ID",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    "created": "2026-03-01T09:00:00.000+0000",
                    "updated": "2026-03-01T09:00:00.000+0000",
                    "customfield_99999": [
                        {
                            "id": 1, "name": "Sprint A", "state": "closed",
                            "startDate": "2026-01-05T09:00:00.000Z",
                            "endDate": "2026-01-19T09:00:00.000Z",
                        }
                    ]
                }
            }),
        ],
    )
    # Register Sprint under a different field id (customfield_99999 instead of customfield_20002)
    con.execute("INSERT INTO fields VALUES ('customfield_99999', 'Sprint', NULL)")
    con.execute("INSERT INTO fields VALUES ('customfield_20001', 'Story Points', NULL)")
    con.execute("INSERT INTO statuses VALUES ('To Do', 'new')")
    urd.save_scope(con, status_order="To Do", start_status="To Do", review_status="To Do")
    urd.derive_issues(con)
    count = urd.derive_sprints(con)
    # Must find and insert the sprint from customfield_99999, not customfield_20002
    assert count == 1
    row = con.execute("SELECT sprint_name FROM issue_sprints WHERE key = 'PROJ-DIFF-ID'").fetchone()
    assert row[0] == "Sprint A"


def test_ordinal_follows_array_order_not_date_order():
    """Ordinal must follow array position even when sprints are out of chronological
    order. A mutation that sorts by startDate would scramble the ordinal. The later
    sprint is active (still open) while the earlier is closed, so state varies."""
    con = urd.open_db(_tmpdb())
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "PROJ-OUT-OF-ORDER",
            "u",
            urd._now(),
            json.dumps({
                "key": "PROJ-OUT-OF-ORDER",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    "created": "2026-03-01T09:00:00.000+0000",
                    "updated": "2026-03-01T09:00:00.000+0000",
                    "customfield_20002": [
                        {
                            "id": 20, "name": "Sprint Z",
                            "state": "closed",
                            "startDate": "2026-02-01T09:00:00.000Z",
                            "endDate": "2026-02-15T09:00:00.000Z",
                        },
                        {
                            "id": 10, "name": "Sprint Y",
                            "state": "active",
                            "startDate": "2026-01-01T09:00:00.000Z",
                            "endDate": "2026-01-15T09:00:00.000Z",
                        },
                    ],
                }
            }),
        ],
    )
    con.execute("INSERT INTO fields VALUES ('customfield_20001', 'Story Points', NULL)")
    con.execute("INSERT INTO fields VALUES ('customfield_20002', 'Sprint', NULL)")
    con.execute("INSERT INTO statuses VALUES ('To Do', 'new')")
    urd.save_scope(con, status_order="To Do", start_status="To Do", review_status="To Do")
    urd.derive_issues(con)
    count = urd.derive_sprints(con)
    assert count == 2
    rows = con.execute(
        "SELECT sprint_id, sprint_name, state, ordinal FROM issue_sprints "
        "WHERE key = 'PROJ-OUT-OF-ORDER' ORDER BY ordinal"
    ).fetchall()
    # Array [Z closed Feb, Y active Jan] is out of date order
    # Ordinal [1, 2] follows array, not date [2, 1] or state variation
    assert rows == [(20, "Sprint Z", "closed", 1), (10, "Sprint Y", "active", 2)]


def test_resolve_field_returns_exact_match_not_lexicographic_largest():
    """resolve_field must return the field matching "Sprint" by name, not pick
    the lexicographically largest field id when multiple names exist. Add a decoy
    field (customfield_zzzz under a different name) and verify it is ignored."""
    con = urd.open_db(_tmpdb())
    con.execute(
        "INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
        [
            "PROJ-LEX",
            "u",
            urd._now(),
            json.dumps({
                "key": "PROJ-LEX",
                "fields": {
                    "issuetype": {"name": "Task"},
                    "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    "created": "2026-03-01T09:00:00.000+0000",
                    "updated": "2026-03-01T09:00:00.000+0000",
                    "customfield_20002": [
                        {"id": 1, "name": "Sprint 1", "state": "closed",
                         "startDate": "2026-01-05T09:00:00.000Z",
                         "endDate": "2026-01-19T09:00:00.000Z"}
                    ]
                }
            }),
        ],
    )
    # Register the actual Sprint field
    con.execute("INSERT INTO fields VALUES ('customfield_20002', 'Sprint', NULL)")
    # Add a decoy field with a lexicographically larger id
    con.execute("INSERT INTO fields VALUES ('customfield_zzzz', 'Other Field', NULL)")
    con.execute("INSERT INTO fields VALUES ('customfield_20001', 'Story Points', NULL)")
    con.execute("INSERT INTO statuses VALUES ('To Do', 'new')")
    urd.save_scope(con, status_order="To Do", start_status="To Do", review_status="To Do")
    urd.derive_issues(con)
    count = urd.derive_sprints(con)
    # Must find the sprint in customfield_20002, not error on customfield_zzzz
    assert count == 1
    row = con.execute("SELECT sprint_name FROM issue_sprints WHERE key = 'PROJ-LEX'").fetchone()
    assert row[0] == "Sprint 1"


### render.py: SVG primitives (Task 8) ###

# The seven tests below are the brief's contract, verbatim.


def test_escaping_prevents_a_ticket_summary_breaking_the_svg():
    assert render.esc('a <b> & "c"') == "a &lt;b&gt; &amp; &quot;c&quot;"


def test_esc_never_prints_the_literal_word_nan_or_inf():
    """_num already treats a non-finite float as missing everywhere chart
    math happens, but a raw nan/inf can still reach *display* text
    directly (a table cell that isn't the shaded column, or a category
    value _num rejects and axes() then treats as a plain label). esc()
    is the one place every such value is stringified, so it's the one
    place this has to be caught. Renders as "" (this module's existing
    no-value convention), not "0": a displayed zero must be indistinguishable
    from a *measured* zero, which a non-finite value is not."""
    assert render.esc(float("nan")) == ""
    assert render.esc(float("inf")) == ""
    assert render.esc(float("-inf")) == ""


def test_esc_non_finite_guard_covers_decimal_too():
    """esc() originally guarded isinstance(text, float) only; _num accepts
    decimal.Decimal as a first-class number (DuckDB returns it for ROUND()/
    SUM() over a decimal column), so esc(decimal.Decimal("NaN")) printed
    the literal "NaN" and Decimal("Infinity") printed "Infinity", the same
    module-disagrees-with-itself defect closed for _num/bars/lines/scatter
    in an earlier round, just relocated to esc()'s own guard."""
    assert render.esc(decimal.Decimal("NaN")) == ""
    assert render.esc(decimal.Decimal("Infinity")) == ""
    assert render.esc(decimal.Decimal("-Infinity")) == ""
    assert render.esc(decimal.Decimal("3.5")) == "3.5"  # a finite Decimal still displays


def test_esc_does_not_blank_a_real_boolean_or_zero():
    """The non-finite guard must not over-fire: bool is a subclass of int,
    and _num deliberately excludes it (a True/False column is categorical,
    not a number) which means _num(True) is None too, indistinguishable
    from a genuine non-finite value unless esc() excludes bool explicitly.
    And a real 0 must still render as "0", not fall into the same bucket
    as a non-finite value."""
    assert render.esc(True) == "True"
    assert render.esc(False) == "False"
    assert render.esc(0) == "0"
    assert render.esc(0.0) == "0.0"


def test_bars_emits_one_rect_per_value_and_scales_to_the_maximum():
    out = render.bars(
        [{"label": "a", "done": 2, "open": 0}, {"label": "b", "done": 4, "open": 1}],
        labels="label", series=["done", "open"],
    )
    assert out.count("<rect") == 4
    assert "<svg" in out and "</svg>" in out


def test_lines_survives_a_single_point_without_dividing_by_zero():
    out = render.lines([{"wk": "2026-01-05", "created": 3}], x="wk", series=["created"])
    assert "<svg" in out


def test_empty_data_renders_a_note_not_a_broken_axis():
    assert "no data" in render.lines([], x="wk", series=["created"]).lower()


def test_scatter_draws_a_guide_line_per_percentile():
    out = render.scatter(
        [{"x": 1, "y": 2}, {"x": 2, "y": 8}], x="x", y="y",
        guides=[("p50", 5.0), ("p85", 7.6)],
    )
    assert out.count("stroke-dasharray") == 2
    assert "p85" in out


def test_table_shading_maps_the_largest_cell_to_full_strength():
    out = render.table(
        [{"from": "Alder", "to": "Birch", "n": 4}], headers=["from", "to", "n"], shade="n",
    )
    assert "fill-opacity" in out


def test_colours_are_defined_for_both_themes():
    """The file is opened in a browser, so both themes must be explicit."""
    assert "prefers-color-scheme" in render.CSS
    assert ":root" in render.CSS


# Everything below closes gaps found in fix round 1 review: the first pass's
# tests each pinned one interpolation site or one behaviour per primitive,
# and mutation testing showed roughly twenty other sites, and several whole
# properties (shared scales, zero floors, accessibility attributes), were
# free to regress unnoticed. See task-8-report.md's "Fix Round 1" section for
# the mutation log proving each of these actually goes red.

_DANGEROUS = 'a <b> & "c"'


def test_palette_slots_painted_in_css_are_distinct():
    """Checks the values CSS actually paints, not the PALETTE list, so a
    duplicate landing only in the copy that paints (not the copy that's
    otherwise tested) is still caught. Does NOT by itself prove CSS is
    *generated* from PALETTE rather than a hand-copied literal that
    happens to currently match it (a pre-fix, hand-maintained CSS with
    these same values would pass this exact check too); see
    test_palette_wiring_is_generated_not_hand_copied for that. Light and
    dark are checked separately, since slot 6 (green) is the documented
    exception that's intentionally identical in both modes."""
    hexes = re.findall(r"--s\d: (#[0-9a-fA-F]{6});", render.CSS)
    n = len(render.PALETTE)
    assert n >= 6
    assert len(hexes) == 2 * n  # one block for light, one for dark
    light, dark = hexes[:n], hexes[n:]
    assert light == render.PALETTE  # CSS's current light values match PALETTE's
    assert len(set(light)) == n
    assert len(set(dark)) == n


def test_palette_wiring_is_generated_not_hand_copied():
    """The value-equality check above would pass just as well against a
    hand-copied CSS literal that happens to match PALETTE's current
    values (that was the actual pre-fix state, and it satisfied an
    equivalent check). Pin the mechanism at the source instead: the CSS
    assembly must call the generator, not spell the hex values out a
    second time."""
    source = (pathlib.Path(__file__).parent / "render.py").read_text()
    assert "_token_block(PALETTE," in source
    assert "_token_block(_PALETTE_DARK," in source


def test_palette_muted_drives_the_css_chrome_tokens():
    """Checks the values CSS actually paints for --grid/--baseline/--muted
    match PALETTE_MUTED's current values. Like the --sN check above, this
    does NOT by itself prove generation: a hand-copied literal matching
    PALETTE_MUTED's current values (the actual pre-fix state) would pass
    this exact check too; see test_palette_muted_wiring_is_generated_not_
    hand_copied for that."""
    assert f'--grid: {render.PALETTE_MUTED["grid"]};' in render.CSS
    assert f'--baseline: {render.PALETTE_MUTED["baseline"]};' in render.CSS
    assert f'--muted: {render.PALETTE_MUTED["text"]};' in render.CSS


def test_palette_muted_wiring_is_generated_not_hand_copied():
    """PALETTE_MUTED had zero consumers once scatter's guide line moved to
    var(--baseline): a public, tested export that painted nothing, the
    same disconnect PALETTE's own CSS generation exists to prevent, just
    relocated to the chrome tokens. Pin the mechanism at the source: the
    CSS assembly must read PALETTE_MUTED's dict values, not spell them
    out a second time (a hand-copied literal matching its current values
    would satisfy the value-presence check above just as well)."""
    source = (pathlib.Path(__file__).parent / "render.py").read_text()
    assert 'PALETTE_MUTED["grid"]' in source
    assert 'PALETTE_MUTED["baseline"]' in source
    assert 'PALETTE_MUTED["text"]' in source


def test_lines_survives_all_equal_values_without_dividing_by_zero():
    """Distinct from the single-point case: three rows, all the same value,
    so the y-range is zero despite there being more than one point."""
    out = render.lines(
        [{"wk": "2026-01-05", "created": 7}, {"wk": "2026-01-12", "created": 7},
         {"wk": "2026-01-19", "created": 7}],
        x="wk", series=["created"],
    )
    assert "<svg" in out
    assert "nan" not in out.lower()


# Each escaping test below seeds a hostile string into both a row VALUE
# (label/x/band/y content) and, separately, a column-NAME argument
# (labels=/x=/band=/y=, which flow into a chart's title/tooltip text just
# as directly). No count assertion: an exact count once caught a real gap
# (fix round 1 pinned one site per primitive and missed the rest) but also
# discourages adding more coverage later, since every new site changes the
# number. "no raw markup leaked" is the property that matters; two
# different marker tags (<i>, <u>) let one test check two or three sites
# at once without them masking each other.
#
# render.py has 33 esc() call sites, not all reachable: 9 are
# esc(_fmt_num(...)), which can only ever receive a float _fmt_num itself
# already formats to clean digits/commas, so no test (hostile or
# otherwise) can reach them with anything but a number. The other 24 are
# what these tests actually pin: 24 of 24 reachable, not 33 of 33.

_DANGEROUS_NAME = 'n <i> & "m"'  # a column-name argument (labels=/x=/band=/y=)
_DANGEROUS_NAME2 = 'q <u> & "z"'  # a second one, for primitives with two


def test_bars_escapes_every_interpolated_field():
    # _DANGEROUS_NAME is the `labels` column-name arg (used in the title);
    # the row stores the actual tick-label VALUE under that same key, also
    # dangerous, so both the argument and the value it names are covered.
    rows = [{_DANGEROUS_NAME: _DANGEROUS, "n1": 2, _DANGEROUS: 1}]
    out = render.bars(rows, labels=_DANGEROUS_NAME, series=["n1", _DANGEROUS])
    assert "<b>" not in out
    assert "<i>" not in out


def test_lines_escapes_every_interpolated_field():
    rows = [
        {_DANGEROUS_NAME: _DANGEROUS, "n1": 3, _DANGEROUS: 2},
        {_DANGEROUS_NAME: "2026-01-12", "n1": 5, _DANGEROUS: 4},
    ]
    out = render.lines(rows, x=_DANGEROUS_NAME, series=["n1", _DANGEROUS])
    assert "<b>" not in out
    assert "<i>" not in out


def test_stacked_escapes_every_interpolated_field():
    rows = [
        {_DANGEROUS_NAME: _DANGEROUS, _DANGEROUS_NAME2: _DANGEROUS, _DANGEROUS: 3},
        {_DANGEROUS_NAME: "2026-01-12", _DANGEROUS_NAME2: "Done", _DANGEROUS: 2},
    ]
    out = render.stacked(rows, x=_DANGEROUS_NAME, band=_DANGEROUS_NAME2, value=_DANGEROUS)
    assert "<b>" not in out
    assert "<i>" not in out
    assert "<u>" not in out


def test_scatter_escapes_every_interpolated_field():
    rows = [{_DANGEROUS: 1, _DANGEROUS_NAME: 2}]
    out = render.scatter(rows, x=_DANGEROUS, y=_DANGEROUS_NAME, guides=[(_DANGEROUS, 1.5)])
    assert "<b>" not in out
    assert "<i>" not in out


def test_table_escapes_every_interpolated_field():
    out = render.table(
        [{_DANGEROUS: _DANGEROUS, "to": "Birch", "n": 4}], headers=[_DANGEROUS, "to", "n"],
    )
    assert "<b>" not in out


def test_small_multiples_escapes_every_interpolated_field():
    groups = {_DANGEROUS: [{_DANGEROUS: "w1", _DANGEROUS_NAME: 3}]}
    out = render.small_multiples(groups, x=_DANGEROUS, y=_DANGEROUS_NAME)
    assert "<b>" not in out
    assert "<i>" not in out


def test_every_svg_chart_has_role_title_and_viewbox_scaling():
    """role="img" plus a leading <title> gives the chart its accessible
    name; preserveAspectRatio is what lets it scale down instead of
    cropping in a narrow window. Checked across every primitive that
    returns an <svg>, not just one."""
    samples = [
        render.bars([{"label": "a", "n": 1}], labels="label", series=["n"]),
        render.lines([{"wk": "2026-01-05", "n": 1}], x="wk", series=["n"]),
        render.stacked(
            [{"wk": "2026-01-05", "status": "Done", "n": 1}], x="wk", band="status", value="n",
        ),
        render.scatter([{"x": 1, "y": 2}], x="x", y="y"),
        render.small_multiples({"g": [{"wk": "2026-01-05", "n": 1}]}, x="wk", y="n"),
    ]
    for out in samples:
        assert 'role="img"' in out
        assert "preserveAspectRatio" in out
        after_open = out.split(">", 1)[1]
        assert after_open.startswith("<title>")


def test_bars_bar_height_is_proportional_to_value_not_fixed():
    """The brief's own test is named ..._and_scales_to_the_maximum but only
    counts rects; a primitive that drew every bar at a fixed height would
    still pass it."""
    out = render.bars(
        [{"label": "a", "n": 2}, {"label": "b", "n": 4}], labels="label", series=["n"],
    )
    heights = sorted(float(h) for h in re.findall(r'height="([\d.]+)"', out))
    assert heights[0] > 0
    assert heights[1] > heights[0] * 1.5  # value doubles; height must not be flat


def test_bars_grow_from_zero_even_when_values_are_all_far_from_it():
    """Without the zero floor, a domain of [100, 102] leaves sy(0) (the
    baseline every bar measures from) extrapolating far outside that
    domain instead of landing on the true baseline, so a bar wildly
    overflows the chart's own viewBox rather than merely looking flat."""
    rows = [{"label": "a", "n": 100}, {"label": "b", "n": 102}]
    out = render.bars(rows, labels="label", series=["n"])
    vb_h = float(re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', out).group(1))
    rect_re = r'<rect x="[\d.]+" y="(-?[\d.]+)" width="[\d.]+" height="([\d.]+)"'
    for y, h in re.findall(rect_re, out):
        y, h = float(y), float(h)
        assert y >= -1
        assert y + h <= vb_h + 1


def test_stacked_colours_bands_by_first_seen_order():
    """A reversed (or otherwise reordered) colour assignment would still
    produce 3 distinctly-coloured rects; only checking which colour landed
    on which band's title catches it."""
    rows = [
        {"wk": "w1", "status": "A", "n": 1},
        {"wk": "w1", "status": "B", "n": 2},
        {"wk": "w1", "status": "C", "n": 3},
    ]
    out = render.stacked(rows, x="wk", band="status", value="n")
    assert 'fill="var(--s1)"><title>A:' in out
    assert 'fill="var(--s2)"><title>B:' in out
    assert 'fill="var(--s3)"><title>C:' in out


def test_stacked_segments_within_one_bar_have_a_surface_gap():
    """The mark spec requires the same 2px surface gap between touching
    stacked segments as between adjacent bars; segments that share an
    edge with no gap fail it even though the count and colours are right."""
    rows = [{"wk": "w1", "status": "A", "n": 10}, {"wk": "w1", "status": "B", "n": 10}]
    out = render.stacked(rows, x="wk", band="status", value="n")
    tops = [float(y) for y in re.findall(r'<rect x="[\d.]+" y="([\d.]+)"', out)]
    heights = [float(h) for h in re.findall(r'height="([\d.]+)"', out)]
    # segment A (drawn first, lower in the stack) sits below segment B
    # (drawn second, higher up); B's bottom edge (y + height) must clear
    # A's top edge (y) by a visible margin, not touch it exactly.
    a_top, b_top = tops
    a_height, b_height = heights
    assert a_top - (b_top + b_height) >= 1.5


def test_stacked_small_band_survives_the_inter_segment_gap():
    """An unconditional 2px gap between segments swallowed a small band
    whole: bands of 1000, 5 and 200 in one bar used to render at heights
    151.0, 0.0 and 28.2, the 5 present only as an invisible rect with a
    tooltip. Every band with a real value must render with visible
    height, not just correct count and colour."""
    rows = [
        {"wk": "w1", "status": "A", "n": 1000},
        {"wk": "w1", "status": "B", "n": 5},
        {"wk": "w1", "status": "C", "n": 200},
    ]
    out = render.stacked(rows, x="wk", band="status", value="n")
    assert out.count("<rect") == 3
    for h in re.findall(r'height="([\d.]+)"', out):
        assert float(h) >= 1.0


def test_stacked_segments_are_never_occluded_however_many_are_tiny():
    """The 1px floor sets a rect's height but the original fix left its
    position untouched, so a later segment (painted on top) could cover
    an earlier tiny one entirely: at 100000/1/1/1 two of the three 1-unit
    bands were fully hidden under the third, even though the height
    assertion above (which only reads each rect's own height attribute,
    never its neighbours) passed on all three. This checks the actual
    rendered extent: no two segments' [y, y+height] pixel ranges may
    overlap, in either the mild (1000/5/200) or the extreme
    (100000/1/1/1) case."""
    rect_re = r'<rect x="[\d.]+" y="(-?[\d.]+)" width="[\d.]+" height="([\d.]+)"'

    def spans(rows):
        out = render.stacked(rows, x="wk", band="status", value="n")
        return sorted((float(y), float(y) + float(h)) for y, h in re.findall(rect_re, out))

    for values in ([1000, 5, 200], [100000, 1, 1, 1]):
        rows = [{"wk": "w1", "status": chr(65 + i), "n": v} for i, v in enumerate(values)]
        segs = spans(rows)
        for pair in zip(segs, segs[1:], strict=False):  # deliberately different lengths (pairwise)
            (_top1, bottom1), (top2, _bottom2) = pair
            # segments sorted by y; each must end at or before the next begins
            assert bottom1 <= top2 + 0.01


def test_stacked_viewbox_height_matches_the_chart_not_the_last_segment():
    """Regression guard for the blocker: a loop-local `height` (now
    `seg_h`) shadowed the outer chart-height variable that the same name
    fed into svg(width, height, ...), so the viewBox height became
    whatever the very last drawn segment's own pixel height happened to
    be. Checked across band counts that exercise both the no-legend
    (<=1 band) and with-legend (2+ bands) height branches."""
    for n_bands, expected_h in ((1, 220), (2, 242), (6, 242)):
        rows = [{"wk": "w1", "status": chr(65 + i), "n": i + 1} for i in range(n_bands)]
        out = render.stacked(rows, x="wk", band="status", value="n")
        vb_h = float(re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', out).group(1))
        assert vb_h == expected_h


def _assert_content_within_viewbox(out, label=""):
    """Zero tolerance, both axes: every rect's edges (x, y, x+width,
    y+height) must sit inside its own declared viewBox, and so must every
    circle's (cx +/- r, cy +/- r) and line's (x1, y1, x2, y2). A raw
    coordinate tolerance is exactly where this class of defect hides (an
    earlier round's span invariant carried a 2-second tolerance that let
    a whole-hour timezone error straight through it), so rects/circles/
    lines are checked on their literal geometry, zero tolerance.

    A <text> element's own x/y is its anchor point, not its rendered
    extent: text-anchor="middle" centred exactly on a boundary (x=466 in
    a 480 frame) is a coordinate inside [0, 480] even though the rendered
    glyphs run well past it, so checking coordinates alone cannot see
    this failure mode at all (the same species of mistake as an earlier
    round's mirror-clip test, which checked an anchor coordinate instead
    of the direction the text renders in). Text is instead checked by
    estimated rendered extent on both axes: horizontally by the same
    rough 7px/character this module's own `_legend`/`_y_tick_pad` use to
    make the same decision, vertically by cap-height above the baseline.
    """
    vb_w, vb_h = (float(m) for m in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', out).groups())
    for x, w in re.findall(r'<rect[^>]*\bx="(-?[\d.]+)"[^>]*width="([\d.]+)"', out):
        assert float(x) >= 0, f"{label}: rect x={x} < 0"
        assert float(x) + float(w) <= vb_w + 0.01, f"{label}: rect right edge past {vb_w}"
    for y, h in re.findall(r'<rect[^>]*\by="(-?[\d.]+)"[^>]*height="([\d.]+)"', out):
        assert float(y) >= 0, f"{label}: rect y={y} < 0"
        assert float(y) + float(h) <= vb_h + 0.01, f"{label}: rect bottom edge past {vb_h}"
    for cx, cy, r in re.findall(r'<circle cx="(-?[\d.]+)" cy="(-?[\d.]+)" r="([\d.]+)"', out):
        cx, cy, r = float(cx), float(cy), float(r)
        assert cx - r >= 0, f"{label}: circle cx={cx} r={r} left edge < 0"
        assert cx + r <= vb_w + 0.01, f"{label}: circle cx={cx} r={r} right edge past {vb_w}"
        assert cy - r >= 0, f"{label}: circle cy={cy} r={r} top edge < 0"
        assert cy + r <= vb_h + 0.01, f"{label}: circle cy={cy} r={r} bottom edge past {vb_h}"
    for x1, y1, x2, y2 in re.findall(
        r'<line x1="(-?[\d.]+)" y1="(-?[\d.]+)" x2="(-?[\d.]+)" y2="(-?[\d.]+)"', out
    ):
        for x in (x1, x2):
            assert 0 <= float(x) <= vb_w + 0.01, f"{label}: line x={x} outside [0,{vb_w}]"
        for y in (y1, y2):
            assert 0 <= float(y) <= vb_h + 0.01, f"{label}: line y={y} outside [0,{vb_h}]"

    for x, tag, content in re.findall(r'<text x="(-?[\d.]+)"([^>]*)>([^<]*)</text>', out):
        m = re.search(r'text-anchor="(start|end|middle)"', tag)
        anchor = m.group(1) if m else "start"  # SVG's own default when omitted (_legend's labels)
        x = float(x)
        est_w = 7 * len(content)
        if anchor == "start":
            left, right = x, x + est_w
        elif anchor == "end":
            left, right = x - est_w, x
        else:
            left, right = x - est_w / 2, x + est_w / 2
        assert left >= -0.5, f"{label}: text {content!r} ({anchor}) left edge {left} < 0"
        msg = f"{label}: text {content!r} ({anchor}) right edge {right} > {vb_w}"
        assert right <= vb_w + 0.5, msg

    # Text needs the same extent treatment vertically, and for the same
    # reason: a baseline inside [0, vb_h] still renders its glyphs ~8px
    # above itself (cap height of the 11px charts use), so a label whose
    # baseline sits at y=6 has already lost its top 2px off the frame.
    # Checking x extent alone caught the 5- and 6-band stacked regressions
    # only because those *also* happened to shift a y tick past the left
    # edge; at 2 bands the label clipped vertically and no assertion here
    # could see it. Ascent, not the full font size: 11px digits are cap
    # height, and using 11 would fail every correctly-placed top label.
    for tag in re.findall(r"<text([^>]*)>", out):
        ym = re.search(r'\by="(-?[\d.]+)"', tag)
        if ym is None:
            continue
        baseline = float(ym.group(1))
        # 0.5px is the error bar on the 8px ascent estimate, not slack in
        # the property: rects/lines/circles above are exact geometry and
        # stay at zero tolerance, while both text checks are estimates of
        # rendered extent and carry the same 0.5. It is deliberately far
        # below the failure this guards (5 bands clipped by 8px, 8 bands
        # by 14px) and just above the sub-pixel overshoot the documented
        # 1px band floor produces legitimately (a 0.8px band painted at
        # 1px lifts the stack top, and its label, by 0.2px).
        top = baseline - 8
        assert top >= -0.5, f"{label}: text {baseline} puts glyph top at {top}, above the frame"
        assert baseline <= vb_h + 0.5, f"{label}: text baseline {baseline} past {vb_h}"


def test_every_primitive_keeps_marks_within_the_viewbox_zero_tolerance():
    """The frame (viewBox) and the content (marks, text) drifting apart is
    exactly how the stacked blocker shipped (the viewBox tracked the last
    segment instead of the chart) and how the lines endpoint label
    shipped clipped (the mark was correctly inside the frame; the label
    wasn't). Check the frame against the content directly, across every
    primitive that draws one, not just the ones with a bespoke test, with
    zero tolerance and both axes (the version this replaces used +/-5px
    and y only, which is why it did not catch the stacked total-label
    regression in the same round it was added)."""
    bars_rows = [{"label": "a", "n": 1}, {"label": "b", "n": 100}]
    samples = {
        "bars": render.bars(bars_rows, labels="label", series=["n"]),
        "lines": render.lines(
            [{"wk": "2026-01-05", "n": 1}, {"wk": "2026-01-12", "n": 100}], x="wk", series=["n"],
        ),
        "stacked": render.stacked(
            [{"wk": "w1", "status": "A", "n": 1000}, {"wk": "w1", "status": "B", "n": 5},
             {"wk": "w1", "status": "C", "n": 200}],
            x="wk", band="status", value="n",
        ),
        "scatter": render.scatter(
            [{"x": 1, "y": 2}, {"x": 2, "y": 8}], x="x", y="y", guides=[("p50", 5.0)],
        ),
        "small_multiples": render.small_multiples(
            {"g": [{"wk": "2026-01-05", "n": 3}]}, x="wk", y="n",
        ),
    }
    for name, out in samples.items():
        _assert_content_within_viewbox(out, name)


def test_stacked_total_label_stays_in_frame_across_band_counts_zero_tolerance():
    """The blocker's own fix (positioning segments sequentially) added the
    2px inter-segment gap *on top of* each segment's natural height
    instead of spending it out of that height, so the whole stack grew by
    2px per boundary: a 5-band chart pushed its total label to y=0.0, and
    an 8-band chart's top rect started at y=-2.0, both outside the
    viewBox entirely. Band counts 1 through 8, zero tolerance, both axes;
    1 through 8 is deliberately past the 3-band sample the prior check
    used, since this defect only shows up at 5 or more."""
    for n_bands in range(1, 9):
        rows = [{"wk": "w1", "status": chr(65 + i), "n": (i + 1) * 10} for i in range(n_bands)]
        out = render.stacked(rows, x="wk", band="status", value="n")
        _assert_content_within_viewbox(out, f"{n_bands} bands")


def test_small_multiples_handles_null_and_mixed_type_x_without_crashing():
    """Replacing first-seen order with sorted() introduced a comparison
    where there was none: a NULL week from a LEFT JOIN, or facets whose x
    values are different types, both raised TypeError. bars/lines/stacked
    render a None category without complaint; this must too."""
    groups = {"A": [{"wk": None, "n": 1}, {"wk": "w2", "n": 2}], "B": [{"n": 5}]}
    out = render.small_multiples(groups, x="wk", y="n")
    assert "<svg" in out

    mixed = {"A": [{"wk": 1, "n": 1}], "B": [{"wk": "w2", "n": 2}]}
    out2 = render.small_multiples(mixed, x="wk", y="n")
    assert "<svg" in out2


def test_small_multiples_sorts_integer_categories_numerically_not_lexically():
    """key=(c is None, str(c)) sorted integer weeks {2, 9, 10, 11} as the
    strings "10", "11", "2", "9". A facet that visits every category (in
    any order, since it's iterated in shared-category order regardless)
    always traces a monotonically-increasing pixel path by construction,
    which cannot tell a correct sort from a wrong one: it's checking that
    points are visited in shared_cats' own order, not that shared_cats
    itself is in the right order. Two single-point facets (so each
    renders in isolation, at the position its own value's rank in the
    shared axis gives it) expose the real property: week 2 must land
    left of week 11, not the reverse."""
    groups = {
        "full": [{"wk": w, "n": w} for w in (2, 9, 10, 11)],  # populates the shared axis
        "low": [{"wk": 2, "n": 1}],
        "high": [{"wk": 11, "n": 1}],
    }
    out = render.small_multiples(groups, x="wk", y="n")
    # local cx within each facet's own <g> (not the translated screen
    # position, which the 3-column grid layout dominates and which says
    # nothing about the value/category ordering this test is checking)
    gs = re.findall(r'<g transform="translate\([\d.]+,[\d.]+\)">(.*?)</g>', out, re.DOTALL)
    local_x = {}
    for body in gs:
        title = re.search(r'class="facet-title"[^>]*>([^<]+)<', body).group(1)
        cx = re.search(r'<circle cx="([\d.]+)"', body).group(1)
        local_x[title] = float(cx)
    assert local_x["low"] < local_x["high"]


def test_bars_skips_a_missing_value_instead_of_drawing_a_zero():
    """A missing/non-numeric value is not the same fact as a measured
    zero. Drawing a zero-height bar with a "<title>series: 0</title>"
    tooltip claims data that isn't there; the other five primitives all
    draw no mark for a missing point, and bars must match them."""
    rows = [{"label": "a", "present": 5}]
    out = render.bars(rows, labels="label", series=["present", "missing"])
    assert out.count("<rect") == 1
    assert "missing: 0" not in out


def test_none_category_renders_as_empty_text_not_the_literal_none():
    """table() already turns a None cell into empty text; bars, stacked
    and small_multiples used to render the literal word "None" for a NULL
    category (a LEFT JOIN with no match, or an ungrouped bucket)."""
    out = render.bars([{"label": None, "n": 3}], labels="label", series=["n"])
    assert ">None<" not in out

    out = render.stacked([{"wk": "w1", "status": None, "n": 3}], x="wk", band="status", value="n")
    assert ">None<" not in out
    assert "<title>None:" not in out

    out = render.small_multiples({None: [{"wk": "w1", "n": 3}]}, x="wk", y="n")
    assert ">None<" not in out


def test_stacked_none_x_value_renders_as_empty_tick_text():
    """The prior test only ever set the *band* to None; stacked's x-tick
    None-guard is a separate line and was never exercised by it. A
    second, real x value keeps the tick text from collapsing to a single
    band (which would skip the tick loop's only interesting case)."""
    rows = [{"wk": None, "status": "A", "n": 3}, {"wk": "w2", "status": "A", "n": 2}]
    out = render.stacked(rows, x="wk", band="status", value="n")
    assert ">None<" not in out


def test_legend_none_name_renders_as_empty_text_not_the_literal_none():
    """The prior test's stacked/small_multiples scenarios only ever had
    one distinct band/group, so _legend (which needs 2+ names to render
    at all) was never called with a None name; its own None-guard was a
    dead arm."""
    rows = [{"wk": "w1", "status": None, "n": 3}, {"wk": "w1", "status": "Done", "n": 2}]
    out = render.stacked(rows, x="wk", band="status", value="n")
    assert "legend-label" in out  # confirms the legend actually rendered
    assert ">None<" not in out


def test_lines_endpoint_labels_stay_inside_the_viewbox_with_realistic_data():
    """dataviz relief rule: a contrast WARN on a categorical slot requires
    visible direct labels or the table view, but the first version of this
    label was left-anchored past the last point, which sits at the plot's
    right edge on any real (multi-point) chart: it started at x=472 in a
    480-wide viewBox and ran off it. A single-row chart is the one
    geometry where sx returns the plot centre and nothing clips, so this
    uses three weeks and two series instead."""
    rows = [
        {"wk": "2026-01-05", "created": 3, "closed": 1},
        {"wk": "2026-01-12", "created": 5, "closed": 4},
        {"wk": "2026-01-19", "created": 1234567, "closed": 42},
    ]
    out = render.lines(rows, x="wk", series=["created", "closed"])
    label_re = r'x="(-?[\d.]+)"[^>]*class="value-label" text-anchor="end"'
    label_xs = [float(m) for m in re.findall(label_re, out)]
    assert label_xs  # at least one endpoint label actually drawn
    assert max(label_xs) <= 466  # the plot's own right edge, not the viewBox's


def test_lines_axis_ticks_stay_in_frame_with_iso_weeks_and_a_7digit_value():
    """The same clip as the endpoint label, on both axes of the most
    canonical chart there is. `lines(rows, x="wk")` with ISO week labels
    used to centre the last x-tick at x=466 in a 480 frame (glyphs
    running to about 500), since a centred label on the very first/last
    tick is centred *on the plot's own edge*. And every y-tick is
    right-aligned at a fixed x=40 regardless of how wide the value is: a
    7-digit count measured at -41.8. Nothing in the original suite looked
    at x at all."""
    rows = [
        {"wk": "2026-W01", "created": 3},
        {"wk": "2026-W02", "created": 5},
        {"wk": "2026-W03", "created": 1234567},
    ]
    out = render.lines(rows, x="wk", series=["created"])
    _assert_content_within_viewbox(out, "iso weeks + 7-digit y")


def test_y_tick_pad_widens_only_when_a_label_actually_needs_it():
    """The default 46px margin is unchanged for realistic small values (no
    plot width sacrificed for the common case); a 7-digit value must
    widen it, since its right-aligned tick label needs more room than
    that to avoid running past x=0."""
    assert render._y_tick_pad([5, 10, 20], zero_floor=True) == 46
    assert render._y_tick_pad([5, 1234567], zero_floor=False) > 46


def test_bars_and_stacked_y_axis_widens_for_a_wide_value_too():
    """Not just lines/scatter (via axes()): bars and stacked compute their
    own left padding the same way, and both used to right-align a
    7-digit tick label at the same fixed x=40 regardless of width."""
    bars_rows = [{"label": "a", "n": 5}, {"label": "b", "n": 1234567}]
    out = render.bars(bars_rows, labels="label", series=["n"])
    _assert_content_within_viewbox(out, "bars 7-digit y")

    stacked_rows = [{"wk": "w1", "status": "A", "n": 1234567}]
    out2 = render.stacked(stacked_rows, x="wk", band="status", value="n")
    _assert_content_within_viewbox(out2, "stacked 7-digit y")


def test_lines_skips_a_value_label_that_would_overlap_a_convergent_series():
    """dataviz: "when end-labels collide, don't stack them." Three series
    ending within a pixel of each other must not draw three overlapping
    numbers; a dropped label's value still reaches the reader through its
    <title> tooltip and the legend, so nothing is gated, only decluttered."""
    rows = [
        {"wk": "2026-01-05", "a": 10, "b": 10, "c": 10},
        {"wk": "2026-01-12", "a": 50, "b": 50.3, "c": 49.8},
    ]
    out = render.lines(rows, x="wk", series=["a", "b", "c"])
    # class="value-label" (anchor-agnostic), not "...text-anchor=\"end\"":
    # the mirror-clip fix means a label can anchor "start" instead when its
    # point is on the left half, and counting only "end" would go vacuous
    # (always pass, whether or not the collision rule actually fired) the
    # day these endpoints land there.
    assert out.count('class="value-label"') < 3
    assert "<title>a: " in out and "<title>b: " in out and "<title>c: " in out


def test_lines_endpoint_label_stays_inside_the_left_edge_too():
    """The right-edge fix anchored every label leftward of its dot
    unconditionally (text-anchor="end", x=lx-6): safe when lx is near the
    plot's right edge (text grows left, into the plot), but a series
    whose last non-null point is the *first* x (it stopped appearing, a
    status or assignee that dropped out) sits near the plot's *left*
    edge, where growing further leftward runs a multi-digit value past
    x=0. Checking only the anchor's x coordinate can't see this: that
    coordinate stays a small positive number in both the buggy and the
    fixed version, since the actual clipping happens in the *rendered
    text*, which grows outward from the anchor in the direction
    text-anchor points, not at the anchor point itself. The fix is which
    direction it grows in, so check that."""
    rows = [
        {"wk": "2026-01-05", "a": 1234567},
        {"wk": "2026-01-12"},
        {"wk": "2026-01-19"},
    ]
    out = render.lines(rows, x="wk", series=["a"])
    m = re.search(r'x="(-?[\d.]+)"[^>]*class="value-label" text-anchor="(start|end)"', out)
    assert m, "expected exactly one endpoint label"
    x, anchor = float(m.group(1)), m.group(2)
    assert anchor == "start"  # grows rightward, into the plot, not off the left edge
    assert x >= 0


def test_lines_collision_check_compares_x_not_just_y():
    """Comparing only y meant three series ending 210px apart at the same
    height still counted as one crowded cluster and produced a single
    label; they don't overlap and each needs its own."""
    rows = [
        {"wk": "w1", "a": 10},
        {"wk": "w2", "b": 10},
        {"wk": "w3", "c": 10},
    ]
    out = render.lines(rows, x="wk", series=["a", "b", "c"])
    assert out.count('class="value-label"') == 3


def test_lines_drops_the_high_contrast_label_first_on_collision():
    """dataviz: the sub-3:1 WARN slots' documented mitigation *is* the
    direct label; dropping their label on a collision and keeping a
    high-contrast slot's (whose line and dot are already legible) defeats
    the whole mechanism. Slot index 2 (aqua) is a WARN slot; slot 0
    (blue) is not."""
    rows = [
        {"wk": "w1", "blue": 0, "other": 100, "aqua": 100},
        {"wk": "w2", "blue": 50, "other": 5, "aqua": 50.3},
    ]
    out = render.lines(rows, x="wk", series=["blue", "other", "aqua"])
    assert out.count('class="value-label"') == 2  # blue and aqua collide; one drops
    # class="value-label", not the axis tick (class="tick"), which can
    # coincidentally show the same numbers
    assert 'class="value-label" text-anchor="end">50.30</text>' in out  # aqua survives
    assert 'class="value-label" text-anchor="end">50</text>' not in out  # blue does not


def test_stacked_labels_each_bars_total():
    """Same relief rule as lines, but a stacked segment has no free end to
    label (an interior segment's own value belongs in the tooltip/legend),
    so the bar's total is what gets the direct label instead."""
    rows = [{"wk": "w1", "status": "A", "n": 3}, {"wk": "w1", "status": "B", "n": 4}]
    out = render.stacked(rows, x="wk", band="status", value="n")
    # class="value-label", not the axis tick, which can coincidentally show
    # the same number (7 = 3 + 4) when it's also the domain max
    assert 'class="value-label" text-anchor="middle">7</text>' in out


def test_table_shading_is_proportional_not_uniform():
    """A mutation that shades every cell at full strength still passes the
    brief's single-row test (there's only one cell, and it is the max)."""
    out = render.table(
        [{"from": "A", "to": "B", "n": 2}, {"from": "C", "to": "D", "n": 4}],
        headers=["from", "to", "n"], shade="n",
    )
    assert 'fill-opacity="1.00"' in out
    assert 'fill-opacity="0.50"' in out


def test_table_shading_handles_an_all_negative_column():
    """max(shade_v, 0) / shade_max floors the numerator at 0 but not the
    denominator: an all-negative shade column gave a negative shade_max,
    which is truthy, so 0 / negative produced "fill-opacity=-0.00" against
    a docstring promising the largest cell renders at full strength."""
    rows = [{"from": "A", "to": "B", "n": -5}, {"from": "C", "to": "D", "n": -1}]
    out = render.table(rows, headers=["from", "to", "n"], shade="n")
    assert "-0.00" not in out
    assert "fill-opacity" not in out


def test_table_never_prints_the_literal_word_nan_for_a_non_shaded_cell():
    """A non-shaded cell's text is `esc(v)` with no numeric formatting in
    between; a raw nan/inf value there used to print the literal word
    "nan"/"inf" as if it were real ticket data."""
    out = render.table([{"from": "A", "to": "B", "n": float("nan")}], headers=["from", "to", "n"])
    assert "nan" not in out.lower()


def test_scatter_axis_never_prints_nan_or_inf_as_a_tick_label():
    """A NaN in the x column makes axes() fall back to its categorical
    branch (not every value is numeric), whose tick labels used to print
    str(c) verbatim: "nan" as an axis category, right next to the real
    numeric ticks on the y axis."""
    rows = [{"x": 1, "y": 2}, {"x": float("nan"), "y": 3}]
    out = render.scatter(rows, x="x", y="y")
    assert "nan" not in out.lower()
    assert "inf" not in out.lower()


def test_scatter_guide_line_uses_a_theme_variable_not_a_hardcoded_hex():
    """A hard-coded light-mode hex on the guide line would be the one mark
    in the whole module that doesn't switch with the theme, and in dark
    mode it would outshout the data it's meant to annotate."""
    out = render.scatter([{"x": 1, "y": 2}], x="x", y="y", guides=[("p50", 1.5)])
    assert 'stroke="var(--baseline)"' in out
    assert "#c3c2b7" not in out


def test_legend_wraps_instead_of_running_past_the_viewbox():
    """Six realistic status names used to put the last legend entry past
    x=480 in a 480-wide viewBox, cropped with no indication anything was
    missing; worst for `stacked`, whose bands have no other identity
    channel."""
    statuses = ["To Do", "In Progress", "Waiting for review", "In Review", "Blocked", "Done"]
    rows = [{"wk": "2026-01-05", **{s: 1 for s in statuses}}]
    out = render.lines(rows, x="wk", series=statuses)
    xs = [float(m) for m in re.findall(r'<text x="([\d.]+)" y="[\d.]+" class="legend-label"', out)]
    assert xs
    assert max(xs) < 480  # never past the viewBox width
    ys = {float(m) for m in re.findall(r'<text x="[\d.]+" y="([\d.]+)" class="legend-label"', out)}
    assert len(ys) > 1  # wrapped onto more than one row


def test_small_multiples_shares_one_y_scale_across_facets():
    """Each facet auto-scaling to its own max independently is exactly how
    small multiples mislead; the same value in two facets with different
    maxima must land at the same pixel height."""
    facets = {
        "low-max": [{"wk": "w1", "n": 2}],
        "high-max": [{"wk": "w1", "n": 2}, {"wk": "w2", "n": 20}],
    }
    out = render.small_multiples(facets, x="wk", y="n")
    groups = re.findall(r'<g transform="translate\([\d.]+,[\d.]+\)">(.*?)</g>', out, re.DOTALL)
    ys = set()
    for body in groups:
        # first point of the facet: a path start (2+ points), an isolated-
        # point dot (r=2), or the end-marker (r=3) when that's the only point
        m = re.search(
            r'(?:<path d="M[\d.]+,([\d.]+))|(?:<circle cx="[\d.]+" cy="([\d.]+)" r="[23]")', body,
        )
        ys.add(m.group(1) or m.group(2))
    assert len(ys) == 1  # the n=2 point renders at the same y in both facets


def test_small_multiples_shares_one_x_domain_and_breaks_at_gaps():
    """An independent x per facet puts a different category under "column 1"
    in every panel (Task 13 facets by person and week; a person with no
    output some week has no row for it), so the shared y-scale buys nothing.
    A silent joining line across the gap would also read a real gap as
    steady output."""
    facets = {
        "sparse": [{"wk": "w1", "n": 1}, {"wk": "w3", "n": 3}],  # w2 missing
        "full": [{"wk": "w1", "n": 1}, {"wk": "w2", "n": 2}, {"wk": "w3", "n": 3}],
    }
    out = render.small_multiples(facets, x="wk", y="n")
    # only the facet with no gap draws a connected path across all 3 points
    assert out.count("<path") == 1
    paths = re.findall(r'<path d="([^"]+)"', out)
    assert paths[0].count("L") == 2  # 3 points, 2 line segments, in the full facet


def test_bars_accepts_decimal_values_like_a_float():
    """DuckDB returns decimal.Decimal from ROUND()/SUM() over a decimal
    column; an earlier version of this module raised TypeError here."""
    rows = [{"label": "a", "n": decimal.Decimal("2")}, {"label": "b", "n": decimal.Decimal("4")}]
    out = render.bars(rows, labels="label", series=["n"])
    assert out.count("<rect") == 2
    heights = [float(h) for h in re.findall(r'height="([\d.]+)"', out)]
    assert max(heights) > min(heights) * 1.5


def test_lines_accepts_decimal_values_like_a_float():
    """An earlier version silently dropped every point for a Decimal
    series, rendering axes and a title but zero paths and zero circles,
    which looks like an empty chart rather than a bug."""
    rows = [
        {"wk": "2026-01-05", "n": decimal.Decimal("3")},
        {"wk": "2026-01-12", "n": decimal.Decimal("5")},
    ]
    out = render.lines(rows, x="wk", series=["n"])
    assert "<path" in out
    assert "<circle" in out


def test_stacked_accepts_decimal_values_like_a_float():
    rows = [{"wk": "2026-01-05", "status": "Done", "n": decimal.Decimal("3")}]
    out = render.stacked(rows, x="wk", band="status", value="n")
    assert out.count("<rect") == 1


def test_scatter_accepts_decimal_values_like_a_float():
    """An earlier version's numeric-x check rejected Decimal, silently
    falling back to a categorical axis (one tick per distinct value)
    instead of the continuous scale (5 ticks from the default tick
    count), which draws real correlation data as evenly-spaced labels.
    Total tick count (not just middle-anchored ones, since the first/last
    x-tick anchor "start"/"end" instead) distinguishes 5 x-ticks + 5
    y-ticks = 10 for the numeric branch from 2 x-ticks + 5 y-ticks = 7 for
    the categorical fallback."""
    rows = [{"x": decimal.Decimal("1"), "y": decimal.Decimal("2")},
            {"x": decimal.Decimal("2"), "y": decimal.Decimal("8")}]
    out = render.scatter(rows, x="x", y="y")
    assert out.count("<circle") == 2
    assert out.count('class="tick"') == 10


def test_table_shading_accepts_decimal_values_like_a_float():
    rows = [{"from": "A", "to": "B", "n": decimal.Decimal("2")},
            {"from": "C", "to": "D", "n": decimal.Decimal("4")}]
    out = render.table(rows, headers=["from", "to", "n"], shade="n")
    assert 'fill-opacity="1.00"' in out
    assert 'fill-opacity="0.50"' in out


def test_small_multiples_accepts_decimal_values_like_a_float():
    out = render.small_multiples(
        {"g": [{"wk": "2026-01-05", "n": decimal.Decimal("3")}]}, x="wk", y="n",
    )
    assert "<path" in out or "<circle" in out


def test_bars_empty_data_renders_a_note():
    assert "no data" in render.bars([], labels="label", series=["done"]).lower()


def test_stacked_empty_data_renders_a_note():
    assert "no data" in render.stacked([], x="wk", band="status", value="n").lower()


def _stacked_tick_labels(out):
    """(anchor, text) for each x-axis tick, in document order."""
    return re.findall(r'class="tick" text-anchor="(\w+)">([^<]*)</text>', out)


def _weeks(n):
    return [{"day": f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}", "status": s, "tickets": 1 + i % 4}
            for i in range(n) for s in ("To Do", "In Progress", "Review")]


def test_stacked_thins_x_labels_to_what_actually_fits():
    """26 weeks is an ordinary cumulative flow, and it put 26 ten-character dates
    into 480px. The count must fall well below the bar count, not track it."""
    for n in (13, 26, 52, 222):
        dates = [t for _, t in _stacked_tick_labels(render.stacked(
            _weeks(n), x="day", band="status", value="tickets")) if t.startswith("2026")]
        assert 1 < len(dates) <= 8, f"{n} bars produced {len(dates)} date labels"


def test_stacked_anchors_its_outermost_x_labels_away_from_the_frame():
    """A centred label on the last bar is centred on the plot's own right edge, so
    half of it always leaves the viewBox. `axes` fixes this for every renderer
    that shares it; `stacked` builds its own axis and did not inherit it."""
    ticks = [(a, t) for a, t in _stacked_tick_labels(
        render.stacked(_weeks(26), x="day", band="status", value="tickets"))
        if t.startswith("2026")]
    assert ticks[0][0] == "start" and ticks[-1][0] == "end"
    assert all(a == "middle" for a, _ in ticks[1:-1])
    # A lone label has no edge to run off, so it stays centred.
    single = [a for a, t in _stacked_tick_labels(render.stacked(
        _weeks(1), x="day", band="status", value="tickets")) if t.startswith("2026")]
    assert single == ["middle"]


def test_stacked_stays_in_frame_across_realistic_x_counts():
    for n in (1, 2, 5, 13, 26, 52, 222):
        _assert_content_within_viewbox(
            render.stacked(_weeks(n), x="day", band="status", value="tickets"), f"x={n}")


def test_scatter_empty_data_renders_a_note():
    assert "no data" in render.scatter([], x="x", y="y").lower()


def test_table_empty_data_renders_a_note():
    assert "no data" in render.table([], headers=["a", "b"]).lower()


def test_small_multiples_empty_data_renders_a_note():
    assert "no data" in render.small_multiples({}, x="wk", y="n").lower()


def _flow_rows(con, key):
    # Named failure rather than a bare StopIteration from next(): a renamed or
    # missing key otherwise surfaces as an exception that says nothing about
    # which chart, in a helper that six tests and counting go through.
    chart = next((c for c in chart_specs.CHARTS if c.key == key), None)
    assert chart is not None, (
        f"no chart keyed {key!r}; have {sorted(c.key for c in chart_specs.CHARTS)}")
    cursor = con.execute(chart.sql)
    columns = [d[0] for d in cursor.description]
    return chart, [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]


def test_every_chart_spec_is_well_formed():
    seen = set()
    for chart in chart_specs.CHARTS:
        assert chart.key not in seen, f"duplicate chart key {chart.key}"
        seen.add(chart.key)
        # Against what figure can actually draw, not a hand-kept list: a spec naming
        # a kind no renderer handles must fail here, not as a blank space in a browser.
        assert chart.kind in render.FIGURE_KINDS, f"{chart.key}: unrenderable {chart.kind}"
        assert chart.section in chart_specs.SECTIONS, f"{chart.key}: stray section"
        assert chart.caption, f"{chart.key} has no caption"


def test_every_chart_sql_returns_the_columns_its_options_name():
    """A query that runs but names its columns differently draws an empty chart.
    The renderers read rows by key and silently get None, so this is the only
    place the mismatch is visible."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    needed = {"lines": ("x",), "stacked": ("x", "band", "value"), "scatter": ("x", "y")}
    for chart in chart_specs.CHARTS:
        cursor = con.execute(chart.sql)
        columns = {d[0] for d in cursor.description}
        cursor.fetchall()
        for option in needed.get(chart.kind, ()):
            assert chart.options[option] in columns, (
                f"{chart.key}: options[{option!r}]={chart.options[option]!r} "
                f"is not among {sorted(columns)}")
        for series in chart.options.get("series", []):
            assert series in columns, f"{chart.key}: series {series!r} not in {sorted(columns)}"
        for header in chart.options.get("headers", []):
            assert header in columns, f"{chart.key}: header {header!r} not in {sorted(columns)}"
        if chart.coverage:
            numerator, denominator = con.execute(chart.coverage).fetchone()
            assert numerator is not None and denominator is not None


def test_flow_health_charts_are_all_present():
    keys = {c.key for c in chart_specs.CHARTS if c.section == "Flow health"}
    assert keys == {"aging_wip", "created_vs_closed", "cfd", "cycle_scatter", "time_in_status"}


def test_aging_lists_only_open_tickets_worst_first():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, rows = _flow_rows(con, "aging_wip")
    # PROJ-1 and PROJ-2 are status_category 'done'; PROJ-3 is In Progress.
    assert [r["key"] for r in rows] == ["PROJ-3"]


def test_the_cumulative_flow_is_a_weekly_snapshot_not_a_daily_sum():
    """Grouping days into weeks with count(*) multiplies every value by about
    seven while preserving the shape, so the chart still looks right. The count
    of tickets in a status can never exceed the number of tickets."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, rows = _flow_rows(con, "cfd")
    total = con.execute("SELECT count(*) FROM issues").fetchone()[0]
    assert rows, "no cumulative flow rows at all"
    assert max(r["tickets"] for r in rows) <= total
    # Bounded, so the chart does not grow a bar a day forever.
    assert len({r["day"] for r in rows}) <= 27


def test_time_in_status_ignores_time_spent_already_done():
    """status_durations closes an open span at now(), so a closed ticket's Done
    span measures time since resolution. It read 207 days in the fixtures."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, rows = _flow_rows(con, "time_in_status")
    assert rows
    assert "Done" not in {r["status"] for r in rows}


def test_cycle_time_falls_below_its_threshold_in_the_fixtures():
    """skipped_progress resolves without ever entering start_status, so coverage
    is 1 of 2. The chart must say so rather than plot one point as if it were the
    whole picture. If this test fails, the threshold or the fixture moved."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart = next(c for c in chart_specs.CHARTS if c.key == "cycle_scatter")
    numerator, denominator = con.execute(chart.coverage).fetchone()
    assert (numerator, denominator) == (1, 2)
    out = urd.run_chart(con, chart)
    assert "1 of 2" in out and "<svg" not in out


def test_sections_render_in_a_fixed_order():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    titles = [title for title, _ in urd.render_sections(con)]
    assert titles[0] == "Flow health"
    # Only one section holds charts until Task 11, so asserting the real list is
    # in SECTIONS order is vacuous: a one-element list is sorted every way at
    # once, and reversing SECTIONS leaves this green. Order is therefore checked
    # against a stand-in chart list, declared deliberately back to front.
    spare = [s for s in chart_specs.SECTIONS if s != "Flow health"][0]
    stub = {"headers": ["n"]}
    original = chart_specs.CHARTS
    try:
        chart_specs.CHARTS = [
            chart_specs.Chart(key="second", section=spare, title="B", kind="table",
                              caption="c", sql="SELECT 1 AS n", options=stub),
            chart_specs.Chart(key="first", section="Flow health", title="A", kind="table",
                              caption="c", sql="SELECT 1 AS n", options=stub),
        ]
        ordered = [title for title, _ in urd.render_sections(con)]
    finally:
        chart_specs.CHARTS = original
    assert ordered == ["Flow health", spare], "sections follow CHARTS order, not SECTIONS"


def test_no_chart_axis_label_carries_a_midnight_timestamp():
    """A tick label is the bucket value's own str(), and date_trunc returns a
    TIMESTAMP, so a weekly bucket prints '2026-01-05 00:00:00': nineteen
    characters of which the last eight are always midnight. It also crowds the
    axis badly enough that thinning drops most of the labels. Cast to ::DATE."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    checked = 0
    for section, figures in urd.render_sections(con):
        for markup in figures:
            for label in re.findall(r'class="tick"[^>]*>([^<]*)</text>', markup):
                assert "00:00:00" not in label, f"{section}: axis label {label!r}"
                checked += 1
    assert checked, "no tick labels found at all; the regex is what broke"


def test_every_rendered_chart_stays_inside_its_frame():
    """Every chart the report draws, against the frame it draws into.

    This does NOT catch the daily cumulative flow that prompted it: that chart
    overflowed by 0.8px before commit 91e8f85 taught `stacked` to thin and anchor
    its x labels, and the same fix now absorbs 222 days without complaint. The
    bar-count bound in the weekly-snapshot test is what catches it. Kept because
    it is a standing guard over every chart Tasks 11 to 13 add, not because it
    guards the one that motivated it."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    drawn = 0
    for section, figures in urd.render_sections(con):
        for markup in figures:
            for chunk in re.findall(r"<svg\b.*?</svg>", markup, re.S):
                _assert_content_within_viewbox(chunk, f"{section}")
                drawn += 1
    assert drawn >= 3, f"only {drawn} SVGs rendered; the loop is not seeing charts"


def test_figure_draws_a_scatter_with_its_percentile_guides():
    """The fixtures always take the coverage-strip path for the only scatter, so
    the renderer branch is exercised directly or not at all."""
    chart = chart_specs.Chart(
        key="k", section="Flow health", title="T", kind="scatter",
        caption="c", options={"x": "resolved", "y": "cycle_days",
                              "guides_sql": "SELECT 1.0, 4.0"},
        sql="SELECT 1",
    )
    con = urd.open_db(_tmpdb())
    rows = [{"resolved": 1, "cycle_days": 2}, {"resolved": 3, "cycle_days": 6}]
    out = render.figure(chart, rows, "sub", con)
    assert "<svg" in out and "p50" in out and "p85" in out
    assert "<figcaption>sub</figcaption>" in out
    assert "<h3>T</h3>" in out


def test_figure_refuses_a_kind_no_renderer_handles():
    chart = chart_specs.Chart(key="k", section="Flow health", title="T", kind="sunburst",
                              caption="c", sql="SELECT 1")
    try:
        render.figure(chart, [], "sub", None)
    except ValueError as exc:
        assert "sunburst" in str(exc)
    else:
        raise AssertionError("an unknown kind rendered silently")


def _header(**over):
    base = {"project": "PROJ", "component": "TEAM", "since": "2026-01-01",
            "synced": "2026-08-13T17:00:00", "errors": 0, "issues": 41}
    return {**base, **over}


def test_the_page_states_the_scope_it_covers():
    """A report must never be mistaken for one covering a different slice."""
    html = render.page(_header(), [("Flow health", ["<p>chart</p>"])])
    # The composed scope, not two substrings that could each appear anywhere.
    assert "PROJ / TEAM" in html
    assert "2026-01-01" in html
    assert "2026-08-13T17:00:00" in html
    assert "Flow health" in html
    assert html.count("<p>chart</p>") == 1
    assert "<title>" in html and html.rstrip().endswith("</html>")


def test_a_scope_with_no_component_says_so_without_a_stray_separator():
    html = render.page(_header(component=None), [])
    assert "PROJ" in html
    assert "/" not in html[html.index("<h1>"):html.index("</h1>")]


def test_a_scope_containing_markup_is_escaped_not_injected():
    html = render.page(_header(project="P&D", component="<script>x</script>"), [])
    assert "<script>" not in html
    assert "P&amp;D" in html


def test_outstanding_sync_errors_are_visible_in_the_header():
    """41 tickets and 3 errors: the 3 has to be the error count, not the ticket count."""
    html = render.page(_header(errors=3), [])
    assert "3 sync error" in html
    # And the warning is conditional, not decoration that is always on.
    assert "sync error" not in render.page(_header(errors=0), [])


def test_a_chart_below_its_threshold_becomes_a_warning_not_a_plot():
    strip = render.coverage_strip("Points per person", 4, 100, 0.5)
    assert "4 of 100" in strip
    assert "4%" in strip     # the coverage actually measured
    assert "50%" in strip    # the threshold it fell short of
    assert "<svg" not in strip


def test_a_coverage_strip_with_no_tickets_at_all_does_not_divide_by_zero():
    strip = render.coverage_strip("Points per person", 0, 0, 0.5)
    assert "0 of 0" in strip


def test_every_class_the_page_emits_is_styled():
    """An unstyled warning is an invisible warning, which is the failure this guards."""
    emitted = render.page(_header(errors=3), []) + render.coverage_strip("X", 1, 10, 0.5)
    classes = set(re.findall(r'class="([\w-]+)"', emitted))
    assert classes, "no classes found: the regex, not the page, is what broke"
    for cls in classes:
        # The lookahead is load-bearing, not tidiness. A plain `f".{cls}" in CSS`
        # is satisfied by any longer selector sharing the prefix: `.warn` matches
        # inside `.warn-inline`, so the whole `.warn` rule could be deleted with
        # this test still green. `[\w-]` is CSS's ident continuation set.
        selector = re.compile(rf"\.{re.escape(cls)}(?![\w-])")
        assert selector.search(render.CSS), f"class {cls} is emitted but never styled"


def test_report_writes_a_standalone_file_with_no_external_references():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    out = os.path.join(tempfile.mkdtemp(), "report.html")
    assert urd.report(con, out) == 0
    html = pathlib.Path(out).read_text()
    assert html.startswith("<!doctype html>")
    # Every way a saved file could still reach the network, not just one of them.
    for pattern in (r"https?:", r"//\w", r"@import", r"\bsrc\s*=", r"\bhref\s*=", r"url\("):
        assert not re.search(pattern, html), f"{pattern} would fetch from the network"


def test_the_report_header_reflects_the_database_it_read():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    out = os.path.join(tempfile.mkdtemp(), "report.html")
    urd.report(con, out)
    html = pathlib.Path(out).read_text()
    expected = con.execute("SELECT count(*) FROM issues").fetchone()[0]
    assert f"{expected} tickets" in html


def test_outward_reporting_charts_are_all_present():
    keys = {c.key for c in chart_specs.CHARTS if c.section == "Reporting outward"}
    assert keys == {"per_fix_version", "per_epic", "type_mix"}


def test_fix_version_chart_counts_a_ticket_in_every_version_it_carries():
    """No fixture ticket carries two versions, so the unnest this chart exists for
    is not exercised by the fixture data alone: one is given a second version here."""
    con = _derived("reopened", "two_sprints")
    con.execute("UPDATE issues SET fix_versions = ['R1', 'R2'] WHERE key = 'PROJ-1'")
    _, rows = _flow_rows(con, "per_fix_version")
    counts = {r["fix_version"]: (r["done"], r["open"]) for r in rows}
    # PROJ-1 is done and now in both; PROJ-3 is open and in R2 only.
    assert counts == {"R1": (1, 0), "R2": (1, 1)}


def test_epic_chart_splits_done_from_open_rather_than_done_from_total():
    """done and total side by side double-counts: the first bar is contained in
    the second, and grouped bars read as disjoint quantities."""
    con = _derived("reopened", "two_sprints")
    chart, rows = _flow_rows(con, "per_epic")
    assert chart.options["series"] == ["done", "open"]
    assert [(r["epic"], r["done"], r["open"]) for r in rows] == [("PROJ-100", 1, 1)]


def test_bars_is_renderable_now_that_charts_ask_for_it():
    """Task 10 left `bars` out of FIGURE_KINDS on purpose, so the first spec that
    needs it fails in the suite rather than as a blank space in the report."""
    assert "bars" in render.FIGURE_KINDS
    assert {c.kind for c in chart_specs.CHARTS} <= render.FIGURE_KINDS


def test_sections_render_in_a_fixed_order_with_two_populated():
    """Now that a second section holds charts, the real list is long enough for
    its order to mean something."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    titles = [title for title, _ in urd.render_sections(con)]
    assert titles == ["Flow health", "Reporting outward"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
