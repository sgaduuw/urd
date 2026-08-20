import json
import os
import tempfile

import test_helpers  # noqa: F401 - installs the network-refusal guard on import
import urd
import wizard


def _proposal(**over):
    base = dict(site="example.invalid", email="a@b.c", project="PROJ",
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


def test_apply_writes_every_field_of_the_proposal():
    con = urd.open_db(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    wizard.apply(con, _proposal())
    scope = urd.load_scope(con)
    assert scope["site"] == "example.invalid"
    assert scope["email"] == "a@b.c"
    assert scope["project"] == "PROJ"
    assert scope["component"] == "TEAM"
    assert scope["earliest_since"] == "2026-01-01"
    assert scope["status_order"] == "To Do,In Progress,Review,Done"
    assert scope["start_status"] == "In Progress"
    assert scope["review_status"] == "Review"
    # The base proposal carries "", and apply maps empty to None: deliberate,
    # not an omission, so make the mapping explicit here.
    assert scope["abandoned_status"] is None


def test_apply_writes_a_non_empty_abandoned_status():
    """The empty-to-None case above would also pass if apply dropped the field
    entirely, since the column's default is NULL either way; this pins the
    other half of the mapping, that a real value is actually written."""
    con = urd.open_db(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    wizard.apply(con, _proposal(abandoned_status="Won't do"))
    assert urd.load_scope(con)["abandoned_status"] == "Won't do"


def test_an_incomplete_proposal_is_refused_before_any_request():
    # Pinned literally: the loop below iterates this same constant, so a name
    # silently dropped from it would just shorten the loop and still pass.
    assert wizard.REQUIRED_FOR_SCOPE == ("site", "email", "project", "since")
    opener = _ok_opener()
    for field in wizard.REQUIRED_FOR_SCOPE:
        result = wizard.validate(_proposal(**{field: ""}), "tok", opener=opener)
        assert result.ok is False, field
        assert field.replace("_", " ") in result.problem.lower() or field in result.problem
    assert opener.urls == [], "a request was made for an incomplete proposal"


def test_a_missing_token_is_reported_rather_than_crashing():
    result = wizard.validate(_proposal(), "", opener=_ok_opener())
    assert result.ok is False
    assert "token" in result.problem.lower()


def test_token_reports_a_missing_security_binary_as_no_token():
    """check=False only stops a non-zero exit from raising; the binary not
    existing at all (every non-macOS host, including the container this ships
    in) raises FileNotFoundError regardless, which used to escape as a bare
    exception instead of the same "no API token" SystemExit every other
    no-credential path already gets."""
    original_run = urd.subprocess.run

    def _no_such_binary(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "security")

    urd.subprocess.run = _no_such_binary
    try:
        try:
            urd.token({})
            raise AssertionError("expected SystemExit")
        except SystemExit as exc:
            assert "no API token" in str(exc)
    finally:
        urd.subprocess.run = original_run


_INSTANCE_STATUSES = [
    {"name": "To Do", "statusCategory": {"key": "new"}},
    {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
    {"name": "Code Review", "statusCategory": {"key": "indeterminate"}},
    {"name": "Blocked", "statusCategory": {"key": "indeterminate"}},
    {"name": "Done", "statusCategory": {"key": "done"}},
    {"name": "Won't Do", "statusCategory": {"key": "done"}},
    {"name": "Retired", "statusCategory": {"key": "new"}},
    # The real API does send this, and test_urd.py already pins the case.
    {"name": "Odd", "statusCategory": None},
]

_WORKFLOW = [
    # Deliberately not already in category order (new, then done, then
    # uncategorised, then indeterminate): a fixture that happens to list
    # statuses in the order propose() would sort them into cannot tell a
    # sorted result from an unsorted one, which is exactly how "sorted(...)"
    # once quietly became "list(...)" without a single test noticing.
    {"statuses": [{"name": "To Do"}, {"name": "Done"}, {"name": "Won't Do"},
                  {"name": "Odd"}]},
    {"statuses": [{"name": "In Progress"}, {"name": "Code Review"},
                  {"name": "Blocked"}]},
]


def _discovery_opener(statuses=None, workflow=None, fail=None):
    """Answers the two discovery endpoints. `fail` names a URL fragment that
    should return 403 instead, so the degradation path is exercised with the
    real client rather than a stub."""
    def opener(url, headers):
        if fail and fail in url:
            return 403, b'{"message": "no"}'
        if "/project/" in url:
            return 200, json.dumps(workflow if workflow is not None else _WORKFLOW).encode()
        if url.rstrip("/").endswith("/status"):
            payload = statuses if statuses is not None else _INSTANCE_STATUSES
            return 200, json.dumps(payload).encode()
        raise AssertionError(f"unexpected request: {url}")
    return opener


def test_discovery_keeps_only_the_project_s_own_statuses():
    """Retired is in the instance list but not this project's workflow, so it
    must not reach the proposal."""
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    assert found.problem == ""
    assert "Retired" not in [s.name for s in found.statuses]
    assert "To Do" in [s.name for s in found.statuses]


def test_discovery_attaches_the_category_from_the_instance_list():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    by_name = {s.name: s.category for s in found.statuses}
    assert by_name["To Do"] == "new"
    assert by_name["In Progress"] == "indeterminate"
    assert by_name["Done"] == "done"


def test_a_status_the_instance_list_does_not_describe_has_no_category():
    """statusCategory can be null, and a status in the workflow can be absent
    from the instance list entirely. Both end up uncategorised rather than
    guessed at."""
    workflow = [{"statuses": [{"name": "To Do"}, {"name": "Odd"},
                              {"name": "Unlisted"}]}]
    found = wizard.discover(_proposal(), "tok",
                            opener=_discovery_opener(workflow=workflow))
    by_name = {s.name: s.category for s in found.statuses}
    assert by_name["Odd"] == ""
    assert by_name["Unlisted"] == ""


def test_discovery_reports_a_refusal_instead_of_raising():
    """Either call can 403 on a restricted project, and setup must still finish."""
    found = wizard.discover(_proposal(), "tok",
                            opener=_discovery_opener(fail="/project/"))
    assert found.statuses == []
    assert "403" in found.problem or "could not" in found.problem.lower()


def test_discover_survives_a_non_json_response():
    """An interposing proxy or captive portal can answer a 200 with an HTML
    body; json.loads inside Jira.get then raises JSONDecodeError, which used
    to escape past discover's SystemExit-only guard to the generic 500 and
    discard everything typed on page one. SystemExit is not an Exception
    subclass, so both must be caught."""
    def opener(url, headers):
        if "/project/" in url:
            return 200, json.dumps(_WORKFLOW).encode()
        if url.rstrip("/").endswith("/status"):
            return 200, b"<html>not json</html>"
        raise AssertionError(f"unexpected request: {url}")
    found = wizard.discover(_proposal(), "tok", opener=opener)
    assert found.statuses == []
    assert "could not" in found.problem.lower()


def test_discovery_reads_every_project_key_not_just_the_first():
    """_refresh_workflow_statuses loops every comma separated project key;
    discover used to read only the first, so a multi-project scope was told
    the workflow is only that one project's."""
    def opener(url, headers):
        if "/project/PROJA/statuses" in url:
            return 200, json.dumps([{"statuses": [{"name": "To Do"}]}]).encode()
        if "/project/PROJB/statuses" in url:
            return 200, json.dumps([{"statuses": [{"name": "Blocked"}]}]).encode()
        if url.rstrip("/").endswith("/status"):
            return 200, json.dumps(_INSTANCE_STATUSES).encode()
        raise AssertionError(f"unexpected request: {url}")
    found = wizard.discover(_proposal(project="PROJA,PROJB"), "tok", opener=opener)
    assert [s.name for s in found.statuses] == ["To Do", "Blocked"]


def test_the_proposed_order_is_category_order_with_uncategorised_last():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    order = wizard.propose(found.statuses)["status_order"].split(",")
    assert order[0] == "To Do"
    assert order.index("In Progress") < order.index("Done")
    assert order[-1] == "Odd", order


def test_the_first_indeterminate_status_is_proposed_as_the_start():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    assert wizard.propose(found.statuses)["start_status"] == "In Progress"


def test_a_review_looking_status_is_proposed_as_review():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    assert wizard.propose(found.statuses)["review_status"] == "Code Review"


def test_no_review_looking_status_leaves_review_blank():
    """A wrong guess here silently mislabels every review measurement, so
    nothing is better than something."""
    statuses = [wizard.Status("To Do", "new"), wizard.Status("Doing", "indeterminate")]
    assert wizard.propose(statuses)["review_status"] == ""


def test_rejection_looking_done_statuses_are_proposed_as_abandoned():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    assert wizard.propose(found.statuses)["abandoned_status"] == "Won't Do"


def test_a_review_looking_name_outside_indeterminate_is_not_proposed():
    """"Reviewed" reads as review-complete, not review-in-progress. The shared
    fixture's only review-hint match happens to be indeterminate, which cannot
    tell a category-scoped search apart from a name-only one; this local
    fixture can, by putting the hint match in "done" instead."""
    statuses = [wizard.Status("To Do", "new"),
                wizard.Status("In Progress", "indeterminate"),
                wizard.Status("Reviewed", "done")]
    assert wizard.propose(statuses)["review_status"] == ""


def test_an_abandoned_looking_name_outside_done_is_not_proposed():
    """Same shape as the review case: the shared fixture's only abandoned-hint
    match happens to already be "done", so it cannot catch a search that was
    never actually scoped to that category."""
    statuses = [wizard.Status("To Do", "new"),
                wizard.Status("Duplicate", "indeterminate"),
                wizard.Status("Done", "done")]
    assert wizard.propose(statuses)["abandoned_status"] == ""


def test_abandoned_status_joins_multiple_matches_in_order():
    """Nothing else exercises more than one abandoned-looking status, so the
    comma-join itself is otherwise unverified."""
    statuses = [wizard.Status("Won't Do", "done"),
                wizard.Status("Rejected", "done"),
                wizard.Status("Done", "done")]
    assert wizard.propose(statuses)["abandoned_status"] == "Won't Do,Rejected"


def test_propose_on_nothing_returns_four_empty_strings():
    """The degradation path renders these into the form, so they have to be
    strings rather than None."""
    proposed = wizard.propose([])
    assert set(proposed) == {"status_order", "start_status", "review_status",
                             "abandoned_status"}
    assert all(v == "" for v in proposed.values())


def test_validate_no_longer_demands_the_workflow_fields():
    """Page one has no status fields to send. Whether a scope works against Jira
    has nothing to do with the workflow ordering, so validate stopped asking."""
    assert "status_order" not in wizard.REQUIRED_FOR_SCOPE
    assert "start_status" not in wizard.REQUIRED_FOR_SCOPE
    assert "status_order" in wizard.REQUIRED_FOR_DERIVE
    result = wizard.validate(_proposal(status_order="", start_status=""), "tok",
                             opener=_ok_opener(3))
    assert result.ok is True, result.problem


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
