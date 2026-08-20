import re

import test_helpers
import urd
import wizard as wizard_mod

# The network guard lives in test_helpers now (importing it protects every
# file, not just this one): `wizard.validate` ends in a real HTTP request
# built from urd.token()'s real credential, so any test anywhere that forgets
# to mock it would otherwise send that credential to whatever host the test
# data happens to name.

_SCOPE = {"site": "example.invalid", "email": "a@b.c", "project": "PROJ",
          "component": "TEAM", "since": "2026-01-01"}


def _found(names_and_categories):
    return wizard_mod.Discovery(
        [wizard_mod.Status(n, c) for n, c in names_and_categories])


def _patched(monkey, validate=None, discover=None):
    """Replaces both network calls. Every test here goes through the real route,
    so without this the route would reach urd.token() and a live request."""
    monkey["validate"] = wizard_mod.validate
    monkey["discover"] = wizard_mod.discover
    wizard_mod.validate = validate or (
        lambda p, t, opener=None: wizard_mod.Result(True, who="A Person", issues=751))
    wizard_mod.discover = discover or (
        lambda p, t, opener=None: _found([("To Do", "new"),
                                          ("In Progress", "indeterminate"),
                                          ("Done", "done")]))


def _restore(monkey):
    wizard_mod.validate = monkey["validate"]
    wizard_mod.discover = monkey["discover"]


def test_setup_offers_a_form_when_there_are_no_projects():
    body = test_helpers.client(test_helpers.registry()).get("/setup").get_data(as_text=True)
    assert "URD_TOKEN" in body, "the form must say where the token comes from"
    assert 'type="password"' not in body, "the token is never a form field"


def test_setup_validates_before_writing_anything():
    registry = test_helpers.registry()
    calls = {}

    def fake_validate(proposal, token, opener=None):
        calls["proposal"] = proposal
        return wizard_mod.Result(False, problem="could not authenticate: 401")

    original = wizard_mod.validate
    wizard_mod.validate = fake_validate
    try:
        response = test_helpers.client(registry).post("/setup", data={
            "slug": "alpha", "site": "example.atlassian.net", "email": "a@b.c",
            "project": "PROJ", "component": "TEAM", "since": "2026-01-01",
            "status_order": "To Do,Done", "start_status": "To Do",
            "review_status": "", "abandoned_status": "", "confirm": "",
        })
    finally:
        wizard_mod.validate = original
    body = response.get_data(as_text=True)
    assert "401" in body
    assert registry.get("alpha") is None, "a rejected scope must create no database"


def test_a_validated_scope_is_shown_for_confirmation_before_it_is_written():
    registry = test_helpers.registry()
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(registry).post("/setup", data={
            "site": "example.atlassian.net", "email": "a@b.c",
            "project": "PROJ", "component": "TEAM", "since": "2026-01-01",
        }).get_data(as_text=True)
    finally:
        _restore(monkey)
    assert "A Person" in body
    assert "751" in body
    assert 'name="confirm"' in body
    assert registry.projects() == [], "nothing is written before confirm"


def test_confirming_writes_the_scope_and_redirects():
    registry = test_helpers.registry()
    original = wizard_mod.validate
    wizard_mod.validate = lambda p, t, opener=None: wizard_mod.Result(
        True, who="A Person", issues=751)
    try:
        response = test_helpers.client(registry).post("/setup", data={
            "slug": "alpha", "site": "example.atlassian.net", "email": "a@b.c",
            "project": "PROJ", "component": "TEAM", "since": "2026-01-01",
            "status_order": "To Do,Done", "start_status": "To Do",
            "review_status": "", "abandoned_status": "", "confirm": "yes",
        })
    finally:
        wizard_mod.validate = original
    assert response.status_code == 302
    assert "/alpha/" in response.headers["Location"]
    assert registry.get("alpha").configured() is True


def test_a_bad_slug_is_refused_with_a_message():
    # validate must be mocked to ok=True: unmocked, the route never reaches
    # registry.add at all (it returns earlier on the real credential/scope
    # check), so this test would pass without ever exercising the bad-slug
    # path it names. See ValueError's message in projects.py: add().
    registry = test_helpers.registry()
    original = wizard_mod.validate
    wizard_mod.validate = lambda p, t, opener=None: wizard_mod.Result(
        True, who="A Person", issues=1)
    try:
        body = test_helpers.client(registry).post("/setup", data={
            "slug": "../escape", "site": "example.atlassian.net", "email": "a@b.c",
            "project": "PROJ", "component": "", "since": "2026-01-01",
            "status_order": "To Do,Done", "start_status": "To Do",
            "review_status": "", "abandoned_status": "", "confirm": "yes",
        }).get_data(as_text=True)
    finally:
        wizard_mod.validate = original
    # The exact wording ValueError carries, not "slug", which every field
    # label on this page also contains and would satisfy by accident.
    assert "not a usable project slug" in body
    assert "../escape" in body
    # Row 5 of the discovery-state table: no discovery ran for this re-render
    # (found is None), so the page must not claim it did.
    assert "Status order is every status this project uses" not in body


def test_setup_still_adds_projects_once_one_exists():
    """Adding a second project is the main reason this page exists."""
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    assert test_helpers.client(registry).get("/setup").status_code == 200


def test_the_setup_form_is_laid_out_as_stacked_fields():
    """Without a rule for it a label is inline, so ten label-and-input pairs flow
    as one wrapping paragraph with each label butting against the previous input,
    and the inputs keep the browser's white default on a dark page. This is the
    first screen anyone sees, so the layout is part of the page working."""
    body = test_helpers.client(test_helpers.registry()).get(
        "/setup").get_data(as_text=True)
    assert re.search(r"\blabel\s*\{[^}]*display:\s*block", body), "labels are inline"
    assert re.search(r"label input\s*\{[^}]*background:\s*var\(--surface\)", body), \
        "inputs are not themed"


def test_page_one_asks_only_what_the_operator_knows():
    body = test_helpers.client(test_helpers.registry()).get(
        "/setup").get_data(as_text=True)
    for field in ("site", "email", "project", "component", "since"):
        assert f'name="{field}"' in body, field
    for field in ("slug", "status_order", "start_status", "review_status",
                  "abandoned_status"):
        assert f'name="{field}"' not in body, f"{field} should be on page two"


def test_page_two_prefills_the_workflow_fields_from_what_was_found():
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    assert 'value="To Do,In Progress,Done"' in body
    assert 'value="In Progress"' in body
    assert 'name="confirm"' in body


def test_page_two_prefills_the_slug_from_the_project_key():
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    assert f'value="{urd.project_slug("PROJ")}"' in body


def test_page_two_writes_nothing_until_confirmed():
    registry = test_helpers.registry()
    monkey = {}
    _patched(monkey)
    try:
        test_helpers.client(registry).post("/setup", data=_SCOPE)
    finally:
        _restore(monkey)
    assert registry.projects() == [], "page two must not create a database"


def test_confirming_writes_the_scope_including_the_discovered_workflow():
    registry = test_helpers.registry()
    monkey = {}
    _patched(monkey)
    try:
        response = test_helpers.client(registry).post("/setup", data={
            **_SCOPE, "slug": "proj", "status_order": "To Do,In Progress,Done",
            "start_status": "In Progress", "review_status": "",
            "abandoned_status": "", "confirm": "yes"})
    finally:
        _restore(monkey)
    assert response.status_code == 302
    project = registry.get("proj")
    assert project is not None
    assert urd.load_scope(project.con)["status_order"] == "To Do,In Progress,Done"


def test_a_failed_discovery_still_reaches_page_two():
    """A 403 on a restricted project costs the prefill, not the setup."""
    monkey = {}
    _patched(monkey, discover=lambda p, t, opener=None: wizard_mod.Discovery(
        [], "could not read the workflow's statuses: 403"))
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    # render.esc HTML-escapes the apostrophe in this message (quote=True), so
    # the raw text never appears in the body; assert what actually lands.
    assert "could not read the workflow&#x27;s statuses: 403" in body
    assert 'name="status_order"' in body, "the field must still be there to type into"
    assert 'name="confirm"' in body, "setup must still be completable"


def test_confirming_without_a_status_order_is_refused_with_a_message():
    """derive refuses without it, and a project that cannot derive dead-ends on
    a page whose only action repeats the failure."""
    registry = test_helpers.registry()
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(registry).post("/setup", data={
            **_SCOPE, "slug": "proj", "status_order": "", "start_status": "",
            "review_status": "", "abandoned_status": "",
            "confirm": "yes"}).get_data(as_text=True)
    finally:
        _restore(monkey)
    # The exact message submit() builds from REQUIRED_FOR_DERIVE, not a substring
    # ("status order" also appears in the page's own explanatory prose and would
    # pass even if the missing-field check were removed).
    assert "missing: status order, start status" in body
    assert registry.projects() == []
    # Row 4 of the discovery-state table: this is a re-render after a 403 lost
    # the hint, and confirming with the field left empty must not make the
    # page start claiming discovery worked and forget the 403 ever happened.
    assert "Status order is every status this project uses" not in body
    assert 'name="status_order"' in body, "the field must still be there to type into"
    assert 'name="confirm"' in body, "setup must still be completable"


def test_page_two_says_which_values_are_guesses():
    """A guess presented as an answer is worse than a blank field, and the
    ordering within a category is genuinely not known here."""
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    # The two exact sentences _workflow_page prints, not just the words "guess"
    # and "derive", which the page's other prose (the token notice, the problem
    # message) could also satisfy on its own.
    assert ("Start, review and abandoned status are guesses from the names; "
            "check them.") in body
    assert ("so derive prints a better order to the terminal running urd "
            "serve after the first sync.") in body


def test_discovery_finding_nothing_does_not_claim_it_worked():
    """Row 3 of the discovery-state table: a real discovery call that returns
    no statuses and no error is the same story as a failed lookup, since there
    is equally nothing to show. It must not fall into the "worked" branch just
    because found.problem happens to be empty."""
    monkey = {}
    _patched(monkey, discover=lambda p, t, opener=None: wizard_mod.Discovery([]))
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    assert "Status order is every status this project uses" not in body
    assert "Type the workflow fields yourself" in body
    assert 'name="confirm"' in body


def test_confirming_a_slug_already_in_use_does_not_rescope_it():
    """registry.add returns the existing project for a slug already present
    rather than refusing, so confirming a second setup with the same
    prefilled slug used to silently repoint that project's database at a
    different scope, and the redirect looked like success either way. Assert
    on the stored scope, not the response code."""
    registry = test_helpers.registry()
    monkey = {}
    _patched(monkey)
    try:
        first = test_helpers.client(registry).post("/setup", data={
            "site": "one.invalid", "email": "a@b.c", "project": "PROJ",
            "component": "TEAM-A", "since": "2026-01-01", "slug": "proj",
            "status_order": "To Do,Done", "start_status": "To Do",
            "review_status": "", "abandoned_status": "", "confirm": "yes"})
        assert first.status_code == 302
        original_scope = urd.load_scope(registry.get("proj").con)

        second = test_helpers.client(registry).post("/setup", data={
            "site": "two.invalid", "email": "x@y.z", "project": "OTHER",
            "component": "TEAM-B", "since": "2020-01-01", "slug": "proj",
            "status_order": "To Do,Done", "start_status": "To Do",
            "review_status": "", "abandoned_status": "", "confirm": "yes"})
    finally:
        _restore(monkey)
    body = second.get_data(as_text=True)
    assert len(registry.projects()) == 1
    assert urd.load_scope(registry.get("proj").con) == original_scope, \
        "the second confirm must not change the first project's stored scope"
    assert "already in use" in body


def test_the_guessed_fields_are_marked_in_the_form():
    """The caveat naming start, review and abandoned status as guesses is easy
    to miss two paragraphs up; the label the operator is actually looking at
    should say so too."""
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    for field in ("start status", "review status", "abandoned status"):
        assert f"{field} (guess)" in body, field
    assert "status order (guess)" not in body, "status order is derived, not guessed"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
