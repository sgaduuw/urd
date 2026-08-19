import projects as projects_mod
import test_helpers


def test_refresh_starts_a_job_and_redirects_back():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    started = {}

    def fake_start(project, jira_factory=None):
        started["slug"] = project.slug
        project.job.state = "running"
        return True

    original = projects_mod.start_refresh
    projects_mod.start_refresh = fake_start
    try:
        response = test_helpers.client(registry).post("/alpha/refresh")
    finally:
        projects_mod.start_refresh = original
    assert response.status_code == 302
    assert "/alpha/" in response.headers["Location"]
    assert started["slug"] == "alpha"


def test_refresh_is_not_reachable_by_a_get():
    """It changes things, so a link or a prefetch must not trigger it."""
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    assert test_helpers.client(registry).get("/alpha/refresh").status_code == 405


def test_refresh_always_redirects_back_regardless_of_whether_it_started():
    """The route no longer encodes the reason into the URL: whichever of the
    three ways start_refresh can refuse applies, the reason comes off
    project.job the next time the report renders (see test_views_report.py's
    job-state tests and test_projects.py's three refusal-reason tests), not a
    query-string marker that meant "already running" two times out of three."""
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    original = projects_mod.start_refresh
    projects_mod.start_refresh = lambda project, jira_factory=None: False
    try:
        response = test_helpers.client(registry).post("/alpha/refresh")
    finally:
        projects_mod.start_refresh = original
    assert response.status_code == 302
    assert response.headers["Location"] == "/alpha/"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
