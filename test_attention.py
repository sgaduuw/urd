"""Explicit parking semantics, using synthetic tickets only."""
import json
import tempfile
from pathlib import Path

import charts
import test_helpers  # noqa: F401
import urd
from test_subtask_report import _rows
from test_team_report import _db, _ticket


def test_attention_groups_keep_active_work_visible():
    con = _db()
    urd.save_scope(con, status_order='To Do, In Progress, Blocked, Deferred, Done',
                   parked_status='Deferred')
    for n in range(45):
        _ticket(con, f'PARK-{n:02}')
    for key in ('ACTIVE', 'BLOCKED', 'UNKNOWN', 'DONE'):
        _ticket(con, key)
    con.execute("UPDATE issues_all SET status = 'Deferred'")
    for key, status in [('ACTIVE', 'In Progress'), ('BLOCKED', 'Blocked'),
                        ('UNKNOWN', 'Unfamiliar'), ('DONE', 'Done')]:
        con.execute('UPDATE issues_all SET status = ? WHERE key = ?', [status, key])
    con.execute("UPDATE issues_all SET status_category = 'done' WHERE key = 'DONE'")
    con.execute('INSERT INTO issue_sprints_all SELECT key, sprint_id + 1, sprint_name, '
                'state, start, "end", ordinal + 1, completed_at, fetched_at FROM issue_sprints_all')
    assert _rows(con, 'attention_counts') == [{'active': 2, 'parked': 45, 'unclassified': 1}]
    for key in ('aging_wip', 'carried_sprints'):
        assert {r['key'] for r in _rows(con, key)} == {'ACTIVE', 'BLOCKED'}
        assert len(_rows(con, key + '_parked')) == 40
        assert [r['key'] for r in _rows(con, key + '_unclassified')] == ['UNKNOWN']
        assert key + '_parked' in charts.WINDOW_EXEMPT
    urd.set_report_window(con, '2099-01-01')
    assert _rows(con, 'attention_counts')[0]['parked'] == 45
    urd.set_excluded_epics(con, ['ACTIVE'])
    assert _rows(con, 'attention_counts')[0]['active'] == 1
    urd.set_report_components(con, ['OTHER'])
    assert _rows(con, 'attention_counts') == [{'active': 0, 'parked': 0, 'unclassified': 0}]
    con.close()


def test_parking_configuration_is_nullable_clearable_and_validated():
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / 'synthetic.duckdb')
        con = urd.open_db(path)
        assert urd.load_scope(con)['parked_status'] is None
        urd.derive(con, 'To Do,Deferred,Done', 'To Do', None, parked_status='Deferred')
        assert urd.load_scope(con)['parked_status'] == 'Deferred'
        con.close()
        urd.main(['--db', path, 'derive', '--parked-status', ''])
        con = urd.open_db(path)
        assert urd.load_scope(con)['parked_status'] == ''
        for invalid in ('Typo', 'Deferred,,To Do', 'Deferred,Deferred'):
            try:
                urd.derive(con, 'To Do,Deferred,Done', 'To Do', None, parked_status=invalid)
            except SystemExit:
                pass
            else:
                raise AssertionError(invalid)
        assert urd.load_scope(con)['parked_status'] == ''
        con.execute("INSERT INTO raw_issues VALUES ('SYN-DONE', '2026-01-01', now(), ?)",
                    [json.dumps({'key': 'SYN-DONE', 'fields': {
                        'status': {'name': 'Done', 'statusCategory': {'key': 'done'}}}})])
        try:
            urd.derive(con, 'To Do,Deferred,Done', 'To Do', None, parked_status='Done')
        except SystemExit:
            pass
        else:
            raise AssertionError('done status accepted as parked')
        urd.derive(con, 'To Do,Deferred,Done', 'To Do', None)
        assert urd.load_scope(con)['parked_status'] == ''
        con.close()
    con = _db()
    _ticket(con, 'ACTIVE')
    assert _rows(con, 'attention_counts')[0]['unclassified'] == 1
    urd.save_scope(con, parked_status='')
    assert _rows(con, 'attention_counts')[0]['active'] == 1
    con.execute("UPDATE issues_all SET status_category = NULL")
    assert _rows(con, 'attention_counts')[0]['unclassified'] == 1
    con.execute("UPDATE issues_all SET status_category = 'new', status = NULL")
    assert _rows(con, 'attention_counts')[0]['unclassified'] == 1
    con.close()


def test_parking_validation_ignores_other_workflows():
    con = urd.open_db(':memory:')
    con.execute("INSERT INTO statuses VALUES ('Hold', 'done')")
    con.execute("INSERT INTO raw_issues VALUES ('SYN-1', '2026-01-01', now(), ?)",
                [json.dumps({'key': 'SYN-1', 'fields': {
                    'status': {'name': 'Hold', 'statusCategory': {'key': 'new'}}}})])
    urd.derive(con, 'Hold,Done', 'Hold', None, parked_status='Hold')
    assert urd.load_scope(con)['parked_status'] == 'Hold'
    con.execute("INSERT INTO raw_issues VALUES ('SYN-2', '2026-01-01', now(), ?)",
                [json.dumps({'key': 'SYN-2', 'fields': {
                    'status': {'name': 'Hold', 'statusCategory': {'key': 'done'}}}})])
    try:
        urd.derive(con, 'Hold,Done', 'Hold', None, parked_status='Hold')
    except SystemExit:
        pass
    else:
        raise AssertionError('ambiguous open/done parking accepted')
    con.close()


if __name__ == '__main__':
    test_parking_validation_ignores_other_workflows()
    test_attention_groups_keep_active_work_visible()
    test_parking_configuration_is_nullable_clearable_and_validated()
    print('attention checks passed')
