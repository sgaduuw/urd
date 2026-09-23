"""A broken search response must never become authority to prune the mirror."""
import json
import urllib.parse

import urd

UPDATED = "2026-09-01T00:00:00.000+0000"
ISSUE = {"key": "PROJ-1", "fields": {"updated": UPDATED}}


def _sync(pages):
    con = urd.open_db(":memory:")
    urd.save_scope(con, project="PROJ", earliest_since="2026-01-01",
                   last_sync_at="2026-08-01T00:00:00Z", fetched_fields=urd.fetch_fields(con))
    for key in ("PROJ-1", "PROJ-2"):
        con.execute("INSERT INTO raw_issues VALUES (?, ?, ?, ?)",
                    [key, UPDATED, urd._now(), "{}"])
    con.execute("INSERT INTO sync_errors VALUES (?, ?, ?)",
                ["PROJ-2", urd._now(), "previous failure"])
    requested_fields = []
    responses = iter(pages)

    def opener(url, headers):
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/search/jql"):
            body = next(responses)
        elif path.endswith("/myself"):
            body = {"accountId": "synthetic"}
        elif "/issue/" in path:
            requested_fields.extend(urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query)["fields"][0].split(","))
            body = {**ISSUE, "changelog": {"histories": [], "total": 0}}
        else:
            body = []
        return 200, json.dumps(body).encode()

    jira = urd.Jira("example.invalid", "user@example.invalid", "dummy", opener=opener)
    return con, jira, requested_fields


def test_invalid_search_preserves_cached_issues_errors_and_sync_timestamp():
    malformed = [
        {}, [], {"issues": None, "isLast": True},
        {"issues": []}, {"issues": [], "isLast": "true"},
        {"issues": [], "isLast": False},
        {"issues": [], "isLast": False, "nextPageToken": 12},
        {"issues": [{}], "isLast": True},
        {"issues": [{"key": "PROJ-1", "fields": {"updated": None}}], "isLast": True},
    ]
    first = {"issues": [ISSUE], "isLast": False, "nextPageToken": "next"}
    for bad in malformed:
        for pages in ([bad], [first, bad]):
            con, jira, _ = _sync(pages)
            try:
                try:
                    urd.sync(con, jira)
                except SystemExit as exc:
                    assert "search" in str(exc).lower(), str(exc)
                else:
                    raise AssertionError(f"malformed search was accepted: {pages!r}")
                assert con.execute("SELECT key FROM raw_issues ORDER BY key").fetchall() == [
                    ("PROJ-1",), ("PROJ-2",)]
                assert con.execute("SELECT key, error FROM sync_errors").fetchall() == [
                    ("PROJ-2", "previous failure")]
                assert urd.load_scope(con)["last_sync_at"] == "2026-08-01T00:00:00Z"
            finally:
                con.close()


def test_valid_empty_search_can_still_remove_issues_that_left_scope():
    con, jira, _ = _sync([{"issues": [], "isLast": True}])
    try:
        assert urd.sync(con, jira) == 0
        assert con.execute("SELECT count(*) FROM raw_issues").fetchone() == (0,)
    finally:
        con.close()


def test_sync_does_not_request_unused_worklog_data():
    con, jira, fields = _sync([{"issues": [ISSUE], "isLast": True}])
    try:
        con.execute("DELETE FROM raw_issues")
        assert urd.sync(con, jira) == 0
        assert "summary" in fields
        assert "worklog" not in fields
    finally:
        con.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
