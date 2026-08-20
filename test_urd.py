import decimal
import json
import os
import pathlib
import re
import tempfile
from datetime import datetime

import charts as chart_specs
import render
import test_helpers  # noqa: F401 - installs the network-refusal guard on import
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


def _abandoned(*fixtures, statuses="Done"):
    """Derive with a done-category status declared as abandonment rather than
    delivery. The fixtures close everything into Done, so naming Done here is
    what makes the split observable without inventing a fourth fixture."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, *fixtures)
    scope = urd.load_scope(con)
    urd.derive(con, scope["status_order"], scope["start_status"], scope["review_status"],
               abandoned_status=statuses)
    return con


def test_a_closure_into_an_abandoned_status_is_not_delivery():
    """A ticket dropped is a data point, not something shipped. Both counts stay
    available; only their meaning is separated."""
    con = _abandoned("reopened", "skipped_progress", "two_sprints")
    rows = con.execute("SELECT abandoned, count(*) FROM closures GROUP BY 1").fetchall()
    assert dict(rows) == {True: 2}, rows
    plain = _derived("reopened", "skipped_progress", "two_sprints")
    assert dict(plain.execute(
        "SELECT abandoned, count(*) FROM closures GROUP BY 1").fetchall()) == {False: 2}


def test_an_abandoned_ticket_is_flagged_on_issues_too():
    """closures is per event; issues is current state. Charts that ask
    `status_category = 'done'` need the same distinction or they count a dropped
    ticket as delivered."""
    con = _abandoned("reopened", "skipped_progress", "two_sprints")
    assert con.execute("SELECT count(*) FROM issues WHERE abandoned").fetchone()[0] == 2
    plain = _derived("reopened", "skipped_progress", "two_sprints")
    assert plain.execute("SELECT count(*) FROM issues WHERE abandoned").fetchone()[0] == 0


def test_abandonment_defaults_to_nothing_rather_than_guessing():
    """No status name is universal, so an unset config must not invent one: the
    safe default is that everything closed counts as delivered, as before."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    assert con.execute("SELECT count(*) FROM closures WHERE abandoned").fetchone()[0] == 0
    assert urd.load_scope(con)["abandoned_status"] is None


def test_an_abandoned_status_outside_the_done_category_is_rejected():
    """Naming an in-flight status would silently delete work from the delivered
    line while leaving it open, which is a worse error than a typo."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    scope = urd.load_scope(con)
    try:
        urd.derive(con, scope["status_order"], scope["start_status"], scope["review_status"],
                   abandoned_status="In Progress")
    except SystemExit as exc:
        assert "In Progress" in str(exc)
    else:
        raise AssertionError("an in-flight status was accepted as abandonment")


def test_observed_statuses_are_readable_before_any_view_exists():
    """The listing has to work on a first run, when --status-order is missing and
    derive has therefore built no transitions view to read. It parses raw_issues
    directly for exactly that reason."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "skipped_progress", "two_sprints")
    seen = urd.observed_statuses(con)
    assert seen, "no statuses found in the fetched data"
    by_name = {s["status"]: s for s in seen}
    assert {"To Do", "In Progress", "Review", "Done"} <= set(by_name)
    assert by_name["Review"]["entries"] >= 1
    assert by_name["Done"]["category"] == "done"


def test_observed_statuses_are_ordered_by_when_work_reaches_them():
    """The point of the listing is to make --status-order easy to write, so the
    order it suggests has to resemble the workflow rather than the alphabet."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "skipped_progress", "two_sprints")
    names = [s["status"] for s in urd.observed_statuses(con)]
    assert names.index("In Progress") < names.index("Done")
    assert names.index("To Do") < names.index("In Progress")


class _WorkflowJira:
    """A fake that answers the per-project status call.

    Standalone rather than subclassing _FieldJira, which is defined further down:
    a base class has to exist when the class statement runs, and these tests read
    better next to the feature than next to the fake.
    """

    def __init__(self, workflow=("To Do", "In Progress", "Done")):
        self.workflow = list(workflow)
        self.asked_projects = []

    def search(self, jql):
        yield "PROJ-1", "u1"

    def issue(self, key, fields):
        return {"key": key, "fields": {"updated": "u1"}}

    def fields(self):
        return []

    def statuses(self):
        return []

    def project_statuses(self, project):
        self.asked_projects.append(project)
        return [{"name": "Task", "statuses": [{"name": n} for n in self.workflow]}]


def test_sync_records_the_statuses_in_the_project_workflow():
    con = _scoped_db()
    jira = _WorkflowJira()
    urd.sync(con, jira)
    assert jira.asked_projects == ["PROJ"]
    stored = {r[0] for r in con.execute("SELECT status FROM workflow_statuses").fetchall()}
    assert stored == {"To Do", "In Progress", "Done"}


def test_each_project_in_a_comma_separated_scope_is_asked():
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ,OTHER",
                   earliest_since="2026-01-01")
    jira = _WorkflowJira()
    urd.sync(con, jira)
    assert jira.asked_projects == ["PROJ", "OTHER"]


def test_a_status_the_project_workflow_no_longer_has_is_marked_not_dropped():
    """Six of fifteen statuses on the real project are historical or arrived with
    tickets moved in from elsewhere. They are still real history, so they stay in
    the listing; they just leave the line the operator has to write."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    con.execute("INSERT INTO workflow_statuses VALUES ('To Do'), ('In Progress'), ('Done')")
    rows = {s["status"]: s for s in urd.observed_statuses(con)}
    assert rows["Review"]["in_workflow"] is False
    assert rows["In Progress"]["in_workflow"] is True
    listing = urd.format_statuses(list(rows.values()))
    assert "Review" in listing, "a retired status must still be listed"
    order = [ln for ln in listing.splitlines() if "--status-order" in ln][0]
    assert "Review" not in order, order
    assert "In Progress" in order


def test_no_workflow_information_means_every_status_counts():
    """The call needs no admin but can still fail, and an older database has no
    such table. Either way the listing must behave as it did before."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    rows = urd.observed_statuses(con)
    assert all(s["in_workflow"] for s in rows)
    order = [ln for ln in urd.format_statuses(rows).splitlines() if "--status-order" in ln][0]
    assert "Review" in order


def test_status_order_uses_each_ticket_s_first_arrival_not_every_arrival():
    """A status re-entered late drags its median past one reached once early, so
    counting every arrival inverted the pair it most needed to get right: against
    the real project it put the review status ahead of the in-progress status
    that feeds it, when that one precedes it
    precedes it 506 times to 39."""
    con = urd.open_db(_tmpdb())
    issue = {
        "key": "PROJ-9",
        "fields": {"created": "2026-01-01T00:00:00.000+0000",
                   "status": {"name": "Done", "statusCategory": {"key": "done"}}},
        "changelog": {"histories": [
            {"id": "1", "created": "2026-01-02T00:00:00.000+0000",
             "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]},
            {"id": "2", "created": "2026-01-03T00:00:00.000+0000",
             "items": [{"field": "status", "fromString": "In Progress", "toString": "Review"}]},
            # Sent back, so In Progress is entered a second time much later.
            {"id": "3", "created": "2026-01-31T00:00:00.000+0000",
             "items": [{"field": "status", "fromString": "Review", "toString": "In Progress"}]},
        ]},
    }
    con.execute("INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
                ["PROJ-9", "u1", urd._now(), json.dumps(issue)])
    names = [s["status"] for s in urd.observed_statuses(con)]
    assert names.index("In Progress") < names.index("Review"), names


def test_a_first_run_without_an_order_lists_the_statuses_it_found():
    """Otherwise the operator is told to supply an order for statuses they have
    no way to enumerate: derive is what would have told them, and it refuses to
    run without the answer."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened")
    try:
        urd.derive(con, None, None, None)
    except SystemExit as exc:
        message = str(exc)
        # NOT the status names: the hardcoded example in this message is
        # "To Do,In Progress,Review,Done", which is exactly the fixture's set, so
        # asserting on those passes with no listing present at all. That is how
        # this test was first written and it was green before the feature existed.
        # Assert on what only a real listing produces: the status categories.
        assert "indeterminate" in message, message
        assert "--status-order" in message
    else:
        raise AssertionError("derive ran without a status order")


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

    # Between calls, drop the base table to prove the second call rebuilds it.
    # changes_all, not changes: the latter is the scope-filtered view over it.
    con = urd.open_db(db)
    con.execute("DROP VIEW IF EXISTS changes")
    con.execute("DROP TABLE changes_all")
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
    jira = urd.Jira("example.invalid", "a@b.c", "t", opener=opener)
    assert [k for k, _ in jira.search("project = PROJ")] == ["PROJ-1", "PROJ-2"]
    assert len(opener.calls) == 2


def test_get_is_authenticated_with_basic_auth():
    seen = {}

    def opener(url, headers):
        seen.update(headers)
        return 200, b"{}"

    urd.Jira("example.invalid", "a@b.c", "tok", opener=opener).get("/field")
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
    got = urd.Jira("example.invalid", "a@b.c", "t", opener=opener).issue(
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
        jira = urd.Jira("example.invalid", "a@b.c", "t", opener=opener)
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
        jira = urd.Jira("example.invalid", "a@b.c", "t", opener=opener)
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
    jira = urd.Jira("example.invalid", "a@b.c", "t", opener=opener)
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
    jira = urd.Jira("example.invalid", "a@b.c", "t", opener=opener)
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
        jira = urd.Jira("example.invalid", "a@b.c", "t", opener=opener)
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

    jira = urd.Jira("example.invalid", "a@b.c", "t", opener=opener)
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
    urd.save_scope(con, site="example.invalid", project="PROJ", component="TEAM")
    assert urd.load_scope(con)["project"] == "PROJ"


def test_saving_scope_partially_keeps_the_rest():
    """A later `urd sync --since` must not wipe the site it was told once."""
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.invalid", project="PROJ")
    urd.save_scope(con, earliest_since="2026-01-01")
    scope = urd.load_scope(con)
    assert scope["site"] == "example.invalid"
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
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ",
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
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ",
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
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ",
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
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ",
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
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ",
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
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ",
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

    jira = urd.Jira("example.invalid", "a@b.c", "t", opener=ArrayResponseOpener())

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
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ",
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


class _FieldJira:
    """Fake instance whose custom field list is controllable, recording the
    `fields` string each issue fetch asked for."""

    def __init__(self, custom=(), asked=None, calls=None):
        self.custom = list(custom)
        self.asked = asked if asked is not None else []
        self.calls = calls if calls is not None else []

    def search(self, jql):
        yield "PROJ-1", "u1"

    def issue(self, key, fields):
        self.asked.append(fields)
        self.calls.append(key)
        return {"key": key, "fields": {"updated": "u1"}}

    def fields(self):
        return self.custom

    def statuses(self):
        return []


def _scoped_db():
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ",
                   earliest_since="2026-01-01")
    return con


def test_sync_fetches_the_custom_fields_derive_will_look_for():
    """The first live run reported story points 100% empty and zero sprints, and
    both were this one bug: FETCH_FIELDS held only built-in names, so the resolved
    custom field ids were never requested, and derive then read them out of JSON
    that could not have contained them. Five of the sixteen charts were dead by
    construction rather than for want of data."""
    con = _scoped_db()
    jira = _FieldJira([{"id": "customfield_10030", "name": "Story Points"},
                       {"id": "customfield_10020", "name": "Sprint"}])
    urd.sync(con, jira)
    assert jira.asked, "no issue was fetched at all"
    assert "customfield_10030" in jira.asked[0], f"Story Points absent: {jira.asked[0]}"
    assert "customfield_10020" in jira.asked[0], f"Sprint absent: {jira.asked[0]}"


def test_the_field_set_is_resolved_before_the_first_fetch_not_after():
    """_refresh_lookups used to run after the fetch loop, so on a first run the
    fields table was empty exactly when the field list was being built."""
    con = _scoped_db()
    jira = _FieldJira([{"id": "customfield_10030", "name": "Story Points"}])
    urd.sync(con, jira)
    assert "customfield_10030" in jira.asked[0], "first run fetched without the custom field"


def test_a_changed_field_set_refetches_everything():
    """`updated` has not moved, so the usual rule fetches nothing and the cached
    JSON would stay permanently missing the newly requested field."""
    con = _scoped_db()
    calls = []
    urd.sync(con, _FieldJira([], calls=calls))
    assert calls == ["PROJ-1"]
    urd.sync(con, _FieldJira([], calls=calls))
    assert calls == ["PROJ-1"], "an unchanged field set refetched anyway"
    urd.sync(con, _FieldJira([{"id": "customfield_10030", "name": "Story Points"}],
                             calls=calls))
    assert calls == ["PROJ-1", "PROJ-1"], "a changed field set did not refetch"


def test_sync_errors_are_pruned_for_keys_leaving_scope():
    """Keys that have left the scope are removed from sync_errors, but errors
    for keys still in scope survive the prune even if not refetched."""
    con = urd.open_db(_tmpdb())
    urd.save_scope(con, site="example.invalid", email="a@b.c", project="PROJ",
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
    urd.save_scope(con, site="example.invalid", email="stored@example.com",
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


def test_every_svg_chart_has_role_title_and_viewbox_scaling():
    """role="img" plus a leading <title> gives the chart its accessible
    name; preserveAspectRatio is what lets it scale down instead of
    cropping in a narrow window. Checked across every primitive that
    returns an <svg>, not just one."""
    samples = [
        render.lines([{"wk": "2026-01-05", "n": 1}], x="wk", series=["n"]),
        render.stacked(
            [{"wk": "2026-01-05", "status": "Done", "n": 1}], x="wk", band="status", value="n",
        ),
        render.scatter([{"x": 1, "y": 2}], x="x", y="y"),
        render.hbars([{"label": "a", "n": 1}], labels="label", series=["n"]),
        render.combo([{"wk": "2026-01-05", "n": 1, "m": 2}], x="wk",
                     series=["n", "m"], bars=("n",)),
    ]
    for out in samples:
        assert 'role="img"' in out
        assert "preserveAspectRatio" in out
        after_open = out.split(">", 1)[1]
        assert after_open.startswith("<title>")


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
    samples = {
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
        "hbars": render.hbars(
            [{"label": "a", "n": 1}, {"label": "b", "n": 100}], labels="label", series=["n"],
        ),
        "combo": render.combo(
            [{"wk": "2026-01-05", "n": 1, "m": 100}, {"wk": "2026-01-12", "n": 90, "m": 4}],
            x="wk", series=["n", "m"], bars=("n",),
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


def test_none_category_renders_as_empty_text_not_the_literal_none():
    """table() already turns a None cell into empty text; the band and label
    charts used to render the literal word "None" for a NULL category (a LEFT
    JOIN with no match, or an ungrouped bucket)."""
    out = render.stacked([{"wk": "w1", "status": None, "n": 3}], x="wk", band="status", value="n")
    assert ">None<" not in out
    assert "<title>None:" not in out

    out = render.hbars([{"label": None, "n": 3}], labels="label", series=["n"])
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
    """The prior test's stacked scenario only ever had one distinct band, so
    _legend (which needs 2+ names to render at all) was never called with a
    None name; its own None-guard was a dead arm."""
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


def test_stacked_y_axis_widens_for_a_wide_value_too():
    """Not just lines/scatter (via axes()): stacked computes its own left
    padding the same way, and used to right-align a 7-digit tick label at the
    same fixed x=40 regardless of width."""
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


def test_axes_thins_category_labels_so_interior_ones_stay_in_frame():
    """From the first live report: created-versus-closed has 30 weekly buckets and
    an INTERIOR label overflowed the right edge by 4.2px. axes anchored only the
    first and last tick, so the second from the end still ran out, and 30
    ten-character dates overlapped into noise besides."""
    rows = [{"week": f"2026-{1 + i // 4:02d}-{1 + (i % 4) * 7:02d}",
             "created": i % 5, "closed": i % 3} for i in range(30)]
    out = render.lines(rows, x="week", series=["created", "closed"])
    _assert_content_within_viewbox(out, "lines 30 weeks")
    dates = [t for t in re.findall(r'class="tick"[^>]*>([^<]*)</text>', out)
             if t.startswith("2026")]
    assert 1 < len(dates) <= 8, f"30 buckets produced {len(dates)} date labels"


def test_stacked_keeps_its_total_label_in_frame_when_floors_accumulate():
    """The live cumulative flow has 9 bands and a tallest stack of 689. Each tiny
    band is floored to 1px, and nine of those lifted the total label 0.7px above
    the frame: the documented overshoot grows with band count."""
    rows = [{"day": f"d{i}", "status": s, "tickets": 1 if s != "big" else 400}
            for i in range(6)
            for s in ("big", "b", "c", "d", "e", "f", "g", "h", "i")]
    out = render.stacked(rows, x="day", band="status", value="tickets")
    _assert_content_within_viewbox(out, "stacked 9 bands")


def _epic_rows(n):
    return [{"epic": f"PROJ-{100 + i}", "delivered": i, "dropped": n - i, "open": i % 3}
            for i in range(n)]


def test_a_sortable_table_is_complete_before_any_script_runs():
    """The spec amendment allows script only as an addition. Every row, every
    number and the header text must be in the markup, so the page prints and
    archives exactly as it did when it was inert."""
    out = render.table(_epic_rows(5), headers=["epic", "delivered", "dropped", "open"],
                       shade="delivered", sortable=True)
    for row in _epic_rows(5):
        assert row["epic"] in out
    assert out.count("<tr") == 6, "five rows plus a header"
    assert "sortable" in out


def test_a_sortable_header_is_reachable_without_a_mouse():
    out = render.table(_epic_rows(3), headers=["epic", "delivered"], sortable=True)
    # (?=[ >]) so <thead> does not match: <th followed by "ead" is still a <th prefix.
    headers = re.findall(r"<th(?=[ >])[^>]*>", out)
    assert headers, out
    assert all('tabindex="0"' in h for h in headers), headers
    assert all("aria-sort=" in h for h in headers), headers


def test_a_plain_table_gains_no_script_hooks():
    """Sorting is opt-in per chart: a three-row matrix does not need it and must
    not carry the attributes that imply it."""
    out = render.table(_epic_rows(3), headers=["epic", "delivered"])
    assert "sortable" not in out
    assert "tabindex" not in out


def test_every_script_the_page_carries_is_inline():
    """The library is vendored, so the count is structural (library + wiring) plus
    one JSON island per interactive chart. What must hold regardless of that count
    is that not one of them is fetched."""
    html = render.page(_header(), [("S", [render.table(_epic_rows(2), headers=["epic"],
                                                       sortable=True)])])
    assert html.count("<script") >= 2, "library and wiring should both be present"
    assert not re.search(r"<script[^>]*\bsrc\s*=", html)
    assert "uPlot" in html, "the charting library is not embedded"


def _people_bars(n, prefix="Firstname Surname "):
    return [{"person": f"{prefix}{i}", "points": 100 - i * 3} for i in range(n)]


def test_hbars_writes_every_category_name_in_full():
    """The whole reason this renderer exists: a vertical bar gives a name 21px and
    gets "Phi…", where a horizontal one gives it a whole line."""
    out = render.hbars(_people_bars(20), labels="person", series=["points"])
    for row in _people_bars(20):
        # The label carries a <title> after its text, so the closing tag is not
        # what follows the name.
        assert f">{row['person']}<title>" in out, row["person"]
    assert "…" not in out, "a realistic name should never be cut"


def test_hbars_bar_length_is_proportional_to_value():
    out = render.hbars([{"k": "a", "v": 100}, {"k": "b", "v": 50}, {"k": "c", "v": 0}],
                       labels="k", series=["v"])
    widths = [float(w) for w in re.findall(r'<rect[^>]*class="bar"[^>]*width="([\d.]+)"', out)]
    assert len(widths) == 3, widths
    assert abs(widths[0] - 2 * widths[1]) < 1.0, widths
    assert widths[2] == 0, "a zero value must draw nothing, not a stub"


def test_hbars_grows_taller_rather_than_thinner_with_more_rows():
    """The failure this replaces is bars shrinking to a pixel. Height is the axis
    that scales here, so a row keeps its thickness however many there are."""
    small = render.hbars(_people_bars(5), labels="person", series=["points"])
    large = render.hbars(_people_bars(40), labels="person", series=["points"])
    height = lambda svg: float(re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', svg).group(1))  # noqa: E731
    assert height(large) > height(small) * 4
    bar_h = lambda svg: {float(h) for h in re.findall(  # noqa: E731
        r'<rect[^>]*class="bar"[^>]*height="([\d.]+)"', svg)}
    assert bar_h(small) == bar_h(large), "a row must not thin out as rows are added"


def test_hbars_stays_inside_its_frame_at_any_row_count():
    for n in (1, 5, 20, 40):
        _assert_content_within_viewbox(
            render.hbars(_people_bars(n), labels="person", series=["points"]), f"hbars n={n}")


def test_hbars_is_wider_than_the_other_charts():
    """The label gutter and the bars compete for the same width, and on the epic
    chart the labels wanted 612px of a 480px chart. Widening the chart is the way
    out of that: the page body is 1100px and only the SVG was capped at 480."""
    out = render.hbars(_people_bars(6), labels="person", series=["points"])
    width = float(re.search(r'viewBox="0 0 ([\d.]+)', out).group(1))
    assert width >= 640, width
    assert 'class="chart chart-wide"' in out, out[:200]
    assert ".chart-wide" in render.CSS, "the wider class must actually be styled"
    # A vertical chart is unchanged: it has no gutter to pay for.
    assert 'class="chart"' in render.lines([{"wk": "2026-01-05", "n": 1}],
                                          x="wk", series=["n"])


def test_hbars_leaves_room_for_the_longest_name():
    """The gutter grows to fit the longest label, up to a cap. Past the cap the
    label is cut rather than allowed off the edge, and the full text stays in the
    tooltip."""
    short = render.hbars([{"k": "a", "v": 1}], labels="k", series=["v"])
    long = render.hbars([{"k": "a" * 40, "v": 1}], labels="k", series=["v"])
    assert "<title>" + "a" * 40 + "</title>" in long, "the full name must survive"
    # (?<![a-z]) or this matches the rx="3" corner radius: [^>]*x=" cheerfully
    # eats the r. It did, and reported a gutter of 3 for every label length.
    gutter = lambda svg: float(re.search(  # noqa: E731
        r'<rect[^>]*class="bar"[^>]*(?<![a-z])x="([\d.]+)"', svg).group(1))
    assert gutter(long) > gutter(short)
    _assert_content_within_viewbox(long, "hbars long name")


def test_hbars_empty_data_renders_a_note():
    assert "no data" in render.hbars([], labels="k", series=["v"]).lower()


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


def test_the_backlog_chart_reconciles_with_the_open_ticket_count():
    """The strongest check available: the newest snapshot must equal the number of
    tickets actually open. It did not, twice over, and both were silent.

    Spans close at the moment derive ran, so a series running to current_date has
    a last snapshot after the data ends, which an inner join drops entirely rather
    than showing as zero: the chart simply stopped a week early. And comparing
    against left_at::DATE excludes the final day, because midnight on that date is
    not less than the date itself."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, rows = _flow_rows(con, "net_open")
    assert rows, "no snapshots at all"
    open_now = con.execute(
        "SELECT count(*) FROM issues WHERE status_category <> 'done'").fetchone()[0]
    assert rows[-1]["open_tickets"] == open_now, (rows[-1], open_now)


def test_the_cumulative_flow_reaches_the_same_horizon():
    """Same defect, same fix: its newest snapshot was dropped too, so the chart
    ended before the data did with nothing to say it had."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, backlog = _flow_rows(con, "net_open")
    _, flow = _flow_rows(con, "cfd")
    assert max(r["day"] for r in flow) == max(r["day"] for r in backlog)
    total = con.execute("SELECT count(*) FROM issues").fetchone()[0]
    newest = max(r["day"] for r in flow)
    assert sum(r["tickets"] for r in flow if r["day"] == newest) == total


def _combo_rows(n=12):
    return [{"week": f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}",
             "new_trend": 10 + i % 5, "done_trend": 4 + i % 3,
             "dropped_trend": i % 2, "net_trend": (i % 9) - 3} for i in range(n)]


def test_combo_draws_bars_and_lines_on_one_scale():
    """The point of combining them: the bars are the difference between the lines,
    so they only mean anything measured against the same axis."""
    out = render.combo(_combo_rows(), x="week",
                       series=["net_trend", "new_trend", "done_trend", "dropped_trend"],
                       bars=["net_trend"])
    assert out.count("<rect") >= 12, "no bars drawn"
    assert out.count("<path") >= 3, "no lines drawn"
    assert out.count("<svg") == 1, "one chart, one scale"
    _assert_content_within_viewbox(out, "combo")


def test_combo_keeps_a_zero_baseline_when_values_straddle_it():
    """A signed series is only readable against zero, and a shared axis must not
    float away from it just because the lines are all positive."""
    out = render.combo(_combo_rows(), x="week", series=["net_trend", "new_trend"],
                       bars=["net_trend"])
    # zero_floor puts zero inside the domain; it does not promise a tick label
    # exactly on it, and the baseline is what a reader reads a sign against. So
    # this checks the domain spans zero and the bars pivot on the drawn baseline.
    assert "axis-line" in out
    ticks = [float(t) for t in re.findall(r'class="tick"[^>]*>(-?[\d.]+)</text>', out)]
    assert min(ticks) <= 0 <= max(ticks), ticks
    # y1 precedes class in the emitted markup, and there are two axis lines now:
    # the one axes() puts at the plot bottom and the zero rule this chart adds.
    baselines = [float(v) for v in re.findall(
        r'<line[^>]*y1="([-\d.]+)"[^>]*class="axis-line"', out)]
    assert len(baselines) == 2, baselines
    baseline = min(baselines)
    tops = [float(y) for y in re.findall(
        r'<rect class="bar"[^>]*(?<![a-z])y="([-\d.]+)"', out)]
    assert min(tops) <= baseline <= max(tops) + 0.01, (baseline, min(tops), max(tops))


def test_combo_draws_its_bars_behind_its_lines():
    """Otherwise the bars hide the lines they are derived from."""
    out = render.combo(_combo_rows(), x="week", series=["net_trend", "new_trend"],
                       bars=["net_trend"])
    assert out.index("<rect") < out.index("<path"), "bars must come first in document order"


def test_combo_stays_in_frame_across_counts():
    for n in (1, 5, 32, 125):
        _assert_content_within_viewbox(
            render.combo(_combo_rows(n), x="week",
                         series=["net_trend", "new_trend"], bars=["net_trend"]),
            f"combo n={n}")


def test_the_combined_chart_carries_a_type_per_series():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "flow_trend")
    assert chart.kind == "combo"
    assert chart.options["bars"] == ["net_trend"]
    payload = render.plot_payload(chart, rows)
    kinds = {s["label"]: s["type"] for s in payload["series"]}
    assert kinds["net_trend"] == "bars"
    assert kinds["new_trend"] == "line"


def test_the_separate_net_chart_is_gone():
    """Combined, not duplicated: the same numbers on two charts is how they drift."""
    assert "net_flow" not in {c.key for c in chart_specs.CHARTS}


def test_the_net_bars_reconcile_with_the_lines_beside_them():
    """The bars sit on the same axis as the lines and claim to be their difference,
    so they have to actually be it, row by row. Subtracting delivered alone would
    overstate the gap, which is the error the dropped line was added to fix."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, rows = _flow_rows(con, "flow_trend")
    assert rows
    for row in rows:
        expected = (row["new_trend"] or 0) - (row["done_trend"] or 0) \
            - (row["dropped_trend"] or 0)
        assert abs(row["net_trend"] - expected) < 0.05, row


def test_the_trend_chart_smooths_both_series_over_four_weeks():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "flow_trend")
    assert chart.options["series"] == [
        "net_trend", "new_trend", "done_trend", "dropped_trend"]
    assert rows, "no weeks at all"
    weeks = [r["week"] for r in rows]
    assert weeks == sorted(weeks)
    # Gapless: a week where nothing happened is a zero, not a missing row, or a
    # four-week mean would average four arbitrary weeks instead of four calendar
    # ones and quietly overstate a quiet period.
    assert len(set(weeks)) == len(weeks)
    assert (weeks[-1] - weeks[0]).days // 7 + 1 == len(weeks), weeks


def test_the_trend_counts_dropped_work_as_leaving_the_backlog():
    """Work leaves the backlog by being delivered OR dropped. Without the dropped
    line the chart implies a backlog growing at more than twice the real rate: on
    the live project, new minus delivered came to 271 over 26 weeks against an
    observed 121."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "flow_trend")
    assert "dropped_trend" in chart.options["series"]
    dropped = con.execute(
        "SELECT count(*) FROM closures WHERE abandoned").fetchone()[0]
    total = sum(r["dropped_trend"] or 0 for r in rows)
    assert (total > 0) == (dropped > 0), (total, dropped)


def test_the_trend_at_a_window_edge_uses_the_weeks_before_it():
    """A rolling mean computed after filtering restarts at the window, so the
    first weeks shown would average one, two, then three weeks and read as a
    ramp that never happened. It is computed over everything and filtered after."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, whole = _flow_rows(con, "flow_trend")
    edge = whole[len(whole) // 2]["week"]
    urd.set_report_window(con, edge.isoformat())
    _, windowed = _flow_rows(con, "flow_trend")
    assert windowed, "the window emptied the chart"
    first = windowed[0]
    matching = next(r for r in whole if r["week"] == first["week"])
    assert first["new_trend"] == matching["new_trend"], (first, matching)
    urd.set_report_window(con, None)


def test_flow_health_charts_are_all_present():
    keys = {c.key for c in chart_specs.CHARTS if c.section == "Flow health"}
    assert keys == {"aging_wip", "created_vs_closed", "flow_trend", "net_open",
                    "flow_per_sprint", "cfd", "cycle_scatter", "time_in_status"}


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


def test_every_table_chart_is_sortable():
    """Every table in the report is 40 rows or more, which is past the point of
    scanning by eye. Asserted as a rule over the specs rather than chart by chart
    so a table added later has to make the same decision deliberately: a genuinely
    short one may opt out, but it cannot forget."""
    tables = [c for c in chart_specs.CHARTS if c.kind in ("table", "matrix")]
    assert len(tables) >= 3, tables
    missing = [c.key for c in tables if not c.options.get("sortable")]
    assert not missing, f"table charts without sorting: {missing}"


def test_a_sortable_spec_actually_renders_the_sort_hooks():
    """The option is only a promise until figure passes it through; this is the
    end-to-end half, so a spec saying sortable cannot render an inert table."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "aging_wip")
    out = render.figure(chart, rows, "sub", con)
    assert 'class="urd sortable"' in out, out[:300]
    assert 'aria-sort="none"' in out


def test_time_in_status_shows_the_population_each_median_rests_on():
    """Flows skip statuses, so the rows of this matrix are medians over different
    and sometimes tiny populations: on the real project one status carried 1145
    tickets and another carried 1. Shaded by days alone they look equally solid,
    so the count has to be visible in the table."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "time_in_status")
    assert "tickets" in chart.options["headers"], chart.options["headers"]
    assert chart.options["shade"] == "days", "shading still belongs on the duration"
    assert rows and all(r["tickets"] >= 1 for r in rows)
    # PROJ-2 skips In Progress entirely, so that row must not claim every ticket.
    total = con.execute("SELECT count(*) FROM issues").fetchone()[0]
    assert any(r["tickets"] < total for r in rows), rows


def test_time_in_status_ignores_time_spent_already_done():
    """status_durations closes an open span at now(), so a closed ticket's Done
    span measures time since resolution. It read 207 days in the fixtures."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, rows = _flow_rows(con, "time_in_status")
    assert rows
    assert "Done" not in {r["status"] for r in rows}


def test_a_threshold_override_changes_whether_a_chart_draws():
    """The knobs had been retuned twice by editing source. Cycle time covers 1 of
    2 in the fixtures, exactly the default tier, so a nudge either way flips it."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart = next(c for c in chart_specs.CHARTS if c.key == "cycle_scatter")
    lenient = urd.run_chart(con, chart, {"default": 0.5, "points": 0.35})
    strict = urd.run_chart(con, chart, {"default": 0.6, "points": 0.35})
    assert "<svg" in lenient
    assert "<svg" not in strict and "1 of 2" in strict


def test_a_points_chart_follows_its_own_tier_not_the_default():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart = next(c for c in chart_specs.CHARTS if c.key == "points_vs_cycle")
    assert chart.tier == "points"
    # Default set impossibly high, points tier at zero: the points chart must
    # still draw, which it cannot do if it is reading the wrong tier.
    out = urd.run_chart(con, chart, {"default": 1.0, "points": 0.0})
    assert "<svg" in out


def test_an_unknown_threshold_tier_is_rejected_rather_than_ignored():
    """A typo that is silently dropped leaves the operator certain they changed
    something, which is the failure mode this whole tool keeps tripping over."""
    try:
        urd.parse_thresholds(["poitns=0.4"])
    except SystemExit as exc:
        assert "poitns" in str(exc) and "points" in str(exc)
    else:
        raise AssertionError("a mistyped tier was accepted")


def test_a_threshold_that_is_not_a_share_is_rejected():
    for bad in ("points=high", "points=1.5", "points=-0.2", "points", "=0.4"):
        try:
            urd.parse_thresholds([bad])
        except SystemExit:
            continue
        raise AssertionError(f"{bad!r} was accepted")


def test_thresholds_round_trip_through_the_database():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    urd.save_scope(con, thresholds=urd.format_thresholds({"default": 0.9, "points": 0.1}))
    got = urd.stored_thresholds(con)
    assert got["default"] == 0.9 and got["points"] == 0.1
    # An unset database falls back to the declared defaults, not to zero.
    fresh = urd.open_db(_tmpdb())
    assert urd.stored_thresholds(fresh) == chart_specs.THRESHOLDS


def test_a_chart_sitting_exactly_on_its_threshold_draws():
    """skipped_progress resolves without ever entering start_status, so cycle time
    covers 1 of 2, which is exactly the default tier once it dropped to 0.5.

    run_chart strips on `share < threshold`, so the threshold is the minimum
    coverage required rather than a bar to clear, and a chart landing precisely on
    it draws. Worth pinning rather than deleting: coverage figures land on round
    boundaries often, and this exact chart crossed one when the default moved."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart = next(c for c in chart_specs.CHARTS if c.key == "cycle_scatter")
    numerator, denominator = con.execute(chart.coverage).fetchone()
    assert (numerator, denominator) == (1, 2)
    limit = chart_specs.THRESHOLDS[chart.tier]
    assert numerator / denominator == limit, "fixture no longer sits on the boundary"
    out = urd.run_chart(con, chart)
    assert "<svg" in out, "a chart exactly at its threshold should draw"
    assert "1 of 2 tickets" in out, "coverage still belongs in the caption when it draws"


def test_a_chart_just_below_its_threshold_strips():
    """The other side of the same boundary, on the same chart, so the two cannot
    drift apart: one nudge downward and it must refuse to draw."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart = next(c for c in chart_specs.CHARTS if c.key == "cycle_scatter")
    nudged = dict(chart_specs.THRESHOLDS)
    nudged[chart.tier] += 0.01
    out = urd.run_chart(con, chart, nudged)
    assert "<svg" not in out
    assert "1 of 2" in out


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
            "synced": "2026-08-13T17:00:00", "errors": 0, "issues": 41,
            "window": None, "exempt": [], "excluded": []}
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
    """The page ships several <script> elements of its own, so counting them
    against a fixed number tests the page's structure rather than its escaping.
    The question is whether DATA can add one, so the same page is rendered with
    and without a hostile value and the counts must match."""
    hostile = render.page(_header(project="P&D", component="<script>x</script>"), [])
    benign = render.page(_header(project="P&D", component="TEAM"), [])
    assert "<script>x</script>" not in hostile, "the scope value was injected raw"
    assert "&lt;script&gt;x&lt;/script&gt;" in hostile, "the scope value was not escaped"
    assert hostile.count("<script") == benign.count("<script"), "data added a script element"
    assert "P&amp;D" in hostile


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


# Constructs a browser acts on by itself. Not URLs: a URL in a comment or in an
# anchor fetches nothing, and the vendored library carries its attribution URL in
# a banner comment. Checking for the construct is both stricter (it catches
# fetch("/relative")) and honest about what the guarantee is.
_FETCHING = (
    r"\bsrc\s*=", r"@import", r"url\(", r"\bfetch\s*\(", r"XMLHttpRequest",
    r"\bWebSocket\b", r"sendBeacon", r"EventSource", r"importScripts",
    r"\bimport\s*\(",
)


def _assert_nothing_is_fetched(html, label=""):
    """Nothing in this page causes a network request when it is opened.

    Anchors are removed first: a link is followed only if a human clicks it,
    unlike everything in _FETCHING, which the browser acts on with no choice.
    `href` is then forbidden in what remains, which is what keeps a stylesheet
    <link> out while leaving <a href> in.
    """
    without_anchors = re.sub(r"<a\b[^>]*>", "", html)
    for pattern in _FETCHING + (r"\bhref\s*=",):
        found = re.search(pattern, without_anchors)
        assert not found, f"{label}{pattern} can reach the network: {found.group(0)!r}"


def test_report_writes_a_standalone_file_with_no_external_references():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    out = os.path.join(tempfile.mkdtemp(), "report.html")
    assert urd.report(con, out) == 0
    html = pathlib.Path(out).read_text()
    assert html.startswith("<!doctype html>")
    _assert_nothing_is_fetched(html)


def test_the_fetch_check_is_not_blinded_by_its_own_exemptions():
    """Two exemptions live in that helper: anchors are stripped, and URLs are not
    themselves an offence. Both are the kind that quietly widen until nothing is
    caught, so each fetching form is fed in and must still be rejected."""
    must_catch = [
        '<img src="https://x/y.png">',
        '<img src="/local/y.png">',
        '<link href="x.css" rel="stylesheet">',
        "<style>@import 'x.css';</style>",
        "<style>a{background:url(x.png)}</style>",
        '<script>fetch("/telemetry")</script>',
        "<script>new XMLHttpRequest()</script>",
        "<script>new WebSocket('wss://x')</script>",
        "<script>navigator.sendBeacon('/x')</script>",
        "<script>new EventSource('/x')</script>",
        "<script>import('/x.js')</script>",
    ]
    for markup in must_catch:
        try:
            _assert_nothing_is_fetched(markup)
        except AssertionError:
            continue
        raise AssertionError(f"not caught: {markup}")
    # And the two things that must stay allowed.
    _assert_nothing_is_fetched(
        '<a href="https://example.invalid/browse/PROJ-1">PROJ-1</a>')
    _assert_nothing_is_fetched("<script>/*! https://github.com/leeoniya/uPlot */</script>")


def test_an_interactive_chart_still_ships_its_server_drawn_svg():
    """The whole basis of allowing a library: the page prints, and a browser with
    script disabled shows exactly what it showed before. The upgrade replaces the
    SVG at runtime; it never replaces it in the file."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "created_vs_closed")
    out = render.figure(chart, rows, "sub", con)
    assert "<svg" in out, "the static chart is missing"
    assert 'class="plot"' in out and 'class="plot-data"' in out


def test_the_data_island_carries_the_numbers_the_svg_was_drawn_from():
    """Script navigates data, it does not produce it. Every value a reader can
    hover has to be one Python already computed, or two reports of one database
    stop diffing."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "created_vs_closed")
    payload = render.plot_payload(chart, rows)
    assert [s["label"] for s in payload["series"]] == chart.options["series"]
    assert len(payload["x"]) == len(rows)
    for series in payload["series"]:
        assert series["data"] == [r[series["label"]] for r in rows]
    assert payload["time"] is True, "a weekly axis should be a time axis"


def test_a_stacked_island_carries_cumulative_sums_computed_in_python():
    """uPlot stacks by drawing cumulative series and letting later ones paint over
    earlier. Those sums are the numbers on screen, so they are computed here, not
    in the browser: the spec rule is that script navigates data, never produces
    it. Each series also carries its own value, so hovering reads the band rather
    than the running total."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "cfd")
    payload = render.plot_payload(chart, rows)
    assert payload["kind"] == "stacked"
    by_x = {}
    for r in rows:
        by_x.setdefault(r["day"], {})[r["status"]] = r["tickets"]
    for index, x in enumerate(sorted(by_x)):
        running = 0
        # Series arrive in draw order, largest cumulative first, so the natural
        # band order is the reverse of it.
        for series in reversed(payload["series"]):
            running += series["raw"][index] or 0
            assert series["data"][index] == running, (series["label"], x)
        assert running == sum(by_x[x].values())


def test_a_stack_past_the_palette_folds_its_tail_into_other():
    """_slot cycles past 8, so a 10-band stack drew two pairs of bands in the same
    colour, which in a stack cannot be told apart at all. Its docstring already
    said the caller must collapse the tail; nobody was. The largest bands keep
    their identity and the remainder becomes one honest Other."""
    rows = [{"day": f"2026-01-{d:02d}", "status": f"S{i}", "tickets": 100 - i}
            for d in (1, 2) for i in range(12)]
    folded = render.fold_bands(rows, x="day", band="status", value="tickets")
    labels = list(dict.fromkeys(r["status"] for r in folded))
    assert len(labels) == 8, labels
    assert labels[-1] == "Other"
    # Nothing is lost: the totals per x still match.
    for day in ("2026-01-01", "2026-01-02"):
        before = sum(r["tickets"] for r in rows if r["day"] == day)
        after = sum(r["tickets"] for r in folded if r["day"] == day)
        assert before == after, day
    # And the smallest originals are the ones that went in.
    assert "S11" not in labels and "S0" in labels


def test_a_stack_within_the_palette_is_left_alone():
    rows = [{"day": "d", "status": f"S{i}", "tickets": 1} for i in range(8)]
    assert render.fold_bands(rows, x="day", band="status", value="tickets") == rows


def test_no_two_bands_in_a_rendered_stack_share_a_colour():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    for key in ("cfd", "type_mix"):
        chart, rows = _flow_rows(con, key)
        payload = render.plot_payload(chart, rows)
        slots = [s["slot"] for s in payload["series"]]
        assert len(slots) == len(set(slots)), f"{key}: duplicate colours {slots}"


def test_a_stacked_band_keeps_the_colour_its_svg_twin_used():
    """Draw order is reversed for stacking, so a series' position in the payload
    is no longer its palette slot. Carrying the slot explicitly is what stops an
    upgraded chart recolouring every band the moment it loads."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "cfd")
    payload = render.plot_payload(chart, rows)
    bands = list(dict.fromkeys(r["status"] for r in rows))
    for series in payload["series"]:
        assert series["slot"] == bands.index(series["label"]) % 8 + 1, series["label"]


def test_a_data_island_cannot_close_its_own_script_tag():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "created_vs_closed")
    hostile = chart._replace(title="</script><script>x</script>")
    out = render.figure(hostile, rows, "sub", con)
    assert "</script><script>x" not in out


def test_interactivity_is_declared_on_plot_kinds_only():
    """A chart that is readable as drawn is finished when it is drawn.
    Interactivity is a fix for density, not a badge every chart collects."""
    for chart in chart_specs.CHARTS:
        if chart.options.get("interactive"):
            assert chart.kind in ("lines", "scatter", "stacked",
                                  "combo"), f"{chart.key}: {chart.kind}"


def test_a_stacked_band_is_filled_opaquely():
    """A stack is drawn largest cumulative first with smaller bands painted over
    it, so a translucent fill shows every band underneath and renders each one as
    a blend of itself and all its predecessors. The static SVG fills opaquely;
    the upgraded chart has to agree or the two disagree on sight.

    Source-level, because the alternative is a browser. It reads the one line that
    decides the fill rather than the whole script."""
    # Both "fill:" and "stacked": three lines mention a fill, and the other two
    # are fillAlpha and the points config.
    candidates = [ln for ln in render.PLOT_SCRIPT.splitlines()
                  if "fill:" in ln and "stacked" in ln]
    assert len(candidates) == 1, candidates
    fill_line = candidates[0]
    assert "+ '66'" not in fill_line and '+ "66"' not in fill_line, (
        f"translucent stacked fill: {fill_line.strip()}")


def test_the_embedded_javascript_parses():
    """JavaScript living in a Python string is the one part of this report that
    nothing else checks: ruff does not read it, and a syntax error ships a page
    whose charts silently never upgrade. node --check is the cheapest real check
    available. Where node is absent it degrades to a structural one rather than
    quietly passing."""
    import shutil
    import subprocess
    for name, source in (("sort", render.SORT_SCRIPT), ("plot", render.PLOT_SCRIPT)):
        path = os.path.join(tempfile.mkdtemp(), f"{name}.js")
        pathlib.Path(path).write_text(source)
        node = shutil.which("node")
        if node:
            done = subprocess.run([node, "--check", path], capture_output=True, text=True)
            assert done.returncode == 0, f"{name}.js does not parse:\n{done.stderr}"
        else:
            assert source.count("{") == source.count("}"), f"{name}.js braces"
            assert source.count("(") == source.count(")"), f"{name}.js parens"


def test_the_vendored_library_is_present_and_makes_no_requests():
    """It is committed rather than fetched so a saved report opens offline. An
    upgrade that introduces a network call must fail here, not in a reader's
    browser."""
    root = pathlib.Path(__file__).parent
    js = (root / "vendor" / "uplot.min.js").read_text()
    css = (root / "vendor" / "uplot.min.css").read_text()
    assert len(js) > 10_000, "vendored library looks truncated"
    for pattern in _FETCHING:
        assert not re.search(pattern, js), f"vendored js can reach the network: {pattern}"
        assert not re.search(pattern, css), f"vendored css can reach the network: {pattern}"
    assert "uPlot" in js
    assert (root / "vendor" / "README.md").exists(), "provenance must be recorded"


def test_a_ticket_key_links_to_the_configured_site():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    urd.save_scope(con, site="example.invalid")
    chart, rows = _flow_rows(con, "aging_wip")
    out = urd.run_chart(con, chart)
    assert 'href="https://example.invalid/browse/PROJ-3"' in out, out[:400]
    assert ">PROJ-3</a>" in out
    # Only the linked column becomes a link; a status is not a ticket.
    assert "browse/In Progress" not in out


def test_no_site_means_no_links_rather_than_a_broken_one():
    """A database that has never synced has no site, and half a URL is worse than
    plain text."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    urd.save_scope(con, site=None)
    con.execute("UPDATE sync_state SET site = NULL")
    chart, rows = _flow_rows(con, "aging_wip")
    out = urd.run_chart(con, chart)
    assert "href=" not in out
    assert "PROJ-3" in out


def test_a_linked_key_is_escaped_in_both_the_href_and_the_text():
    out = render.table([{"key": 'A"><script>x</script>', "days": 1}],
                       headers=["key", "days"],
                       link_base="https://example.invalid/browse/",
                       links=["key"])
    assert "<script>" not in out
    assert '"><script' not in out


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
    # issues is a view now; the base table is what a test can write to.
    con.execute("UPDATE issues_all SET fix_versions = ['R1', 'R2'] WHERE key = 'PROJ-1'")
    _, rows = _flow_rows(con, "per_fix_version")
    counts = {r["fix_version"]: (r["delivered"], r["dropped"], r["open"]) for r in rows}
    # PROJ-1 is delivered and now in both; PROJ-3 is open and in R2 only.
    assert counts == {"R1": (1, 0, 0), "R2": (1, 0, 1)}


def test_issues_carry_their_summary():
    """Fetched since the first sync and never stored, exactly like resolution was.
    No refetch needed: it is already sitting in raw_issues."""
    con = _derived("reopened", "two_sprints")
    rows = con.execute("SELECT key, summary FROM issues ORDER BY key").fetchall()
    assert rows and all(summary for _, summary in rows), rows


def test_the_epic_chart_labels_carry_the_title_as_well_as_the_key():
    """A key alone tells you nothing about which epic you are looking at, and the
    tooltip is where the label has room to say it."""
    con = _derived("reopened", "two_sprints")
    # PROJ-100 is the parent in the fixtures and is not itself a fetched issue,
    # so give it one to stand for an epic inside the report's scope.
    con.execute("INSERT INTO issues_all (key, project, type, status, status_category, "
                "created, summary) VALUES ('PROJ-100', 'PROJ', 'Epic', 'To Do', 'new', "
                "TIMESTAMP '2026-01-01 09:00', 'Rebuild the widget pipeline')")
    _, rows = _flow_rows(con, "per_epic")
    labels = [r["epic"] for r in rows]
    assert any("PROJ-100" in x and "Rebuild the widget pipeline" in x for x in labels), labels


def test_per_epic_breaks_a_tie_so_two_renders_of_one_database_agree():
    """The cap was `ORDER BY count(*) DESC LIMIT 40`, so epics on equal counts came
    back in an arbitrary order: which of them survived the cap, and in what order,
    varied between runs on identical data. Two renders of one database are meant to
    diff, which the README states as a design property, and they did not.

    Eight tied epics rather than two. Two passed the unfixed query on the first
    try, because DuckDB happened to return that pair in order; eight produced six
    distinct orderings over thirty runs of one process, none of them sorted. The
    parents are inserted alphabetically backwards on top of that, so insertion
    order cannot be mistaken for a working tiebreaker."""
    con = _derived("reopened", "two_sprints")
    parents = [f"PROJ-{n}" for n in range(800, 100, -100)]
    for i, parent in enumerate(parents):
        con.execute("INSERT INTO issues_all (key, project, type, status, "
                    "status_category, created, summary, parent, abandoned) VALUES "
                    "(?, 'PROJ', 'Task', 'To Do', 'new', TIMESTAMP '2026-01-06 09:00', "
                    "'tied', ?, FALSE)", [f"PROJ-9{i}", parent])
    _, rows = _flow_rows(con, "per_epic")
    labels = [r["epic"] for r in rows]
    # PROJ-100 holds two fixture tickets and leads on count alone. The other seven
    # hold one each, so nothing but the tiebreaker can order them.
    assert labels == ["PROJ-100"] + sorted(parents), labels


def test_an_epic_outside_the_report_falls_back_to_its_key():
    """Parents are routinely outside the fetched scope, and half a label is worse
    than a bare key."""
    con = _derived("reopened", "two_sprints")
    _, rows = _flow_rows(con, "per_epic")
    labels = [r["epic"] for r in rows]
    assert labels == ["PROJ-100"], labels


def test_the_aging_table_shows_what_each_ticket_is():
    """The chart that changes what you do today, and it listed forty keys with no
    hint of what any of them were."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "aging_wip")
    assert "summary" in chart.options["headers"]
    assert rows and all(r["summary"] for r in rows), rows


def test_the_epic_chart_is_horizontal_and_capped():
    """Its labels are a key plus a title, which a vertical axis gives three
    characters. Horizontal gives them a line each, at the cost of height: all 138
    epics would be 9108px, so it is capped and the caption says so."""
    chart = next(c for c in chart_specs.CHARTS if c.key == "per_epic")
    assert chart.kind == "hbars"
    assert "LIMIT" in chart.sql.upper()
    assert "not shown" in chart.caption or "largest" in chart.caption
    # hbars is deliberately not upgraded, so the chart must not claim to be.
    assert not chart.options.get("interactive")


def test_the_epic_chart_shows_the_biggest_epics_not_the_first_ones():
    """A cap is only honest if it keeps the rows worth keeping. Ordering by size
    before limiting is what makes 40 rows a summary rather than an accident."""
    con = _derived("reopened", "two_sprints")
    chart, rows = _flow_rows(con, "per_epic")
    totals = [r["delivered"] + r["dropped"] + r["open"] for r in rows]
    assert totals == sorted(totals, reverse=True), totals


def test_epic_chart_series_are_disjoint_and_sum_to_the_total():
    """done against total double-counts: the first bar is contained in the second,
    and grouped bars read as disjoint quantities. delivered/dropped/open are
    genuinely disjoint, which is the property worth asserting rather than the
    particular names."""
    con = _derived("reopened", "two_sprints")
    chart, rows = _flow_rows(con, "per_epic")
    assert chart.options["series"] == ["delivered", "dropped", "open"]
    assert [(r["epic"], r["delivered"], r["dropped"], r["open"]) for r in rows] == [
        ("PROJ-100", 1, 0, 1)]
    total = con.execute(
        "SELECT count(*) FROM issues WHERE parent IS NOT NULL").fetchone()[0]
    assert sum(r["delivered"] + r["dropped"] + r["open"] for r in rows) == total


def test_dropped_work_never_counts_as_delivered_in_any_chart():
    """The whole point of the split: a status declared abandoned must move out of
    every delivery series at once, not just the one that prompted the change."""
    con = _abandoned("reopened", "two_sprints")
    _, epic = _flow_rows(con, "per_epic")
    assert [(r["delivered"], r["dropped"]) for r in epic] == [(0, 1)]
    _, version = _flow_rows(con, "per_fix_version")
    assert all(r["delivered"] == 0 for r in version), version
    assert sum(r["dropped"] for r in version) == 1
    _, flow = _flow_rows(con, "created_vs_closed")
    assert sum(r["delivered"] for r in flow) == 0
    assert sum(r["dropped"] for r in flow) == 1


def test_the_real_section_list_leads_in_the_declared_order():
    """The stand-in list in test_sections_render_in_a_fixed_order proves the
    ordering rule; this one proves the real charts obey it.

    It asserts a prefix rather than the whole list on purpose: pinning the exact
    list means every task that populates a new section breaks this test and gets
    it "fixed" by pasting in the new answer, which is how a test stops being read.
    Comparing against a rebuild of render_sections' own filter would be worse
    still, since reversing SECTIONS would reorder both sides and prove nothing."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    titles = [title for title, _ in urd.render_sections(con)]
    assert len(titles) >= 2, "needs two populated sections to say anything about order"
    assert titles[:2] == ["Flow health", "Reporting outward"]
    assert set(titles) <= set(chart_specs.SECTIONS)


def test_retro_charts_are_all_present():
    keys = {c.key for c in chart_specs.CHARTS if c.section == "Retro"}
    assert keys == {"rework_per_sprint", "carry_over", "cycle_per_sprint",
                    "points_vs_cycle", "carried_sprints", "points_per_sprint"}


def test_a_mutation_lands_in_at_most_one_sprint():
    """The rule has to be a function, not a preference: a mutation attributed to
    two sprints would be counted twice and every per-sprint total would be wrong
    in a way nothing else would show."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    doubled = con.execute("""
        SELECT count(*) FROM (
            SELECT key, ts, kind FROM mutation_sprint GROUP BY 1, 2, 3 HAVING count(*) > 1)
    """).fetchone()[0]
    assert doubled == 0


def test_membership_settles_a_mutation_inside_two_sprints():
    """56% of the ambiguous mutations on the real project are settled this way:
    two sprints are running, the ticket belongs to one of them, so that is the
    one. Without the rule they would all be unattributed."""
    con = _derived("reopened", "two_sprints")
    # Overlap the two fixture sprints so a mutation falls inside both. PROJ-1,
    # not PROJ-3: PROJ-3 belongs to BOTH sprints, so membership cannot settle it
    # and it is correctly left unattributed. PROJ-1 belongs to Sprint B alone.
    con.execute("UPDATE issue_sprints_all SET start = TIMESTAMP '2026-01-05 09:00' "
                "WHERE sprint_name = 'Sprint A'")
    con.execute("CREATE OR REPLACE VIEW mutation_sprint AS " + urd.MUTATION_SPRINT_SQL)
    # Only the overlap matters. A PROJ-1 mutation after Sprint B ends falls inside
    # Sprint A alone, and rule 2 correctly gives it to Sprint A.
    rows = con.execute("""
        SELECT sprint_name, count(*) FROM mutation_sprint
        WHERE key = 'PROJ-1' AND ts < TIMESTAMP '2026-01-19 09:00' GROUP BY 1
    """).fetchall()
    assert rows, "every PROJ-1 mutation in the overlap went unattributed"
    assert [name for name, _ in rows] == ["Sprint B"], rows
    # And the ticket in both stays unattributed rather than being assigned one.
    both = con.execute(
        "SELECT count(*) FROM mutation_sprint WHERE key = 'PROJ-3'").fetchone()[0]
    assert both == 0, "a ticket in both sprints was given one anyway"


def test_an_unattributable_mutation_is_dropped_not_guessed():
    """Two sprints running and the ticket in neither: there is no answer, and
    inventing one would put work in a sprint it had nothing to do with."""
    con = _derived("reopened", "two_sprints")
    total = con.execute("SELECT count(*) FROM mutations").fetchone()[0]
    attributed = con.execute("SELECT count(*) FROM mutation_sprint").fetchone()[0]
    assert attributed <= total
    assert total > 0


def test_a_coverage_figure_names_the_thing_it_counted():
    """Every coverage line said "tickets". This chart counts mutations, and 15078
    of 22347 tickets is not a true sentence about a project with 1151 of them."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart = next(c for c in chart_specs.CHARTS if c.key == "flow_per_sprint")
    assert chart.options.get("unit") == "mutations"
    out = urd.run_chart(con, chart)
    assert "mutations)" in out, out[-400:]
    assert "tickets)" not in out
    # And a chart that really does count tickets still says tickets.
    other = next(c for c in chart_specs.CHARTS if c.key == "cycle_scatter")
    assert "tickets" in urd.run_chart(con, other)


def test_the_per_sprint_flow_chart_states_how_much_it_covers():
    """Two thirds of mutations attribute on the real project, so a chart that
    said nothing about the other third would overstate what it knows."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    chart, rows = _flow_rows(con, "flow_per_sprint")
    assert chart.coverage, "a chart built on partial attribution needs coverage"
    numerator, denominator = con.execute(chart.coverage).fetchone()
    assert 0 <= numerator <= denominator


def test_the_sprint_charts_run_newest_first():
    """Horizontal bars read top-down, so newest first puts the sprint you care
    about at the top instead of at the end of a fifty-row scroll."""
    con = _derived("reopened", "two_sprints")
    for key, column in (("rework_per_sprint", "sprint"),
                        ("carry_over", "sprint"),
                        ("cycle_per_sprint", "sprint")):
        chart, rows = _flow_rows(con, key)
        names = [r[column] for r in rows]
        # The fixtures run Sprint B (starting 01-05) then Sprint A (01-19), named
        # so that start order disagrees with both name and id order.
        assert names == sorted(names), f"{key}: {names}"
        assert names[0] == "Sprint A" or len(names) < 2, f"{key}: {names}"


def test_rework_is_attributed_to_the_sprint_it_happened_in():
    """No fixture ticket has both a rework row and more than one sprint, so on
    fixture data alone "attribute by transition time" and "attribute to the
    ticket's last sprint" return the same answer and this test cannot tell them
    apart. PROJ-1 gets a later second sprint here so the two models disagree."""
    con = _derived("reopened", "two_sprints")
    con.execute(
        "INSERT INTO issue_sprints_all VALUES ('PROJ-1', 3, 'Sprint A', 'closed', "
        "TIMESTAMP '2026-01-19 09:00', TIMESTAMP '2026-02-02 09:00', 2)")
    _, rows = _flow_rows(con, "rework_per_sprint")
    # The backward move is at 2026-01-09, inside Sprint B's window. Attributing to
    # the ticket's last sprint would answer Sprint A.
    assert [(r["sprint"], r["backward_moves"]) for r in rows] == [("Sprint B", 1)]


def test_a_sprint_chart_with_no_sprints_says_so_rather_than_drawing_nothing():
    """From the first live run: 64 tickets carried rework and the project used no
    sprints at all, so both sprint charts rendered a bare "no data". A reader
    cannot distinguish that from "no rework", which is the opposite conclusion,
    and it is exactly what the coverage mechanism exists to prevent."""
    con = _derived("reopened", "two_sprints")
    # issue_sprints is a filtered view now; the base table is what a test can empty.
    con.execute("DELETE FROM issue_sprints_all")
    for key in ("rework_per_sprint", "carry_over"):
        chart = next(c for c in chart_specs.CHARTS if c.key == key)
        out = urd.run_chart(con, chart)
        assert "not shown" in out, f"{key} drew an empty chart instead of explaining"
        assert "no data" not in out, f"{key} still fell through to the empty-data note"


def test_carry_over_counts_tickets_not_memberships():
    con = _derived("reopened", "two_sprints")
    _, rows = _flow_rows(con, "carry_over")
    # The fixtures name sprints so that array order disagrees with both id and name
    # order (id 7 "Sprint B" first, then id 3 "Sprint A"), so a query that leans on
    # either instead of `ordinal` gets a different answer here.
    assert {r["sprint"]: r["carried"] for r in rows} == {"Sprint A": 1}


def test_carried_sprints_names_the_tickets_carry_over_only_counts():
    """PROJ-3 is the fixtures' carried ticket: In Progress, in Sprint B and then
    Sprint A. carry_over says one ticket was carried into Sprint A; this says
    which one, and since when."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, rows = _flow_rows(con, "carried_sprints")
    assert [(r["key"], r["sprints"]) for r in rows] == [("PROJ-3", 2)]
    # min(start) across its memberships, not the sprint it sits in now: the point
    # of the column is how long this has been rolling.
    assert str(rows[0]["since"]) == "2026-01-05"
    assert rows[0]["status"] == "In Progress"


def test_carried_sprints_leaves_out_a_ticket_that_only_ever_had_one_sprint():
    """Without the floor this is the open-ticket list, not a carry list.

    PROJ-3 is the fixtures' only OPEN ticket, so it is the only one the floor can
    be tested on: PROJ-1 and PROJ-2 are both Done and the status filter excludes
    them whatever their sprint count. Cutting PROJ-3 back to one sprint is what
    makes a lowered floor show up here."""
    con = _derived("reopened", "two_sprints")
    con.execute("DELETE FROM issue_sprints_all WHERE key = 'PROJ-3' "
                "AND sprint_name = 'Sprint A'")
    assert con.execute("SELECT count(*) FROM issue_sprints WHERE key = 'PROJ-3'"
                       ).fetchone()[0] == 1, "the cut did not land"
    assert _flow_rows(con, "carried_sprints")[1] == []


def test_carried_sprints_puts_the_worst_carried_ticket_first():
    """"Worst first" is the whole premise: a retro reads the top of this list and
    stops. One fixture ticket carries a sprint count, so a second open one is
    built here rather than left untested, which is what an ordering test needs to
    be able to fail at all."""
    con = _derived("reopened", "two_sprints")
    con.execute("INSERT INTO issues_all (key, summary, status, status_category, "
                "abandoned) VALUES ('PROJ-9', 'Rolling for months', 'Backlog', "
                "'new', FALSE)")
    # Dated AFTER PROJ-3's first sprint (2026-01-05) on purpose, so sprint count
    # and first-committed date disagree: ordering by the date alone answers
    # PROJ-3 first, which is the mistake this test exists to catch.
    for i, (sid, name, start) in enumerate((
            (11, "Sprint C", "2026-02-01"), (12, "Sprint D", "2026-02-15"),
            (13, "Sprint E", "2026-03-01")), start=1):
        con.execute("INSERT INTO issue_sprints_all VALUES ('PROJ-9', ?, ?, 'closed', "
                    "?::TIMESTAMP, ?::TIMESTAMP + INTERVAL 14 DAY, ?)",
                    [sid, name, start, start, i])
    rows = _flow_rows(con, "carried_sprints")[1]
    assert [(r["key"], r["sprints"]) for r in rows] == [("PROJ-9", 3), ("PROJ-3", 2)]


def test_carried_sprints_drops_a_ticket_once_it_is_done():
    """Delivered-after-five-sprints is history, and this list exists to be acted
    on. PROJ-3 is the only carried ticket, so closing it is the one thing that
    can empty the chart."""
    con = _derived("reopened", "two_sprints")
    assert [r["key"] for r in _flow_rows(con, "carried_sprints")[1]] == ["PROJ-3"]
    con.execute("UPDATE issues_all SET status_category = 'done' WHERE key = 'PROJ-3'")
    assert _flow_rows(con, "carried_sprints")[1] == []


def test_a_membership_with_no_window_is_not_counted_as_a_sprint():
    """This instance reports "Refined Backlog" through the Sprint field with no
    start or end. Counting it adds a phantom sprint to every ticket parked there,
    which is precisely the population this chart ranks.

    Asserted on PROJ-3's count rather than on some other ticket's absence: it is
    the only open ticket, so absence would prove the status filter works and
    nothing about this guard."""
    con = _derived("reopened", "two_sprints")
    con.execute(
        "INSERT INTO issue_sprints_all VALUES ('PROJ-3', 99, 'Refined Backlog', "
        "'active', NULL, NULL, 3)")
    rows = {r["key"]: r["sprints"] for r in _flow_rows(con, "carried_sprints")[1]}
    assert rows == {"PROJ-3": 2}, "a board column was counted as a sprint"


def test_points_per_sprint_credits_the_sprint_that_was_running_at_close():
    """PROJ-1 carries 5 points, belongs to Sprint B, and closes 2026-01-20, after
    Sprint B ended and inside Sprint A. Crediting the ticket's own sprint answers
    Sprint B, so the two models disagree here."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, rows = _flow_rows(con, "points_per_sprint")
    assert [(r["sprint"], r["points"]) for r in rows] == [("Sprint A", 5.0)]


def test_points_per_sprint_ignores_an_open_ticket_with_an_estimate():
    """PROJ-3 carries 3 points and has never closed. Summing estimates rather
    than closures would report work that has not been delivered."""
    con = _derived("reopened", "two_sprints")
    _, rows = _flow_rows(con, "points_per_sprint")
    assert sum(r["points"] for r in rows) == 5.0, rows


def test_cycle_per_sprint_keeps_tickets_that_closed_after_their_sprint_ended():
    """Work routinely closes after the sprint that carried it: PROJ-1 resolves
    2026-01-20 and its only sprint ended 2026-01-19. Attributing by resolution
    date inside the sprint window drops it, and the chart draws nothing."""
    con = _derived("reopened", "two_sprints")
    _, rows = _flow_rows(con, "cycle_per_sprint")
    assert [r["sprint"] for r in rows] == ["Sprint B"]
    assert rows[0]["median_days"] > 0


def test_derive_migrates_a_database_whose_issues_is_still_a_table():
    """Every database built before the scope views has issues, changes and
    issue_sprints as base tables, and CREATE OR REPLACE VIEW refuses to replace a
    table. Without this, derive fails outright on any existing database, which is
    every database anyone actually has."""
    con = urd.open_db(_tmpdb())
    load_fixtures(con, "reopened", "two_sprints")
    scope = urd.load_scope(con)
    urd.derive(con, scope["status_order"], scope["start_status"], scope["review_status"])
    # Put it back the way an older version left it, then derive again.
    con.execute("DROP VIEW issues")
    con.execute("CREATE TABLE issues AS SELECT * FROM issues_all")
    urd.derive(con, scope["status_order"], scope["start_status"], scope["review_status"])
    kind = con.execute("SELECT table_type FROM information_schema.tables "
                       "WHERE table_name = 'issues'").fetchone()[0]
    assert kind == "VIEW", kind
    assert con.execute("SELECT count(*) FROM issues").fetchone()[0] > 0


def test_excluding_an_epic_removes_it_and_its_children():
    """Filtered at the base tables rather than in each chart, so a chart added
    later inherits it. PROJ-1 and PROJ-3 both hang off PROJ-100 in the fixtures."""
    con = _derived("reopened", "two_sprints")
    before = con.execute("SELECT count(*) FROM issues").fetchone()[0]
    assert before >= 2
    urd.set_excluded_epics(con, ["PROJ-100"])
    assert con.execute("SELECT count(*) FROM issues").fetchone()[0] == 0
    urd.set_excluded_epics(con, [])
    assert con.execute("SELECT count(*) FROM issues").fetchone()[0] == before


def test_excluding_an_epic_also_removes_its_history():
    """Filtering issues alone is not enough: closures come from changes, so a
    dropped ticket would keep contributing to every event-based chart while being
    absent from every ticket-based one."""
    con = _derived("reopened", "two_sprints")
    assert con.execute("SELECT count(*) FROM changes").fetchone()[0] > 0
    urd.set_excluded_epics(con, ["PROJ-100"])
    for view in ("changes", "transitions", "closures", "status_durations",
                 "cycle_times", "rework", "issue_sprints", "mutation_sprint"):
        left = con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        assert left == 0, f"{view} still holds {left} rows for an excluded epic"
    urd.set_excluded_epics(con, [])


def test_an_excluded_epic_is_remembered_and_reported():
    con = _derived("reopened", "two_sprints")
    urd.set_excluded_epics(con, ["PROJ-100", "PROJ-200"])
    assert urd.load_scope(con)["excluded_epics"] == "PROJ-100,PROJ-200"
    assert urd.stored_excluded_epics(con) == ["PROJ-100", "PROJ-200"]
    urd.set_excluded_epics(con, [])
    assert urd.stored_excluded_epics(con) == []


def test_the_page_names_the_epics_it_left_out():
    """A report with a trash epic removed and one without look identical, and they
    say different things about every total."""
    html = render.page(_header(excluded=["PROJ-100"]), [])
    assert "PROJ-100" in html
    assert "excluded" in html.lower()
    assert "excluded" not in render.page(_header(), []).lower()


def test_every_chart_respects_the_report_window():
    """Stated as a rule over the specs rather than chart by chart, so a chart
    added later has to decide. A chart that silently ignored the window would
    report a different period from the one beside it, which is worse than a chart
    that is simply wrong: the reader has no way to tell."""
    for chart in chart_specs.CHARTS:
        exempt = chart.key in chart_specs.WINDOW_EXEMPT
        used = "in_window(" in chart.sql
        assert used != exempt, (
            f"{chart.key}: exempt={exempt} but "
            f"{'uses' if used else 'ignores'} the window")
        if chart.coverage and not exempt:
            assert "in_window(" in chart.coverage, f"{chart.key} coverage ignores --since"
    stray = set(chart_specs.WINDOW_EXEMPT) - {c.key for c in chart_specs.CHARTS}
    assert not stray, f"exemption for a chart that no longer exists: {stray}"


def test_the_exempt_chart_really_does_ignore_the_window():
    """The exemption is the point of the flag for this chart, so it is measured
    rather than trusted: the oldest open tickets must survive a window that
    postdates them."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    _, before = _flow_rows(con, "aging_wip")
    urd.set_report_window(con, "2030-01-01")
    _, after = _flow_rows(con, "aging_wip")
    assert [r["key"] for r in after] == [r["key"] for r in before], "the window bit"
    assert before, "fixture has no open tickets, so this proves nothing"


def test_a_window_narrows_every_chart_that_has_data_outside_it():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    before = {c.key: len(con.execute(c.sql).fetchall()) for c in chart_specs.CHARTS}
    urd.set_report_window(con, "2026-02-01")
    after = {c.key: len(con.execute(c.sql).fetchall()) for c in chart_specs.CHARTS}
    for key in before:
        assert after[key] <= before[key], f"{key} grew: {before[key]} -> {after[key]}"
    narrowed = [k for k in before if after[k] < before[k]]
    assert len(narrowed) >= 5, f"a February window barely changed anything: {narrowed}"
    for key in chart_specs.WINDOW_EXEMPT:
        assert after[key] == before[key], f"{key} is exempt but narrowed"


def test_an_unset_window_leaves_every_chart_whole():
    """The default has to be every row, or a first report silently covers nothing."""
    con = _derived("reopened", "skipped_progress", "two_sprints")
    whole = {c.key: len(con.execute(c.sql).fetchall()) for c in chart_specs.CHARTS}
    urd.set_report_window(con, None)
    assert {c.key: len(con.execute(c.sql).fetchall()) for c in chart_specs.CHARTS} == whole


def test_the_page_states_the_window_every_chart_obeys():
    """A windowed report and a whole-history one look identical otherwise, and
    they say different things. It matters most where the window changes a chart's
    meaning rather than just its length."""
    windowed = render.page(_header(window="2026-03-01"), [])
    assert "2026-03-01 onward" in windowed
    assert "onward" not in render.page(_header(), [])
    # "Every chart" stopped being true the moment one was exempted, and a header
    # that overclaims is worse than one that says nothing: a reader quotes the
    # aging table as if it covered the window.
    named = render.page(_header(window="2026-03-01", exempt=["Aging work in progress"]), [])
    assert "Every chart" not in named, named[named.index("<header>"):][:400]
    assert "Aging work in progress" in named


def test_a_report_window_is_remembered_and_validated():
    con = _derived("reopened", "two_sprints")
    urd.set_report_window(con, "2026-02-01")
    assert urd.load_scope(con)["report_since"] == "2026-02-01"
    for bad in ("yesterday", "2026-13-01", "01-02-2026"):
        try:
            urd.set_report_window(con, bad)
        except SystemExit:
            continue
        raise AssertionError(f"{bad!r} was accepted as a date")


def test_a_coverage_query_counts_the_rows_its_chart_actually_plots():
    """A coverage query that measures anything else lets an empty chart through
    its own gate: cycle_per_sprint reported 2 of 2 while plotting zero rows."""
    con = _derived("reopened", "two_sprints")
    for chart in chart_specs.CHARTS:
        if not chart.coverage:
            continue
        numerator, denominator = con.execute(chart.coverage).fetchone()
        plotted = len(con.execute(chart.sql).fetchall())
        assert numerator <= denominator, f"{chart.key}: {numerator} of {denominator}"
        if numerator:
            assert plotted, f"{chart.key}: coverage {numerator}/{denominator} but no rows"


def test_points_charts_are_held_to_the_lower_threshold():
    chart, _ = _flow_rows(_derived("reopened", "two_sprints"), "points_vs_cycle")
    assert chart.tier == "points"
    assert chart_specs.THRESHOLDS["points"] < chart_specs.THRESHOLDS["default"]
    assert chart.coverage is not None


def test_the_chart_list_matches_the_section_index():
    """A count was the original check and went stale the first time a chart was
    added. The property worth holding is that every chart lands in a declared
    section and no two share a key, which stays true however many there are."""
    keys = [c.key for c in chart_specs.CHARTS]
    assert len(keys) == len(set(keys)), "duplicate chart key"
    assert len(keys) >= 15
    for chart in chart_specs.CHARTS:
        assert chart.section in chart_specs.SECTIONS, f"{chart.key}: {chart.section}"



def test_no_chart_measures_an_individual():
    """The People section was deleted deliberately. Per-person throughput, points,
    review load and handoffs all read as a scoreboard, whatever the caption says,
    and the tool is meant to be shared with the people it would have ranked.

    Asserted rather than remembered because the original specs still sit in
    docs/superpowers/plans/2026-08-13-urd.md and a paste from there would put
    them back silently. `people` is the only table holding a human identity, so
    joining it is the thing to catch. Run against the specs as they were before
    the deletion this flags exactly the five that went."""
    for chart in chart_specs.CHARTS:
        # aging_wip is the one chart allowed to name a person: it names the
        # assignee of a single open ticket, which is who to ask about it, not a
        # comparison between people.
        if chart.key == "aging_wip":
            continue
        assert not re.search(r"\b(?:FROM|JOIN)\s+people\b", chart.sql, re.I), chart.key


def test_the_whole_report_renders_end_to_end():
    con = _derived("reopened", "skipped_progress", "two_sprints")
    out = os.path.join(tempfile.mkdtemp(), "report.html")
    urd.report(con, out)
    html = pathlib.Path(out).read_text()
    for chart in chart_specs.CHARTS:
        # A chart below its coverage threshold still names itself, in the strip.
        assert chart.title in html, f"{chart.key} missing from the page"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
