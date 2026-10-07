"""Jira catalogue pagination and credential boundaries, using synthetic responses."""
import json
import os
import urllib.parse
from unittest.mock import MagicMock, patch

import capacity
import urd


def page(values, start=0, last=True, total=None):
    return {"values": values, "startAt": start, "maxResults": 100, "isLast": last,
            "total": len(values) + start if total is None else total}


def test_empty_future_sprint_and_atomic_failure():
    responses = [
        page([{"id": 1, "name": "Board", "type": "scrum"}]),
        page([{"id": 11, "name": "Empty future", "state": "future"}], last=False, total=2),
        page([{"id": 12, "name": "Second", "state": "future"}], start=1, total=2),
    ]
    urls = []

    def opener(url, headers):
        urls.append(url)
        value = responses.pop(0)
        return value if isinstance(value, tuple) else (200, json.dumps(value).encode())

    jira = urd.Jira("example.invalid", "reader@example.invalid", "dummy", opener=opener)
    con = urd.open_db(":memory:")
    capacity.refresh_catalogue(con, jira, "EX", [1])
    assert capacity.sprint(con, 11)["name"] == "Empty future"
    assert capacity.sprint(con, 11).get("startDate") is None
    assert capacity.sprint(con, 11)["boards"] == [1]
    assert len(capacity.sprints(con, [1])) == 2
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
    assert query["projectKeyOrId"] == ["EX"] and query["type"] == ["scrum"]
    assert all("/rest/agile/1.0/board" in url for url in urls)
    before = con.execute("SELECT * FROM capacity_sprints ORDER BY id").fetchall()
    responses.extend([
        page([{"id": 1, "name": "Changed", "type": "scrum"}]),
        page([{"id": 11, "name": "Changed", "state": "future"}], last=False, total=2),
        (403, b"forbidden"),
    ])
    try:
        capacity.refresh_catalogue(con, jira, "EX", [1])
    except SystemExit:
        pass
    else:
        raise AssertionError("Permission error was hidden")
    assert con.execute("SELECT * FROM capacity_sprints ORDER BY id").fetchall() == before
    assert capacity.boards(con)[0]["name"] == "Board"
    con.close()


def test_malformed_and_stalled_pagination():
    failures = [{}, page([], last=False, total=1),
                page([{"id": 1, "name": "Board"}], last=True, total=2),
                page([{"id": 1, "name": "Board"}], start=2)]
    for response in failures:
        jira = urd.Jira("example.invalid", "reader@example.invalid", "dummy",
                        opener=lambda u, h, response=response: (200, json.dumps(response).encode()))
        try:
            list(jira.boards("EX"))
        except SystemExit:
            pass
        else:
            raise AssertionError(f"Accepted incomplete page: {response!r}")
    calls = []
    value = {"id": 1, "name": "Board", "type": "scrum"}

    def repeated(url, headers):
        offset = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["startAt"][0])
        calls.append(offset)
        return 200, json.dumps(page([value], start=offset, last=False, total=3)).encode()

    jira = urd.Jira("example.invalid", "reader@example.invalid", "dummy", opener=repeated)
    try:
        list(jira.boards("EX"))
    except SystemExit:
        pass
    else:
        raise AssertionError("Repeated page was accepted")
    assert len(calls) == 2


def test_agile_keeps_trusted_get_only_transport():
    response = MagicMock()
    response.__enter__.return_value.status = 200
    response.__enter__.return_value.read.return_value = json.dumps(page([])).encode()
    with patch.dict(os.environ, {"URD_JIRA_HOST": "trusted.invalid"}), \
            patch.object(urd._OPENER, "open", return_value=response) as opened:
        jira = urd.Jira("trusted.invalid", "reader@example.invalid", "dummy")
        assert list(jira.boards("EX")) == []
        request = opened.call_args.args[0]
        assert request.full_url.startswith("https://trusted.invalid/rest/agile/1.0/board?")
        assert request.get_method() == "GET"
        for bad in ("https://collector.invalid/", "//collector.invalid/", "/board/../evil"):
            try:
                jira.agile_get(bad)
            except (ValueError, SystemExit):
                pass
            else:
                raise AssertionError("Arbitrary Agile path accepted")
        try:
            urd.Jira("collector.invalid", "reader@example.invalid", "dummy").agile_get("/board")
        except SystemExit as exc:
            assert "URD_JIRA_HOST" in str(exc)
        else:
            raise AssertionError("Untrusted host received credentials")
        assert opened.call_count == 1
        try:
            urd._NoRedirect().redirect_request(request, None, 302, "Found", {},
                                               "https://collector.invalid/")
        except SystemExit:
            pass
        else:
            raise AssertionError("Redirect accepted")



def test_changing_catalogue_total_retains_cache():
    for final_total in (2, None):
        con = urd.open_db(":memory:")
        capacity.cache_sprint(con, {
            "id": 99, "name": "Previous", "state": "future", "boards": [1]})
        before = con.execute("SELECT * FROM capacity_sprints ORDER BY id").fetchall()
        pages = iter([
            page([{"id": 1, "name": "Board", "type": "scrum"}]),
            page([{"id": 11, "name": "First", "state": "future"}], last=False, total=3),
            {"startAt": 1, "isLast": True, "total": final_total,
             "values": [{"id": 12, "name": "Second", "state": "future"}]},
        ])
        jira = urd.Jira(
            "example.invalid", "reader@example.invalid", "dummy",
            opener=lambda url, headers, pages=pages: (200, json.dumps(next(pages)).encode()))
        rejected = False
        try:
            capacity.refresh_catalogue(con, jira, "EX", [1])
        except (SystemExit, ValueError):
            rejected = True
        after = con.execute("SELECT * FROM capacity_sprints ORDER BY id").fetchall()
        con.close()
        assert rejected and after == before, (rejected, after == before)


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("capacity catalogue checks passed")
