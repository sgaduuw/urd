"""Active sprint scope is a dated mirror snapshot, using synthetic history only."""
import json
import os
import tempfile
from datetime import datetime

import charts
import urd
from test_team_report import _change
from test_team_report import _db as _team_db
from test_team_report import _ticket as _team_ticket

SNAPSHOT = '2026-01-12T12:00:00.500000Z'
SUMMARY = 'active_sprint_scope'
DETAIL = 'active_sprint_tickets'


def _derive(con):
    urd.derive(con, 'To Do,In Progress,Done,Dropped', 'In Progress', 'Done', 'Dropped')


def _db():
    con = _team_db()
    urd.save_scope(con, last_sync_at=SNAPSHOT, site='example.invalid')
    _derive(con)
    return con


def _ticket(con, key, **kwargs):
    _team_ticket(con, key, **kwargs)
    con.execute("UPDATE issue_sprints_all SET state = 'active', fetched_at = ? WHERE key = ?",
                [datetime(2026, 1, 12, 11), key])
    con.execute('UPDATE issues_all SET summary = ? WHERE key = ?', [key, key])


def _chart(key):
    chart = next((c for c in charts.CHARTS if c.key == key), None)
    assert chart is not None, f'missing active sprint view: {key}'
    return chart


def _rows(con, key=SUMMARY):
    cursor = con.execute(_chart(key).sql)
    return [dict(zip([d[0] for d in cursor.description], row, strict=True))
            for row in cursor.fetchall()]


def test_active_scope_tracks_original_added_removed_and_uncertain_tickets():
    con = _db()
    try:
        for key in ('ORIGINAL', 'REMOVED', 'RETURNED'):
            _ticket(con, key)
        _change(con, 'REMOVED', '2026-01-10', 'Sprint', '7', '')
        con.execute("DELETE FROM issue_sprints_all WHERE key = 'REMOVED'")
        _change(con, 'RETURNED', '2026-01-10', 'Sprint', '7', '')
        _change(con, 'RETURNED', '2026-01-11', 'Sprint', '', '7', 3)
        _ticket(con, 'ADDED', joined='2026-01-06')
        _ticket(con, 'ADDED-REMOVED', joined='2026-01-07')
        _change(con, 'ADDED-REMOVED', '2026-01-10', 'Sprint', '7', '')
        _ticket(con, 'CREATED', joined=None, created='2026-01-08')
        _change(con, 'CREATED', '2026-01-13', 'Sprint', '7', '7, 8')
        _ticket(con, 'UNKNOWN', joined=None)
        _change(con, 'UNKNOWN', '2026-01-10', 'Sprint', '7', '')
        _change(con, 'UNKNOWN', '2026-01-11', 'Sprint', '', '7', 3)
        _ticket(con, 'FUTURE', joined='2026-01-13')
        _ticket(con, 'REMOVED-BEFORE')
        _change(con, 'REMOVED-BEFORE', '2026-01-03', 'Sprint', '7', '')
        assert _rows(con) == [{
            'sprint': 'Sprint A (#7)', 'work': 'Main work',
            'original': 3, 'added': 3, 'removed': 2, 'unknown': 1,
        }]
        detail = {r['key']: (r['origin'], r['membership']) for r in _rows(con, DETAIL)}
        assert detail == {
            'ORIGINAL': ('Original', 'In sprint'), 'REMOVED': ('Original', 'Removed'),
            'RETURNED': ('Original', 'In sprint'), 'ADDED': ('Added', 'In sprint'),
            'ADDED-REMOVED': ('Added', 'Removed'), 'CREATED': ('Added', 'In sprint'),
            'UNKNOWN': ('Unknown', 'In sprint'),
        }
    finally:
        con.close()


def test_snapshot_includes_cutoff_events_but_excludes_later_events_and_creation():
    con = _db()
    try:
        _ticket(con, 'START', joined='2026-01-05')
        _ticket(con, 'JOIN', joined=SNAPSHOT)
        _ticket(con, 'LATER', joined='2026-01-12T12:00:00.500001Z')
        _ticket(con, 'CREATED', joined=None, created=SNAPSHOT)
        _ticket(con, 'UNBORN', joined=None, created='2026-01-12T12:00:00.500001Z')
        _ticket(con, 'REMOVED')
        _change(con, 'REMOVED', SNAPSHOT, 'Sprint', '7', '')
        _ticket(con, 'RETURNED')
        _change(con, 'RETURNED', '2026-01-10', 'Sprint', '7', '')
        _change(con, 'RETURNED', SNAPSHOT, 'Sprint', '', '7', 3)
        assert _rows(con) == [{
            'sprint': 'Sprint A (#7)', 'work': 'Main work',
            'original': 3, 'added': 2, 'removed': 1, 'unknown': 0,
        }]
        assert {r['key'] for r in _rows(con, DETAIL)} == {
            'START', 'JOIN', 'CREATED', 'REMOVED', 'RETURNED',
        }
    finally:
        con.close()


def test_parallel_sprints_and_issue_classifications_remain_separate():
    con = _db()
    try:
        for key in ('MAIN', 'SUBTASK', 'UNCLASSIFIED', 'PARALLEL'):
            _ticket(con, key)
        con.execute("UPDATE issues_all SET is_subtask = TRUE WHERE key = 'SUBTASK'")
        con.execute("UPDATE issues_all SET is_subtask = NULL WHERE key = 'UNCLASSIFIED'")
        con.execute("UPDATE issue_sprints_all SET sprint_id = 8 WHERE key = 'PARALLEL'")
        con.execute("UPDATE changes_all SET to_id = '8' WHERE key = 'PARALLEL'")
        # Duplicate embedded snapshots must not duplicate tickets or merge IDs.
        con.execute("INSERT INTO issue_sprints_all SELECT * FROM issue_sprints_all "
                    "WHERE key = 'MAIN'")
        assert {(r['sprint'], r['work'], r['original']) for r in _rows(con)} == {
            ('Sprint A (#7)', 'Main work', 1), ('Sprint A (#7)', 'Subtasks', 1),
            ('Sprint A (#7)', 'Unknown type', 1), ('Sprint A (#8)', 'Main work', 1),
        }
        assert len(_rows(con, DETAIL)) == 4
    finally:
        con.close()


def test_scope_filters_preserve_metadata_and_current_snapshot_ignores_since():
    con = _db()
    try:
        _ticket(con, 'REMOVED')
        _change(con, 'REMOVED', '2026-01-10', 'Sprint', '7', '')
        con.execute("DELETE FROM issue_sprints_all WHERE key = 'REMOVED'")
        _ticket(con, 'OTHER')
        con.execute("UPDATE issues_all SET components = ['OTHER'], parent = 'OTHER-EPIC' "
                    "WHERE key = 'OTHER'")
        assert _rows(con)[0]['original'] == 2
        urd.set_report_components(con, ['TEAM'])
        expected = [{'sprint': 'Sprint A (#7)', 'work': 'Main work',
                     'original': 1, 'added': 0, 'removed': 1, 'unknown': 0}]
        assert _rows(con) == expected
        assert [r['key'] for r in _rows(con, DETAIL)] == ['REMOVED']
        urd.set_report_window(con, '2030-01-01')
        assert _rows(con) == expected
        assert [r['key'] for r in _rows(con, DETAIL)] == ['REMOVED']
        urd.set_report_components(con, [])
        urd.set_excluded_epics(con, ['OTHER-EPIC'])
        assert _rows(con) == expected
        con.execute("UPDATE issues_all SET parent = 'OTHER-EPIC' WHERE key = 'REMOVED'")
        assert _rows(con) == _rows(con, DETAIL) == []
    finally:
        con.close()


def test_active_state_uses_freshest_global_evidence_without_assuming_scheduled_close():
    con = _db()
    try:
        _ticket(con, 'SCOPED')
        _ticket(con, 'OTHER')
        con.execute("UPDATE issues_all SET components = ['OTHER'] WHERE key = 'OTHER'")
        urd.set_report_components(con, ['TEAM'])
        con.execute("UPDATE issue_sprints_all SET fetched_at = '2026-01-12 11:30' "
                    "WHERE key = 'OTHER'")
        for state in ('closed', 'future', None):
            con.execute("UPDATE issue_sprints_all SET state = ? WHERE key = 'OTHER'", [state])
            assert _rows(con) == []
        con.execute("UPDATE issue_sprints_all SET state = 'closed' WHERE key = 'SCOPED'")
        con.execute("UPDATE issue_sprints_all SET state = 'active', \"end\" = '2026-01-10' "
                    "WHERE key = 'OTHER'")
        assert _rows(con)[0]['original'] == 1, 'overdue active sprint disappeared'
        con.execute("UPDATE issue_sprints_all SET fetched_at = '2026-01-12 11:30'")
        assert _rows(con) == [], 'conflicting equally fresh snapshots established activity'
        con.execute("UPDATE issue_sprints_all SET state = 'active', start = '2026-01-13'")
        assert _rows(con) == [], 'sprint starts after the snapshot'
    finally:
        con.close()


def test_active_start_uses_freshest_metadata_instead_of_stale_planned_dates():
    con = _db()
    try:
        _ticket(con, 'SCOPED', joined='2026-01-06')
        _ticket(con, 'OTHER')
        con.execute("UPDATE issues_all SET components = ['OTHER'] WHERE key = 'OTHER'")
        urd.set_report_components(con, ['TEAM'])
        con.execute("UPDATE issue_sprints_all SET state = 'future', fetched_at = '2026-01-04' "
                    "WHERE key = 'SCOPED'")
        con.execute("UPDATE issue_sprints_all SET start = '2026-01-07' WHERE key = 'OTHER'")
        for stale_start in ('2026-01-05', '2026-01-09', None):
            con.execute("UPDATE issue_sprints_all SET start = ? WHERE key = 'SCOPED'",
                        [stale_start])
            assert _rows(con) == [{
                'sprint': 'Sprint A (#7)', 'work': 'Main work',
                'original': 1, 'added': 0, 'removed': 0, 'unknown': 0,
            }], stale_start
            assert [(r['key'], r['origin']) for r in _rows(con, DETAIL)] == [
                ('SCOPED', 'Original'),
            ]
        # A missing or future start in the freshest snapshot cannot use an older date.
        con.execute("UPDATE issue_sprints_all SET start = '2026-01-05' WHERE key = 'SCOPED'")
        for fresh_start in (None, '2026-01-13'):
            con.execute("UPDATE issue_sprints_all SET start = ? WHERE key = 'OTHER'",
                        [fresh_start])
            assert _rows(con) == _rows(con, DETAIL) == [], fresh_start
    finally:
        con.close()


def test_equally_fresh_start_dates_must_agree():
    con = _db()
    try:
        _ticket(con, 'SYN-1', joined='2026-01-06')
        _ticket(con, 'SYN-2', joined='2026-01-06')
        for fetched_at in ('2026-01-12 11:00', None):
            con.execute('UPDATE issue_sprints_all SET fetched_at = ?', [fetched_at])
            con.execute("UPDATE issue_sprints_all SET start = '2026-01-07'")
            assert _rows(con)[0]['original'] == 2
            for conflicting_start in ('2026-01-05', None):
                con.execute("UPDATE issue_sprints_all SET start = ? WHERE key = 'SYN-2'",
                            [conflicting_start])
                assert _rows(con) == _rows(con, DETAIL) == [], conflicting_start
    finally:
        con.close()


def test_rendered_snapshot_links_tickets_and_keeps_its_cutoff_until_derive():
    con = _db()
    try:
        _ticket(con, 'SYN-1')
        con.execute("UPDATE issues_all SET summary = '<unsafe>'")
        urd.save_scope(con, last_sync_at='2026-01-13T12:00:00Z')
        for key in (SUMMARY, DETAIL):
            html = urd.run_chart(con, _chart(key))
            assert SNAPSHOT in html
            assert '2026-01-13T12:00:00Z' not in html
        html = urd.run_chart(con, _chart(DETAIL))
        assert 'https://example.invalid/browse/SYN-1' in html
        assert '&lt;unsafe&gt;' in html and '<unsafe>' not in html
        assert 'class="urd sortable"' in html
        con.execute('UPDATE sync_state SET derived_sync_at = NULL')
        for key in (SUMMARY, DETAIL):
            html = urd.run_chart(con, _chart(key))
            assert 'derive' in html.lower() and 'refresh' in html.lower()
            assert 'SYN-1' not in html
            assert _rows(con, key) == []
    finally:
        con.close()


def test_derive_freezes_its_source_sync_cutoff():
    con = _team_db()
    try:
        urd.save_scope(con, last_sync_at=SNAPSHOT)
        _derive(con)
        assert urd.load_scope(con).get('derived_sync_at') == SNAPSHOT
        # A sync can finish before derive begins, or CLI users can run sync alone.
        urd.save_scope(con, last_sync_at='2026-01-13T12:00:00Z')
        assert urd.load_scope(con)['derived_sync_at'] == SNAPSHOT
        _derive(con)
        assert urd.load_scope(con)['derived_sync_at'] == '2026-01-13T12:00:00Z'
        con.execute('UPDATE sync_state SET last_sync_at = NULL')
        _derive(con)
        assert urd.load_scope(con)['derived_sync_at'] is None
    finally:
        con.close()


def test_legacy_database_requires_derive_before_showing_active_scope():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, 'legacy.duckdb')
        con = urd.open_db(path)
        urd.save_scope(con, last_sync_at=SNAPSHOT)
        _derive(con)
        _ticket(con, 'SYN-LEGACY')
        con.execute('ALTER TABLE sync_state DROP COLUMN derived_sync_at')
        con.close()
        con = urd.open_db(path)
        try:
            assert urd.load_scope(con)['derived_sync_at'] is None
            html = urd.run_chart(con, _chart(SUMMARY))
            assert 'derive' in html.lower() and 'refresh' in html.lower()
            assert con.execute('SELECT key FROM issues').fetchall() == [('SYN-LEGACY',)]
            _derive(con)
            assert urd.load_scope(con)['derived_sync_at'] == SNAPSHOT
        finally:
            con.close()


def test_failed_derive_preserves_the_previous_snapshot():
    con = _team_db()
    try:
        urd.save_scope(con, last_sync_at=SNAPSHOT)
        _derive(con)
        assert urd.load_scope(con).get('derived_sync_at') == SNAPSHOT
        con.execute("INSERT INTO issues_all (key) VALUES ('SYN-CACHED')")
        con.execute("INSERT INTO raw_issues VALUES ('SYN-BAD', 'u', ?, ?)",
                    [urd._now(), '{"fields":{"created":"invalid timestamp"}}'])
        urd.save_scope(con, last_sync_at='2026-01-13T12:00:00Z')
        try:
            _derive(con)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid input should fail derivation')
        assert urd.load_scope(con)['derived_sync_at'] == SNAPSHOT
        assert con.execute('SELECT key FROM issues').fetchall() == [('SYN-CACHED',)]
    finally:
        con.close()


def test_interrupted_sync_cannot_backdate_newer_raw_sprint_state():
    con = _db()
    try:
        con.execute("INSERT INTO fields VALUES ('synthetic_sprint', 'Sprint', NULL)")
        sprint = {'id': 7, 'name': 'Sprint A', 'state': 'active',
                  'startDate': '2026-01-05T00:00:00Z'}
        raw = {'key': 'SYN-1', 'fields': {
            'created': '2026-01-01T00:00:00Z', 'updated': '2026-01-12T11:00:00Z',
            'issuetype': {'name': 'Task', 'subtask': False},
            'status': {'name': 'To Do', 'statusCategory': {'key': 'new'}},
            'synthetic_sprint': [sprint],
        }}
        con.execute('INSERT INTO raw_issues VALUES (?, ?, ?, ?)',
                    ['SYN-1', 'u', datetime(2026, 1, 12, 11), json.dumps(raw)])
        _derive(con)
        assert _rows(con)[0]['original'] == 1
        # A killed sync has committed a newer row but never advanced last_sync_at.
        sprint.update(state='closed', completeDate='2026-01-13T10:00:00Z')
        con.execute('UPDATE raw_issues SET fetched_at = ?, json = ?',
                    [datetime(2026, 1, 13, 11), json.dumps(raw)])
        _derive(con)
        assert urd.load_scope(con)['derived_sync_at'] is None, 'new rows got an old cutoff'
        html = urd.run_chart(con, _chart(SUMMARY))
        assert 'cutoff unavailable' in html and 'sync' in html.lower()
        assert SNAPSHOT not in html
        # Completing a sync establishes a usable cutoff again, even without refetching.
        urd.save_scope(con, last_sync_at='2026-01-13T12:00:00Z')
        _derive(con)
        assert urd.load_scope(con)['derived_sync_at'] == '2026-01-13T12:00:00Z'
        assert _rows(con) == []
    finally:
        con.close()


def test_sync_keeps_subsecond_cutoff_precision():
    con = urd.open_db(':memory:')
    urd.save_scope(con, project='SYN', earliest_since='2026-01-01')

    class Jira:
        def fields(self):
            return []

        def statuses(self):
            return []

        def search(self, jql):
            return []

    original = urd._now
    try:
        urd._now = lambda: datetime(2026, 1, 12, 12, 0, 0, 500000)
        urd.sync(con, Jira())
        assert urd.load_scope(con)['last_sync_at'] == SNAPSHOT
    finally:
        urd._now = original
        con.close()


def test_interrupted_pruning_invalidates_source_until_a_sync_completes():
    con = _db()
    now = urd._now
    raw = {'key': 'SYN-2', 'fields': {
        'created': '2026-01-01T00:00:00Z', 'updated': '2026-01-12T11:00:00Z',
        'issuetype': {'name': 'Task', 'subtask': False},
        'status': {'name': 'To Do', 'statusCategory': {'key': 'new'}},
        'synthetic_sprint': [{'id': 7, 'name': 'Sprint A', 'state': 'active',
                             'startDate': '2026-01-05T00:00:00Z'}],
    }}

    class Jira:
        interrupt = True

        def fields(self):
            return [{'id': 'synthetic_sprint', 'name': 'Sprint'}]

        def statuses(self):
            return [{'name': 'To Do', 'statusCategory': {'key': 'new'}}]

        def search(self, jql):
            return [('SYN-2', '2026-01-13T11:00:00Z')]

        def issue(self, key, fields):
            assert key == 'SYN-2'
            if self.interrupt:
                raise KeyboardInterrupt('synthetic interruption after pruning')
            return raw

    try:
        con.execute("INSERT INTO fields VALUES ('synthetic_sprint', 'Sprint', NULL)")
        for key in ('SYN-1', 'SYN-2'):
            con.execute('INSERT INTO raw_issues VALUES (?, ?, ?, ?)',
                        [key, raw['fields']['updated'], datetime(2026, 1, 12, 11),
                         json.dumps({**raw, 'key': key})])
        urd.save_scope(con, project='SYN', earliest_since='2026-01-01')
        _derive(con)
        assert _rows(con)[0]['original'] == 2
        urd._now = lambda: datetime(2026, 1, 13, 12)
        jira = Jira()
        try:
            urd.sync(con, jira)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError('the sync should have been interrupted')
        assert con.execute('SELECT key FROM raw_issues').fetchall() == [('SYN-2',)]
        # Already derived data remains available at its own cutoff until rebuilt.
        assert urd.load_scope(con)['last_sync_at'] == SNAPSHOT
        assert urd.load_scope(con)['derived_sync_at'] == SNAPSHOT
        assert _rows(con)[0]['original'] == 2
        _derive(con)
        assert urd.load_scope(con)['derived_sync_at'] is None, 'pruned rows got an old cutoff'
        assert 'cutoff unavailable' in urd.run_chart(con, _chart(SUMMARY))
        jira.interrupt = False
        urd.sync(con, jira)
        # Clearing source incompleteness must not bless the previous derived data.
        assert urd.load_scope(con)['derived_sync_at'] is None
        _derive(con)
        assert urd.load_scope(con)['derived_sync_at'] == '2026-01-13T12:00:00Z'
        assert _rows(con)[0]['original'] == 1
    finally:
        urd._now = now
        con.close()


def test_metadata_or_search_failure_requires_a_completed_sync_before_deriving_cutoff():
    class Jira:
        stage = None

        def fields(self):
            if self.stage == 'metadata':
                raise SystemExit('synthetic metadata failure')
            return []

        def statuses(self):
            return []

        def search(self, jql):
            if self.stage == 'search':
                raise SystemExit('synthetic search failure')
            return []

    for stage in ('metadata', 'search'):
        con = _db()
        try:
            urd.save_scope(con, project='SYN', earliest_since='2026-01-01')
            jira = Jira()
            jira.stage = stage
            try:
                urd.sync(con, jira)
            except SystemExit:
                pass
            else:
                raise AssertionError(f'{stage} should have failed')
            _derive(con)
            assert urd.load_scope(con)['derived_sync_at'] is None, stage
            # A completed sync with no issues must still clear the incomplete marker.
            jira.stage = None
            urd.sync(con, jira)
            _derive(con)
            scope = urd.load_scope(con)
            assert scope['derived_sync_at'] == scope['last_sync_at'] != SNAPSHOT
        finally:
            con.close()


if __name__ == '__main__':
    for name, fn in sorted(globals().copy().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok {name}')
    print('active sprint scope checks passed')
