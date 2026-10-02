"""Delivery counts separate confirmed subtasks without guessing from ancestry."""
import charts
import test_helpers  # noqa: F401 - refuses network access
import urd
from test_team_report import _change, _db, _ticket


def _rows(con, key):
    cursor = con.execute(next(c.sql for c in charts.CHARTS if c.key == key))
    return [dict(zip([d[0] for d in cursor.description], row, strict=True))
            for row in cursor.fetchall()]


def test_parent_and_subtasks_are_separate_delivery_populations():
    con = _db()
    for key in ('PARENT', 'CHILD-1', 'CHILD-2', 'CHILD-3', 'UNKNOWN'):
        _ticket(con, key)
        _change(con, key, '2026-01-10', 'status', 'To Do', 'Done')
        con.execute("UPDATE issues_all SET is_subtask = ?, parent = ?, "
                    "fix_versions = ['Release'], story_points = 2, "
                    "status = 'Done', status_category = 'done', resolved = '2026-01-10' "
                    "WHERE key = ?",
                    [None if key == 'UNKNOWN' else key != 'PARENT',
                     'EPIC' if key == 'PARENT' else 'PARENT', key])
    row = _rows(con, 'sprint_scope_changes')[0]
    assert row['original_delivered'] == 1, row
    assert row['original_subtasks_completed'] == 3
    assert row['classification_unknown'] == 1
    for key in ('created_vs_closed', 'flow_per_sprint', 'per_fix_version'):
        rows = _rows(con, key)
        assert sum(r['delivered'] for r in rows) == 1, (key, rows)
        assert sum(r['subtasks_completed'] for r in rows) == 3, (key, rows)
    assert [r['epic'] for r in _rows(con, 'per_epic')] == ['EPIC']
    assert _rows(con, 'per_epic')[0]['percent_done'] == 100
    assert _rows(con, 'points_committed_vs_closed')[0]['closed'] == 2
    assert _rows(con, 'points_committed_vs_closed')[0]['committed'] == 2
    assert _rows(con, 'subtasks_by_parent')[0]['subtasks_completed'] == 3
    assert _rows(con, 'issue_classification')[0]['unknown'] == 1
    trend = _rows(con, 'flow_trend')[-1]
    assert trend['done_trend'] == 0.3, trend
    assert trend['subtasks_completed_trend'] == 0.8, trend
    assert max(r['tickets'] for r in _rows(con, 'cfd')) == 1
    con.execute("UPDATE changes_all SET to_str = 'In Progress' WHERE field = 'status'")
    assert max(r['open_tickets'] for r in _rows(con, 'net_open')) == 1
    con.execute("UPDATE changes_all SET to_str = 'Done' WHERE field = 'status'")
    # Losing the parent from scope must never promote its subtasks to delivery.
    con.execute("DELETE FROM issues_all WHERE key = 'PARENT'")
    assert sum(r['delivered'] for r in _rows(con, 'created_vs_closed')) == 0
    assert sum(r['subtasks_completed'] for r in _rows(con, 'created_vs_closed')) == 3
    con.close()


def test_subtask_outcomes_and_coverage_do_not_inflate_delivery():
    con = _db()
    for key, subtask, joined in (
        ('MAIN', False, '2026-01-01'), ('REOPENED', True, '2026-01-01'),
        ('DROPPED', True, '2026-01-01'), ('ADDED', True, '2026-01-06'),
        ('UNCERTAIN', False, None), ('UNKNOWN', None, '2026-01-01'),
    ):
        _ticket(con, key, joined=joined)
        con.execute('UPDATE issues_all SET is_subtask = ?, story_points = ? WHERE key = ?',
                    [subtask, 2 if key == 'MAIN' else None, key])
        _change(con, key, '2026-01-10', 'status', 'To Do',
                'Dropped' if key == 'DROPPED' else 'Done')
    _change(con, 'REOPENED', '2026-01-12', 'status', 'Done', 'In Progress', 3)
    _change(con, 'REOPENED', '2026-01-13', 'status', 'In Progress', 'Done', 4)
    _change(con, 'UNCERTAIN', '2026-01-12', 'Sprint', '7', '', 3)
    row = _rows(con, 'sprint_scope_changes')[0]
    assert (row['original_delivered'], row['original_subtasks_completed'],
            row['added_subtasks_completed'], row['unknown'],
            row['classification_unknown']) == (1, 1, 1, 1, 1)
    assert row['original_dropped'] == row['added'] == row['added_delivered'] == 0
    weekly = _rows(con, 'created_vs_closed')
    assert sum(r['delivered'] for r in weekly) == 2
    assert sum(r['subtasks_completed'] for r in weekly) == 3
    assert sum(r['dropped'] for r in weekly) == 0
    points = next(c for c in charts.CHARTS if c.key == 'points_committed_vs_closed')
    assert con.execute(points.coverage).fetchone() == (1, 2)
    flow = next(c for c in charts.CHARTS if c.key == 'flow_per_sprint')
    # Coverage includes both displayed known classifications, excluding UNKNOWN.
    assert con.execute(flow.coverage).fetchone() == (9, 17)
    con.close()


def test_subtask_only_scope_renders_sprint_completions():
    con = _db()
    _ticket(con, 'CHILD', joined=None, created='2026-01-06')
    con.execute('UPDATE issues_all SET is_subtask = TRUE')
    _change(con, 'CHILD', '2026-01-10', 'status', 'To Do', 'Done')
    flow = next(c for c in charts.CHARTS if c.key == 'flow_per_sprint')
    assert '<svg' in urd.run_chart(con, flow)
    assert con.execute(flow.coverage).fetchone() == (2, 2)
    _ticket(con, 'UNCERTAIN-CHILD', joined=None)
    con.execute("UPDATE issues_all SET is_subtask = TRUE WHERE key = 'UNCERTAIN-CHILD'")
    _change(con, 'UNCERTAIN-CHILD', '2026-01-10', 'Sprint', '7', '')
    assert _rows(con, 'sprint_scope_changes')[0].get('subtasks_unknown') == 1
    con.close()


if __name__ == '__main__':
    test_subtask_only_scope_renders_sprint_completions()
    test_parent_and_subtasks_are_separate_delivery_populations()
    test_subtask_outcomes_and_coverage_do_not_inflate_delivery()
    print('subtask report checks passed')
