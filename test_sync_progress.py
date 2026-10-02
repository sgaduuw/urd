"""Sync progress must be visible before the operation finishes."""
import contextlib
import io
import re
from unittest.mock import patch

import projects
import test_helpers  # noqa: F401
import urd
from test_projects import _FakeJira, _wait_idle


class _Output(io.StringIO):
    visible = ''

    def flush(self):
        self.visible = self.getvalue()


def test_sync_reports_phases_and_counts_while_running():
    output = _Output()
    clock = [0]
    con = urd.open_db(':memory:')
    urd.save_scope(con, project='SYN', earliest_since='2026-01-01')

    class Jira:
        def get(self, path):
            assert 'authenticating' in output.visible

        def fields(self):
            assert 'loading metadata' in output.visible
            return []

        def statuses(self):
            return []

        def search(self, jql):
            assert 'discovering scope' in output.visible
            for n in range(101):
                if n == 0:
                    clock[0] += 11
                if n == 1:
                    assert 'discovered 1 tickets' in output.visible
                yield f'SYN-{n}', '2026-01-01'
            assert 'discovered 100 tickets' in output.visible

        def issue(self, key, fields):
            assert '101 in scope, 101 to fetch' in output.visible
            if key == 'SYN-0':
                clock[0] += 11
            if key == 'SYN-1':
                assert 'processed 1/101; 0 failed' in output.visible
            if key == 'SYN-50':
                assert 'processed 50/101; 1 failed' in output.visible
            if key == 'SYN-49':
                raise SystemExit('sensitive-response-content')
            return {'key': key, 'fields': {'updated': '2026-01-01'}}

    with contextlib.redirect_stderr(output), patch('urd.time.monotonic', lambda: clock[0]):
        assert urd.sync(con, Jira()) == 0
    assert 'processed 101/101; 1 failed' in output.visible
    assert 'sync complete; 1 error(s) outstanding' in output.visible
    assert 'sensitive-response-content' not in output.visible
    assert output.visible.count('processed ') == 4
    assert re.search(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', output.visible)
    assert all("['SYN']" in line and 'elapsed=' in line
               for line in output.visible.splitlines())
    assert con.execute('SELECT count(*) FROM raw_issues').fetchone() == (100,)
    con.close()


def test_sync_reports_zero_changes_and_terminal_failure():
    con = urd.open_db(':memory:')
    urd.save_scope(con, project='EMPTY', earliest_since='2026-01-01')

    class Jira:
        def fields(self):
            return []

        def statuses(self):
            return []

        def search(self, jql):
            return []

    output = _Output()
    with contextlib.redirect_stderr(output):
        urd.sync(con, Jira())
    assert '0 in scope, 0 to fetch' in output.visible
    assert 'sync complete; 0 error(s) outstanding' in output.visible

    class Broken(Jira):
        def fields(self):
            raise SystemExit('private-response')

    output = _Output()
    with contextlib.redirect_stderr(output):
        try:
            urd.sync(con, Broken())
        except SystemExit as exc:
            assert str(exc) == 'private-response'
        else:
            raise AssertionError('sync swallowed the failure')
    assert 'sync failed (SystemExit)' in output.visible
    assert 'private-response' not in output.visible
    con.close()


def test_background_refresh_reports_derivation_completion_and_failure():
    project = projects.Project('alpha', ':memory:')
    urd.save_scope(project.con, site='example.invalid', project='SYN',
                   earliest_since='2026-01-01', status_order='To Do,Done',
                   start_status='To Do')
    output = _Output()
    with contextlib.redirect_stderr(output):
        assert projects.start_refresh(project, jira_factory=lambda scope: _FakeJira())
        assert _wait_idle(project) == 'idle'
        with project.lock:
            pass
    assert "['alpha'] deriving" in output.visible
    assert "['alpha'] refresh complete" in output.visible

    def fail(scope):
        raise SystemExit('private-response')

    output = _Output()
    with contextlib.redirect_stderr(output):
        assert projects.start_refresh(project, jira_factory=fail)
        assert _wait_idle(project) == 'failed'
        with project.lock:
            pass
    assert "['alpha'] refresh failed (SystemExit)" in output.visible
    assert 'private-response' not in output.visible
    assert project.job.message == 'private-response'
    project.con.close()


def test_progress_records_are_written_as_complete_lines():
    class Output(_Output):
        def write(self, text):
            assert text.endswith('\n') and text.count('\n') == 1, repr(text)
            return super().write(text)

    output = Output()
    with contextlib.redirect_stderr(output):
        urd.log_progress('alpha', 'sync starting', urd.time.monotonic())
    assert "['alpha'] sync starting" in output.visible


def test_broken_progress_output_preserves_results_and_exceptions():
    class Output(_Output):
        def write(self, text):
            raise OSError('log sink unavailable')

    class FlushOutput(_Output):
        def flush(self):
            raise OSError('log sink unavailable')

    class BrokenJira(_FakeJira):
        def fields(self):
            raise SystemExit('original sync failure')

    closed = io.StringIO()
    closed.close()
    for output in (Output(), FlushOutput(), closed):
        project = projects.Project('alpha', ':memory:')
        urd.save_scope(project.con, site='example.invalid', project='SYN',
                       earliest_since='2026-01-01', status_order='To Do,Done',
                       start_status='To Do')
        try:
            with contextlib.redirect_stderr(output):
                try:
                    urd.sync(project.con, BrokenJira())
                except SystemExit as exc:
                    assert str(exc) == 'original sync failure'
                else:
                    raise AssertionError('sync swallowed its original failure')
                assert projects.start_refresh(project, jira_factory=lambda scope: _FakeJira())
                _wait_idle(project)
                with project.lock:
                    assert project.job.state == 'idle', project.job.message
                    assert project.con.execute('SELECT count(*) FROM issues').fetchone() == (1,)
        finally:
            project.con.close()


if __name__ == '__main__':
    test_progress_records_are_written_as_complete_lines()
    test_broken_progress_output_preserves_results_and_exceptions()
    test_sync_reports_phases_and_counts_while_running()
    test_sync_reports_zero_changes_and_terminal_failure()
    test_background_refresh_reports_derivation_completion_and_failure()
    print('sync progress checks passed')
