import projects as projects_mod
import test_helpers

_registry = test_helpers.registry
_client = test_helpers.client
_synced = test_helpers.synced


def test_refresh_starts_a_job_and_redirects_back():
    registry = _registry()
    _synced(registry)
    started = {}

    def fake_start(project, jira_factory=None):
        started["slug"] = project.slug
        project.job.state = "running"
        return True

    original = projects_mod.start_refresh
    projects_mod.start_refresh = fake_start
    try:
        response = _client(registry).post("/alpha/refresh")
    finally:
        projects_mod.start_refresh = original
    assert response.status_code == 302
    assert "/alpha/" in response.headers["Location"]
    assert started["slug"] == "alpha"


def test_refresh_is_not_reachable_by_a_get():
    """It changes things, so a link or a prefetch must not trigger it."""
    registry = _registry()
    _synced(registry)
    assert _client(registry).get("/alpha/refresh").status_code == 405


def test_status_reports_the_job_state_as_json():
    registry = _registry()
    project = _synced(registry)
    project.job.state = "running"
    project.job.progress = "syncing"
    payload = _client(registry).get("/alpha/status").get_json()
    assert payload["state"] == "running"
    assert payload["progress"] == "syncing"


def test_a_refused_refresh_still_redirects_and_says_so():
    registry = _registry()
    _synced(registry)
    original = projects_mod.start_refresh
    projects_mod.start_refresh = lambda project, jira_factory=None: False
    try:
        response = _client(registry).post("/alpha/refresh", follow_redirects=True)
    finally:
        projects_mod.start_refresh = original
    assert response.status_code == 200
    # The exact sentence flags_from appends, not a short substring: "already"
    # alone also matches render.py's stylesheet comment ("the room the page
    # already has"), which is inlined into every report page regardless of
    # whether the refusal was ever reported.
    assert "A refresh is already running for this project." in response.get_data(as_text=True)


def test_status_on_an_unknown_project_is_404():
    assert _client(_registry()).get("/nope/status").status_code == 404


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
