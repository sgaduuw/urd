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

    jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
    jira.get("/field")
    assert attempt[0] == 2


def test_transport_failures_exit_on_second_attempt():
    """Transport failure (599 status) on both attempts should raise SystemExit."""
    def opener(url, headers):
        return urd.TRANSPORT_ERROR_STATUS, b"timeout"

    jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
    try:
        jira.get("/field")
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "GET" in str(e) and "599" in str(e)


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


def test_redirects_are_refused():
    """A redirect status should be treated as an error, not followed."""
    def opener(url, headers):
        # Simulate a 302 response from _urlopen (the redirect handler would
        # have already raised before we get here in production, but this tests
        # that if it somehow returned a 302, get() would fail loudly)
        return 302, b"Found"

    jira = urd.Jira("example.atlassian.net", "a@b.c", "t", opener=opener)
    try:
        jira.get("/field")
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "302" in str(e)


def test_the_client_can_only_issue_get_requests():
    """Read-only is a design guarantee, not a preference, so pin it structurally.
    Every other test injects a fake opener, which leaves the real request builder
    untested."""
    source = (pathlib.Path(__file__).parent / "urd.py").read_text()
    assert 'method="GET"' in source
    assert not any(
        f'method="{verb}"' in source for verb in ("POST", "PUT", "PATCH", "DELETE")
    )
    assert "data=" not in source  # urllib sends a body, and so POSTs, when data is set


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
