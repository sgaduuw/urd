import test_helpers
import urd


def test_a_since_parameter_changes_the_page():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    client = test_helpers.client(registry)
    whole = client.get("/alpha/").get_data(as_text=True)
    windowed = client.get("/alpha/?since=2030-01-01").get_data(as_text=True)
    assert whole != windowed
    assert "2030-01-01 onward" in windowed


def test_a_parameter_does_not_change_the_stored_default():
    """Two browser tabs must not fight over each other's window."""
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    before = urd.load_scope(project.con)["report_since"]
    test_helpers.client(registry).get("/alpha/?since=2030-01-01")
    assert urd.load_scope(project.con)["report_since"] == before


def test_a_bad_parameter_is_reported_and_the_page_still_renders():
    """The validators exit on bad input, which would be a 500 through a route."""
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    response = test_helpers.client(registry).get("/alpha/?since=yesterday")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "yesterday" in body
    assert "flow report" in body, "the page should still render with the default"


def test_the_page_offers_controls_for_every_flag():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    body = test_helpers.client(registry).get("/alpha/").get_data(as_text=True)
    for control in ("since", "exclude_epic", "threshold"):
        assert f'name="{control}"' in body, control
    assert 'method="get"' in body


def test_the_page_lists_the_other_projects():
    registry = test_helpers.registry()
    test_helpers.synced(registry, "alpha")
    test_helpers.synced(registry, "beta")
    body = test_helpers.client(registry).get("/alpha/").get_data(as_text=True)
    assert 'href="/beta/"' in body


def test_no_projects_redirects_to_setup():
    """Moved here from Task 6: `/` is this blueprint's route, so its redirect
    behaviour is tested where it lives."""
    response = test_helpers.client(test_helpers.registry()).get("/")
    assert response.status_code == 302
    assert "/setup" in response.headers["Location"]


def test_a_configured_project_is_redirected_to_from_the_root():
    registry = test_helpers.registry()
    test_helpers.synced(registry, "alpha")
    response = test_helpers.client(registry).get("/")
    assert response.status_code == 302
    assert "/alpha/" in response.headers["Location"]


def test_a_running_refresh_says_so_on_the_page():
    """flags_from reads project.job.state directly rather than a query-string
    marker, so this is true regardless of how the request arrived here, not
    only right after the refresh route's own redirect."""
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    project.job.state = "running"
    body = test_helpers.client(registry).get("/alpha/").get_data(as_text=True)
    assert "A refresh is already running for this project." in body


def test_a_failed_refresh_shows_its_message_on_the_page():
    """The bug this fixes: a refresh that fails on the background thread (a
    bad token, Jira down) used to leave job.message sitting on the project
    with nothing anywhere that ever read it."""
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    project.job.state = "failed"
    project.job.message = "no API token"
    body = test_helpers.client(registry).get("/alpha/").get_data(as_text=True)
    assert "no API token" in body


def test_a_never_synced_project_gets_no_duplicate_controls():
    """project_page's own notice already offers a Refresh button; splicing the
    controls form on top doubled it and added a since/exclude box
    that does nothing without a report to apply it to."""
    registry = test_helpers.registry()
    project = registry.add("alpha")
    urd.save_scope(project.con, site=test_helpers.SITE, email=test_helpers.EMAIL,
                   project="PROJ", earliest_since="2026-01-01")
    body = test_helpers.client(registry).get("/alpha/").get_data(as_text=True)
    assert body.count('action="/alpha/refresh"') == 1
    assert 'name="since"' not in body


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
