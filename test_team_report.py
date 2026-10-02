"""Team report decisions, using synthetic sprint histories only."""
import charts
import test_helpers  # noqa: F401 - refuses network access
import urd


def _db():
    con = urd.open_db(':memory:')
    con.executemany('INSERT INTO statuses VALUES (?, ?)', [
        ('To Do', 'new'), ('In Progress', 'indeterminate'),
        ('Done', 'done'), ('Dropped', 'done'),
    ])
    urd.derive(con, 'To Do,In Progress,Done,Dropped', 'In Progress', 'Done', 'Dropped')
    return con


def _ticket(con, key, *, joined='2026-01-01', created='2025-12-01'):
    con.execute("INSERT INTO issues_all (key, created, status, status_category, "
                "components, abandoned) VALUES (?, ?, 'To Do', 'new', ['TEAM'], FALSE)",
                [key, created])
    con.execute("INSERT INTO issue_sprints_all VALUES "
                "(?, 7, 'Sprint A', 'closed', '2026-01-05', '2026-01-19', 1)", [key])
    if joined:
        _change(con, key, joined, 'Sprint', None, '7', 1)


def _change(con, key, ts, field, before, after, history_id=2):
    con.execute('INSERT INTO changes_all VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)',
                [key, ts, field, before, before, after, after, history_id])


def _rows(con):
    chart = next(c for c in charts.CHARTS if c.key == 'sprint_scope_changes')
    cursor = con.execute(chart.sql)
    return [dict(zip([d[0] for d in cursor.description], row, strict=True))
            for row in cursor.fetchall()]


def test_original_commitment_and_added_delivery_stay_separate():
    con = _db()
    for key in ('DONE', 'OPEN', 'REMOVED', 'DROPPED', 'REOPENED', 'LATE', 'RETURNED'):
        _ticket(con, key)
    _change(con, 'DONE', '2026-01-10', 'status', 'To Do', 'Done')
    _change(con, 'REMOVED', '2026-01-10', 'Sprint', '7', '')
    con.execute("DELETE FROM issue_sprints_all WHERE key = 'REMOVED'")
    _change(con, 'DROPPED', '2026-01-10', 'status', 'To Do', 'Dropped')
    _change(con, 'REOPENED', '2026-01-10', 'status', 'To Do', 'Done')
    _change(con, 'REOPENED', '2026-01-12', 'status', 'Done', 'In Progress', 3)
    _change(con, 'LATE', '2026-01-19', 'status', 'To Do', 'Done')
    _change(con, 'RETURNED', '2026-01-10', 'Sprint', '7', '')
    _change(con, 'RETURNED', '2026-01-11', 'Sprint', '', '7', 3)
    _ticket(con, 'ADDED', joined='2026-01-06')
    _change(con, 'ADDED', '2026-01-12', 'status', 'To Do', 'Done')
    _ticket(con, 'ADDED-REMOVED', joined='2026-01-07')
    _change(con, 'ADDED-REMOVED', '2026-01-13', 'Sprint', '7', '', 3)
    _ticket(con, 'CREATED-IN-SPRINT', joined=None, created='2026-01-08')
    _ticket(con, 'FUTURE', joined='2026-01-19')
    _ticket(con, 'REMOVED-BEFORE', joined='2026-01-01')
    _change(con, 'REMOVED-BEFORE', '2026-01-03', 'Sprint', '7', '')
    assert _rows(con) == [{
        'sprint': 'Sprint A', 'original_delivered': 1, 'original_unfinished': 4,
        'original_removed': 1, 'original_dropped': 1, 'added': 3, 'added_delivered': 1,
        'unknown': 0,
    }]
    # Current resolution and later reopening must not rewrite the sprint outcome.
    _change(con, 'DONE', '2026-01-20', 'status', 'Done', 'To Do', 4)
    assert _rows(con)[0]['original_delivered'] == 1
    urd.set_report_window(con, '2026-01-06')
    assert _rows(con) == []
    urd.set_report_window(con, None)
    urd.set_report_components(con, ['OTHER'])
    assert _rows(con) == []
    con.close()


def test_sprint_ids_and_boundaries_prevent_double_counting():
    con = _db()
    _ticket(con, 'BOUNDARY', joined='2026-01-05')
    _change(con, 'BOUNDARY', '2026-01-10', 'status', 'To Do', 'Done')
    _change(con, 'BOUNDARY', '2026-01-19', 'Sprint', '7', '')
    # A renamed embedded sprint and a parallel sprint with the same name.
    con.execute("INSERT INTO issue_sprints_all VALUES "
                "('BOUNDARY', 7, 'Sprint renamed', 'closed', '2026-01-05', '2026-01-19', 2)")
    _ticket(con, 'PARALLEL')
    con.execute("UPDATE issue_sprints_all SET sprint_id = 8 WHERE key = 'PARALLEL'")
    con.execute("UPDATE changes_all SET to_id = '8', to_str = '8' WHERE key = 'PARALLEL'")
    rows = _rows(con)
    assert len(rows) == 2
    assert sum(r['original_delivered'] for r in rows) == 1
    assert sum(r['original_unfinished'] for r in rows) == 1
    assert sum(r['original_removed'] for r in rows) == 0
    con.execute("UPDATE issue_sprints_all SET start = '2099-01-01', \"end\" = '2099-01-15'")
    assert _rows(con) == []
    con.close()


def test_conflicting_sprint_snapshots_do_not_duplicate_or_reclassify_tickets():
    con = _db()
    _ticket(con, 'ORIGINAL')
    _ticket(con, 'ADDITION', joined='2026-01-05 12:00')
    # One stale snapshot starts later, after ADDITION joined. The chart uses
    # the earliest recorded start consistently for membership and its window.
    con.execute("INSERT INTO issue_sprints_all VALUES "
                "('ORIGINAL', 7, 'Sprint A', 'closed', '2026-01-06', '2026-01-19', 2)")
    assert _rows(con) == [{
        'sprint': 'Sprint A', 'original_delivered': 0, 'original_unfinished': 1,
        'original_removed': 0, 'original_dropped': 0, 'added': 1, 'added_delivered': 0,
        'unknown': 0,
    }]
    con.close()


def test_creation_membership_survives_later_sprint_changes():
    for changed_at, after, delivered in (
        ('2026-01-12', '7, 8', 1),
        ('2026-01-20', '7, 8', 1),
        ('2026-01-12', '', 0),
        ('2026-01-20', '', 1),
    ):
        con = _db()
        _ticket(con, 'CREATED', joined=None, created='2026-01-08')
        _change(con, 'CREATED', '2026-01-10', 'status', 'To Do', 'Done')
        assert _rows(con)[0]['added_delivered'] == 1
        _change(con, 'CREATED', changed_at, 'Sprint', '7', after, 3)
        if not after:
            con.execute("DELETE FROM issue_sprints_all WHERE key = 'CREATED'")
            # Another member supplies dates after this ticket is removed.
            _ticket(con, 'ANCHOR')
        rows = _rows(con)
        assert len(rows) == 1, (changed_at, after, rows)
        assert rows[0]['added'] == 1, (changed_at, after, rows)
        assert rows[0]['added_delivered'] == delivered, (changed_at, after, rows)
        con.close()

    # A first explicit addition after the sprint is not membership at creation.
    con = _db()
    _ticket(con, 'LATER', joined='2026-01-20', created='2026-01-08')
    assert _rows(con) == []
    con.close()


def test_scope_filters_preserve_sprint_dates_for_removed_tickets():
    con = _db()
    _ticket(con, 'REMOVED')
    _change(con, 'REMOVED', '2026-01-10', 'Sprint', '7', '')
    con.execute("DELETE FROM issue_sprints_all WHERE key = 'REMOVED'")
    _ticket(con, 'OTHER-TEAM')
    con.execute("UPDATE issues_all SET components = ['OTHER'], parent = 'OTHER-EPIC' "
                "WHERE key = 'OTHER-TEAM'")
    assert _rows(con)[0]['original_removed'] == 1
    expected = [{
        'sprint': 'Sprint A', 'original_delivered': 0, 'original_unfinished': 0,
        'original_removed': 1, 'original_dropped': 0, 'added': 0, 'added_delivered': 0,
        'unknown': 0,
    }]
    urd.set_report_components(con, ['TEAM'])
    assert _rows(con) == expected
    urd.set_report_components(con, [])
    urd.set_excluded_epics(con, ['OTHER-EPIC'])
    assert _rows(con) == expected
    # Metadata alone must not cause an excluded ticket to be counted.
    con.execute("UPDATE issues_all SET parent = 'OTHER-EPIC' WHERE key = 'REMOVED'")
    assert _rows(con) == []
    con.close()


def test_uncertain_original_membership_is_not_added_work():
    con = _db()
    _ticket(con, 'ORIGINAL', joined=None)
    assert _rows(con)[0]['original_unfinished'] == 1
    _change(con, 'ORIGINAL', '2026-01-10', 'Sprint', '7', '')
    _change(con, 'ORIGINAL', '2026-01-12', 'Sprint', '', '7', 3)
    _change(con, 'ORIGINAL', '2026-01-15', 'status', 'To Do', 'Done', 4)
    row = _rows(con)[0]
    assert row['added'] == row['added_delivered'] == 0, row
    assert row['unknown'] == 1, row
    assert sum(row[k] for k in row if k.startswith('original_')) == 0, row
    # In contrast, an explicit first addition proves absence before it.
    _ticket(con, 'KNOWN-ADDITION', joined='2026-01-12')
    row = _rows(con)[0]
    assert row['added'] == 1 and row['unknown'] == 1, row
    con.close()


def test_unknown_status_is_visible_without_claiming_unfinished_work():
    con = _db()
    _ticket(con, 'ORIGINAL')
    _change(con, 'ORIGINAL', '2026-01-10', 'status', 'To Do', 'Retired status')
    row = _rows(con)[0]
    assert row['original_unfinished'] == row['original_delivered'] == 0, row
    assert row['unknown'] == 1, row
    chart = next(c for c in charts.CHARTS if c.key == 'sprint_scope_changes')
    assert 'unknown' in chart.options['series']
    assert 'unknown' in urd.run_chart(con, chart)
    con.execute("INSERT INTO statuses VALUES ('Retired status', 'done')")
    row = _rows(con)[0]
    assert row['original_delivered'] == 1 and row['unknown'] == 0, row
    # A known addition still belongs in scope even if its outcome is unknown.
    _ticket(con, 'ADDED', joined='2026-01-12')
    _change(con, 'ADDED', '2026-01-14', 'status', 'To Do', 'Unmapped status')
    row = _rows(con)[0]
    assert row['added'] == 1 and row['added_delivered'] == 0 and row['unknown'] == 1, row
    con.close()


def test_attention_precedes_flow_and_commitments_in_the_report():
    con = _db()
    sections = urd.render_sections(con)
    assert [name for name, _ in sections] == [
        'Attention today', 'Flow over time', 'Commitments', 'Retrospective',
    ]
    attention = ''.join(sections[0][1])
    assert 'id="aging_wip"' in attention and 'id="carried_sprints"' in attention
    assert 'Sprint scope changes' in sections[2][1][0]
    con.close()


if __name__ == '__main__':
    for name, fn in sorted(globals().copy().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok {name}')
    print('all tests passed')
