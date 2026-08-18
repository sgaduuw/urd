import json
import os
import tempfile

import urd
import wizard


def _proposal(**over):
    base = dict(site="example.atlassian.net", email="a@b.c", project="PROJ",
                component="TEAM", since="2026-01-01",
                status_order="To Do,In Progress,Review,Done",
                start_status="In Progress", review_status="Review",
                abandoned_status="")
    return wizard.Proposal(**{**base, **over})


class _Opener:
    """Answers by URL fragment, the same shape urd's own tests use."""

    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def __call__(self, url, headers):
        self.urls.append(url)
        for fragment, (status, payload) in self.routes.items():
            if fragment in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected request: {url}")


def _ok_opener(total=751):
    return _Opener({
        "/myself": (200, {"displayName": "A Person"}),
        "/search/jql": (200, {"issues": [{"key": f"PROJ-{i}",
                                          "fields": {"updated": "u"}}
                                         for i in range(total)], "isLast": True}),
    })


def test_a_good_scope_reports_who_and_how_many():
    result = wizard.validate(_proposal(), "tok", opener=_ok_opener(3))
    assert result.ok is True
    assert result.who == "A Person"
    assert result.issues == 3
    assert result.problem == ""


def test_a_rejected_credential_is_reported_not_raised():
    """The whole reason this exists. An environment variable fails minutes into a
    sync; this fails in seconds and says which half was wrong."""
    opener = _Opener({"/myself": (401, {"message": "no"})})
    result = wizard.validate(_proposal(), "tok", opener=opener)
    assert result.ok is False
    assert "401" in result.problem or "authenticat" in result.problem.lower()
    assert result.issues == 0


def test_the_credential_is_checked_before_the_scope():
    """A bad token and a bad component produce the same empty count, so checking
    the token first is what makes the message specific."""
    opener = _Opener({"/myself": (401, {})})
    wizard.validate(_proposal(), "tok", opener=opener)
    assert all("/search" not in url for url in opener.urls), opener.urls


def test_a_scope_matching_nothing_is_reported_as_such():
    opener = _ok_opener(0)
    result = wizard.validate(_proposal(component="NOPE"), "tok", opener=opener)
    assert result.ok is False
    assert "0" in result.problem or "no issues" in result.problem.lower()


def test_validate_never_writes_anything():
    """A validation that saved would make a typo permanent."""
    con = urd.open_db(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    before = urd.load_scope(con)
    wizard.validate(_proposal(), "tok", opener=_ok_opener())
    assert urd.load_scope(con) == before


def test_apply_writes_every_field_of_the_proposal():
    con = urd.open_db(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    wizard.apply(con, _proposal())
    scope = urd.load_scope(con)
    assert scope["site"] == "example.atlassian.net"
    assert scope["project"] == "PROJ"
    assert scope["component"] == "TEAM"
    assert scope["earliest_since"] == "2026-01-01"
    assert scope["status_order"] == "To Do,In Progress,Review,Done"
    assert scope["start_status"] == "In Progress"
    assert scope["review_status"] == "Review"


def test_an_incomplete_proposal_is_refused_before_any_request():
    opener = _ok_opener()
    for field in ("site", "project", "since", "status_order", "start_status"):
        result = wizard.validate(_proposal(**{field: ""}), "tok", opener=opener)
        assert result.ok is False, field
        assert field.replace("_", " ") in result.problem.lower() or field in result.problem
    assert opener.urls == [], "a request was made for an incomplete proposal"


def test_a_missing_token_is_reported_rather_than_crashing():
    result = wizard.validate(_proposal(), "", opener=_ok_opener())
    assert result.ok is False
    assert "token" in result.problem.lower()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
