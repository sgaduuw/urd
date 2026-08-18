import test_helpers
import urd

_registry = test_helpers.registry
_client = test_helpers.client
_synced = test_helpers.synced


def test_a_since_parameter_changes_the_page():
    registry = _registry()
    _synced(registry)
    client = _client(registry)
    whole = client.get("/alpha/").get_data(as_text=True)
    windowed = client.get("/alpha/?since=2030-01-01").get_data(as_text=True)
    assert whole != windowed
    assert "2030-01-01 onward" in windowed


def test_a_parameter_does_not_change_the_stored_default():
    """Two browser tabs must not fight over each other's window."""
    registry = _registry()
    project = _synced(registry)
    before = urd.load_scope(project.con)["report_since"]
    _client(registry).get("/alpha/?since=2030-01-01")
    assert urd.load_scope(project.con)["report_since"] == before


def test_a_bad_parameter_is_reported_and_the_page_still_renders():
    """The validators exit on bad input, which would be a 500 through a route."""
    registry = _registry()
    _synced(registry)
    response = _client(registry).get("/alpha/?since=yesterday")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "yesterday" in body
    assert "flow report" in body, "the page should still render with the default"


def test_min_closed_zero_is_rejected_not_ignored():
    """0 is falsy, and the CLI had exactly this bug: `or` sent it to the stored
    default and the validation never ran."""
    registry = _registry()
    _synced(registry)
    body = _client(registry).get("/alpha/?min_closed=0").get_data(as_text=True)
    assert "min-closed" in body or "min_closed" in body


def test_the_page_offers_controls_for_every_flag():
    registry = _registry()
    _synced(registry)
    body = _client(registry).get("/alpha/").get_data(as_text=True)
    for control in ("since", "min_closed", "exclude_epic", "threshold"):
        assert f'name="{control}"' in body, control
    assert 'method="get"' in body


def test_the_page_lists_the_other_projects():
    registry = _registry()
    _synced(registry, "alpha")
    _synced(registry, "beta")
    body = _client(registry).get("/alpha/").get_data(as_text=True)
    assert 'href="/beta/"' in body


def test_no_projects_redirects_to_setup():
    """Moved here from Task 6: `/` is this blueprint's route, so its redirect
    behaviour is tested where it lives."""
    response = _client(_registry()).get("/")
    assert response.status_code == 302
    assert "/setup" in response.headers["Location"]


def test_a_configured_project_is_redirected_to_from_the_root():
    registry = _registry()
    _synced(registry, "alpha")
    response = _client(registry).get("/")
    assert response.status_code == 302
    assert "/alpha/" in response.headers["Location"]


def test_a_refused_refresh_marker_is_surfaced():
    """Task 8's refresh route redirects here with ?refused=1 when a sync is already
    running. Handled here because this task owns this file, and testable without
    that route existing."""
    registry = _registry()
    _synced(registry)
    body = _client(registry).get("/alpha/?refused=1").get_data(as_text=True)
    assert "already running" in body.lower()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
