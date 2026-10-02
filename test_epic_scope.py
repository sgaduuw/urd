"""Epic scope and completion events from synthetic parent histories only."""
import json
import os
import tempfile
from datetime import datetime

import charts
import test_helpers  # noqa: F401 - refuses network access
import urd

CUTOFF = '2026-02-01T12:00:00.500000Z'


def _parent(key='SYN-100', identity='100', level=1):
    return {'key': key, 'id': identity,
            'fields': {'issuetype': {'subtask': False, 'hierarchyLevel': level}}}


def _change(day, identity, field, before=None, after=None, before_key=None, after_key=None):
    return {'id': str(identity), 'created': day, 'items': [{
        'field': field, 'from': before, 'to': after,
        'fromString': before_key, 'toString': after_key,
    }]}


def _move(day, identity, before=None, after='100', field='IssueParentAssociation'):
    return _change(day, identity, field, before, after,
                   'SYN-' + before if before else None, 'SYN-' + after if after else None)


def _done(day, identity, status='Done'):
    return _change(day, identity, 'status', '1', '2', 'In Progress', status)


def _issue(con, key='SYN-1', *, parent=None, changes=(), subtask=False, component='TEAM'):
    raw = {'key': key, 'id': key.split('-')[-1], 'fields': {
        'summary': 'Synthetic work', 'created': '2026-01-01T00:00:00Z',
        'updated': '2026-01-31T00:00:00Z', 'parent': parent,
        'components': [{'name': component}],
        'issuetype': {'subtask': subtask, 'hierarchyLevel': -1 if subtask else 0},
        'status': {'name': 'In Progress', 'statusCategory': {'key': 'indeterminate'}},
    }, 'changelog': {'histories': list(changes), 'total': len(changes)}}
    con.execute('INSERT INTO raw_issues VALUES (?, ?, ?, ?)',
                [key, raw['fields']['updated'], datetime(2026, 2, 1, 11), json.dumps(raw)])


def _db():
    con = urd.open_db(':memory:')
    con.executemany('INSERT INTO statuses VALUES (?, ?)', [
        ('To Do', 'new'), ('In Progress', 'indeterminate'),
        ('Done', 'done'), ('Dropped', 'done'),
    ])
    urd.save_scope(con, last_sync_at=CUTOFF, site='example.invalid')
    return con


def _derive(con):
    urd.derive(con, 'To Do,In Progress,Done,Dropped', 'In Progress', 'Done', 'Dropped')


def _rows(con, key='epic_scope_by_week'):
    chart = next((c for c in charts.CHARTS if c.key == key), None)
    assert chart is not None, f'missing chart: {key}'
    result = con.execute(chart.sql)
    return [dict(zip([d[0] for d in result.description], row, strict=True))
            for row in result.fetchall()]


def _totals(con):
    result = {}
    for row in _rows(con):
        counts = result.setdefault(row['epic'], [0, 0, 0, 0])
        for i, name in enumerate(('added', 'removed', 'delivered', 'dropped')):
            counts[i] += row[name]
    return result


def test_reparenting_moves_scope_and_attributes_completion_at_event_time():
    con = _db()
    try:
        _issue(con, parent=_parent('SYN-200', '200'), changes=[
            _move('2026-01-02', 1), _done('2026-01-03', 2),
            _move('2026-01-09', 3, '100', '200'),
            _done('2026-01-10', 4, 'Dropped'), _done('2026-01-11', 5),
        ])
        # Nested metadata supplies type evidence even when the epic is not mirrored itself.
        _issue(con, 'SYN-2', parent=_parent(), subtask=True)
        _derive(con)
        assert _totals(con) == {'SYN-100': [1, 1, 1, 0], 'SYN-200': [1, 0, 1, 1]}
        trend = _rows(con, 'epic_scope_trend')
        assert sum(r['delivered'] for r in trend) == 2
        assert sum(r['added'] - r['removed'] for r in trend) == 1
    finally:
        con.close()


def test_aliases_and_numeric_parent_ids_do_not_duplicate_scope():
    con = _db()
    try:
        change = _move('2026-01-02', 1)
        change['items'].append({**change['items'][0], 'field': 'Epic Link'})
        change['items'][0]['toString'] = '100'
        _issue(con, parent=_parent(), changes=[change, _done('2026-01-03', 2)])
        _issue(con, 'SYN-2', parent=_parent(), subtask=True,
               changes=[_move('2026-01-02', 1), _done('2026-01-03', 2)])
        _issue(con, 'SYN-3', parent=_parent(), subtask=None,
               changes=[_move('2026-01-02', 1), _done('2026-01-03', 2)])
        _derive(con)
        assert _totals(con) == {'SYN-100': [1, 0, 1, 0]}
    finally:
        con.close()


def test_current_parent_does_not_invent_history_and_initiatives_are_not_epics():
    con = _db()
    try:
        _issue(con, parent=_parent(), changes=[_done('2026-01-03', 2)])
        _issue(con, 'SYN-2', parent=_parent('SYN-200', '200', 2),
               changes=[_move('2026-01-02', 1, after='200'), _done('2026-01-03', 2)])
        _issue(con, 'SYN-3', parent=_parent(), changes=[
            _done('2026-01-01', 1), _move('2026-01-02', 2), _done('2026-01-03', 3),
        ])
        _derive(con)
        assert _totals(con) == {'SYN-100': [1, 0, 1, 0]}
        coverage = _rows(con, 'epic_history_coverage')
        assert any(r['reason'] == 'No recorded parent changes' and r['count'] == 1
                   for r in coverage), coverage
        assert any(r['reason'] == 'Parent unknown at completion' and r['count'] == 2
                   for r in coverage), coverage
    finally:
        con.close()


def test_history_order_and_same_edit_use_the_resulting_parent():
    con = _db()
    try:
        simultaneous = _move('2026-01-09', 3, '100', '200')
        simultaneous['items'] += _done('2026-01-09', 3)['items']
        _issue(con, parent=_parent('SYN-200', '200'), changes=[
            _move('2026-01-02', 1), _done('2026-01-09', 2), simultaneous,
            _done('2026-01-09', 4),
        ])
        _issue(con, 'SYN-2', parent=_parent(), subtask=True)
        _derive(con)
        assert _totals(con) == {'SYN-100': [1, 1, 1, 0], 'SYN-200': [1, 0, 2, 0]}
    finally:
        con.close()


def test_removal_return_and_known_no_parent_are_separate_from_unknown():
    con = _db()
    try:
        _issue(con, parent=_parent(), changes=[
            _move('2026-01-02', 1), _move('2026-01-04', 2, '100', None),
            _done('2026-01-05', 3), _move('2026-01-06', 4), _done('2026-01-07', 5),
        ])
        _derive(con)
        assert _totals(con) == {'SYN-100': [2, 1, 1, 0]}
        assert any(r['reason'] == 'No epic at event' and r['count'] == 1
                   for r in _rows(con, 'epic_history_coverage'))
    finally:
        con.close()


def test_conflicting_aliases_and_broken_chains_do_not_assign_completions():
    con = _db()
    try:
        conflict = _move('2026-01-02', 1)
        conflict['items'] += _move('2026-01-02', 1, after='200', field='Epic Link')['items']
        _issue(con, parent=_parent(), changes=[conflict, _done('2026-01-03', 2)])
        _issue(con, 'SYN-2', parent=_parent(), changes=[
            _move('2026-01-02', 1), _done('2026-01-03', 2),
            _move('2026-01-04', 3, '200', '100'), _done('2026-01-05', 4),
        ])
        _issue(con, 'SYN-3', parent=_parent('SYN-200', '200'), subtask=True)
        _derive(con)
        assert _totals(con) == {'SYN-100': [2, 0, 1, 0], 'SYN-200': [0, 1, 0, 0]}
        coverage = {r['reason']: r['count'] for r in _rows(con, 'epic_history_coverage')}
        assert coverage['Conflicting parent changes'] == 3
        assert coverage['Broken parent history'] == 1
    finally:
        con.close()


def test_unknown_ids_and_parent_types_never_become_confirmed_epics():
    con = _db()
    try:
        unresolved = _move('2026-01-02', 1, after='999')
        unresolved['items'][0]['toString'] = '999'
        _issue(con, changes=[unresolved, _done('2026-01-03', 2)])
        _issue(con, 'SYN-2', parent=_parent('SYN-200', '200', None), changes=[
            _move('2026-01-02', 1, after='200'), _done('2026-01-03', 2),
        ])
        _derive(con)
        assert _totals(con) == {}
        reasons = {r['reason'] for r in _rows(con, 'epic_history_coverage')}
        assert {'Unresolved parent', 'Unknown parent type'} <= reasons
    finally:
        con.close()


def test_filters_apply_to_events_without_erasing_earlier_parent_evidence():
    con = _db()
    try:
        _issue(con, parent=_parent('SYN-200', '200'), changes=[
            _move('2026-01-02', 1), _done('2026-01-10', 2),
            _move('2026-01-11', 3, '100', '200'), _done('2026-01-12', 4),
        ])
        _issue(con, 'SYN-2', parent=_parent(), component='OTHER',
               changes=[_move('2026-01-02', 1), _done('2026-01-10', 2)])
        _derive(con)
        urd.set_report_components(con, ['TEAM'])
        urd.set_report_window(con, '2026-01-09')
        assert _totals(con) == {'SYN-100': [0, 1, 1, 0], 'SYN-200': [1, 0, 1, 0]}
        # An epic remains excluded even after the ticket has left it.
        urd.set_excluded_epics(con, ['SYN-100'])
        assert _totals(con) == {'SYN-200': [1, 0, 1, 0]}
        urd.set_excluded_epics(con, ['SYN-200'])
        assert _totals(con) == {}
    finally:
        con.close()


def test_undated_parent_changes_cannot_establish_completion_membership():
    con = _db()
    try:
        _issue(con, parent=_parent(), changes=[
            _move('2026-01-02', 1), _move(None, 2, '100', '200'),
            _done('2026-01-03', 3),
        ])
        _derive(con)
        assert _totals(con)['SYN-100'][2] == 0
        assert any(r['reason'] == 'Undated parent history'
                   for r in _rows(con, 'epic_history_coverage'))
    finally:
        con.close()


def test_coverage_windows_events_but_keeps_current_relationship_gaps():
    con = _db()
    try:
        _issue(con, parent=_parent(), changes=[_done('2026-01-03', 1)])
        _issue(con, 'SYN-2', parent=_parent(), changes=[
            _move('2026-01-02', 1), _done('2026-01-10', 2),
        ])
        _derive(con)
        urd.set_report_window(con, '2026-01-09')
        assert _rows(con, 'epic_history_coverage') == [
            {'reason': 'Epic', 'unit': 'events', 'count': 1},
            {'reason': 'No recorded parent changes', 'unit': 'tickets', 'count': 1},
        ]
    finally:
        con.close()


def test_missing_history_id_does_not_guess_the_order_of_simultaneous_edits():
    con = _db()
    try:
        change = _move('2026-01-02', 1)
        change['id'] = None
        _issue(con, parent=_parent(), changes=[change, _done('2026-01-02', 2)])
        _derive(con)
        assert _totals(con) == {'SYN-100': [1, 0, 0, 0]}
        assert any(r['reason'] == 'Ambiguous event order' and r['count'] == 1
                   for r in _rows(con, 'epic_history_coverage'))
    finally:
        con.close()


def test_ambiguous_parent_batch_stays_unknown_until_a_later_change():
    con = _db()
    try:
        unordered = _move('2026-01-02', 1, '100', '300')
        unordered['id'] = None
        _issue(con, parent=_parent(), changes=[
            unordered, _move('2026-01-02', 2, '100', '200'),
            _done('2026-01-03', 3), _move('2026-01-04', 4, '200', '100'),
            _done('2026-01-05', 5),
        ])
        _issue(con, 'SYN-8', parent=_parent('SYN-200', '200'), subtask=True)
        _issue(con, 'SYN-9', parent=_parent('SYN-300', '300'), subtask=True)
        _derive(con)
        assert _totals(con) == {
            'SYN-100': [1, 2, 1, 0], 'SYN-200': [1, 1, 0, 0], 'SYN-300': [1, 0, 0, 0],
        }
        assert any(r['reason'] == 'Ambiguous event order' and r['unit'] == 'events'
                   and r['count'] == 1 for r in _rows(con, 'epic_history_coverage'))
    finally:
        con.close()


def test_broken_parent_history_is_visible_without_any_completion():
    for adjacent_gap in (True, False):
        con = _db()
        try:
            changes = [_move('2026-01-02', 1)]
            if adjacent_gap:
                changes.append(_move('2026-01-03', 2, '200', '100'))
            _issue(con, parent=_parent() if adjacent_gap else _parent('SYN-200', '200'),
                   changes=changes)
            _issue(con, 'SYN-8', parent=_parent(), subtask=True)
            _issue(con, 'SYN-9', parent=_parent('SYN-200', '200'), subtask=True)
            _derive(con)
            assert _totals(con)['SYN-100'][0] == (2 if adjacent_gap else 1)
            assert any(r['reason'] == 'Broken parent history' and r['unit'] == 'tickets'
                       and r['count'] == 1 for r in _rows(con, 'epic_history_coverage'))
        finally:
            con.close()


def test_uncertain_next_parent_change_invalidates_the_preceding_interval():
    for reason in ('Ambiguous event order', 'Conflicting parent changes', 'Unresolved parent'):
        con = _db()
        try:
            first = _move('2026-01-02', 1)
            first['items'] += _done('2026-01-02', 1)['items']
            boundary = [_move('2026-01-04', 4, '200', '400')]
            if reason == 'Ambiguous event order':
                boundary[0] = _move('2026-01-04', 4, '200', '300')
                boundary[0]['id'] = None
                boundary.append(_move('2026-01-04', 5, '300', '400'))
            elif reason == 'Conflicting parent changes':
                boundary[0]['items'] += _move('2026-01-04', 4, '300', '400',
                                              field='Epic Link')['items']
            else:
                boundary[0]['items'][0].update({'from': '999', 'fromString': '999'})
            _issue(con, parent=_parent(), changes=[
                first, _done('2026-01-03', 2), _done('2026-01-03T12:00:00Z', 3, 'Dropped'),
                *boundary, _move('2026-01-05', 6, '400', '100'), _done('2026-01-06', 7),
            ])
            for i, identity in enumerate(('200', '300', '400'), 8):
                _issue(con, f'SYN-{i}', parent=_parent(f'SYN-{identity}', identity),
                       subtask=True)
            _derive(con)
            outcomes = con.execute("""
                SELECT parent, reason FROM epic_events_all
                WHERE key = 'SYN-1' AND kind IN ('delivered', 'dropped') ORDER BY ts
            """).fetchall()
            assert outcomes == [('SYN-100', 'Epic'), (None, reason), (None, reason),
                                ('SYN-100', 'Epic')], (reason, outcomes)
            assert _totals(con)['SYN-100'] == [2, 0, 2, 0]
            assert any(r['reason'] == reason and r['unit'] == 'tickets' and r['count'] == 1
                       for r in _rows(con, 'epic_history_coverage'))
        finally:
            con.close()


def test_snapshot_cutoff_and_legacy_report_require_derived_epic_history():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, 'legacy.duckdb')
        con = urd.open_db(path)
        con.executemany('INSERT INTO statuses VALUES (?, ?)',
                        [('Done', 'done'), ('Dropped', 'done')])
        urd.save_scope(con, last_sync_at=CUTOFF, site='example.invalid')
        _issue(con, parent=_parent(), changes=[
            _move('2026-01-02', 1), _done(CUTOFF, 2),
            _done('2026-02-01T12:00:00.500001Z', 3),
        ])
        _derive(con)
        assert _totals(con) == {'SYN-100': [1, 0, 1, 0]}
        chart = next(c for c in charts.CHARTS if c.key == 'epic_scope_by_week')
        html = urd.run_chart(con, chart)
        assert CUTOFF in html and 'https://example.invalid/browse/SYN-100' in html
        con.execute('DROP VIEW epic_events')
        con.execute('DROP TABLE epic_events_all')
        con.close()
        con = urd.open_db(path)
        try:
            assert 'Run derive or Refresh' in urd.run_chart(con, chart)
            assert con.execute('SELECT count(*) FROM raw_issues').fetchone()[0] == 1
            _derive(con)
            assert _totals(con) == {'SYN-100': [1, 0, 1, 0]}
            before = urd.run_chart(con, chart)
            urd.save_scope(con, sync_started_at='2026-02-02T00:00:00Z')
            con.execute('DELETE FROM raw_issues')
            assert urd.run_chart(con, chart) == before
            _derive(con)
            assert 'cutoff unavailable' in urd.run_chart(con, chart)
        finally:
            con.close()


if __name__ == '__main__':
    for name, fn in sorted(globals().copy().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok {name}')
    print('epic scope checks passed')
