import test_helpers
import wizard as wizard_mod

# The network guard lives in test_helpers now (importing it protects every
# file, not just this one): `wizard.validate` ends in a real HTTP request
# built from urd.token()'s real credential, so any test anywhere that forgets
# to mock it would otherwise send that credential to whatever host the test
# data happens to name.


def test_setup_offers_a_form_when_there_are_no_projects():
    body = test_helpers.client(test_helpers.registry()).get("/setup").get_data(as_text=True)
    for field in ("slug", "site", "email", "project", "since", "status_order",
                  "start_status"):
        assert f'name="{field}"' in body, field
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
    original = wizard_mod.validate
    wizard_mod.validate = lambda p, t, opener=None: wizard_mod.Result(
        True, who="A Person", issues=751)
    try:
        body = test_helpers.client(registry).post("/setup", data={
            "slug": "alpha", "site": "example.atlassian.net", "email": "a@b.c",
            "project": "PROJ", "component": "TEAM", "since": "2026-01-01",
            "status_order": "To Do,Done", "start_status": "To Do",
            "review_status": "", "abandoned_status": "",
        }).get_data(as_text=True)
    finally:
        wizard_mod.validate = original
    assert "A Person" in body
    assert "751" in body
    assert 'name="confirm"' in body
    assert registry.get("alpha") is None, "nothing is written before confirm"


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


def test_setup_still_adds_projects_once_one_exists():
    """Adding a second project is the main reason this page exists."""
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    assert test_helpers.client(registry).get("/setup").status_code == 200


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
