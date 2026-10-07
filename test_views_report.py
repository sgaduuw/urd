import re

import charts as chart_specs
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


def _with_components(project, **by_key):
    """Tickets straight into issues_all: synced() derives an empty mirror, and
    what these tests need is components, not a history."""
    for key, names in by_key.items():
        project.con.execute(
            "INSERT INTO issues_all (key, project, type, summary, status, "
            "status_category, created, components) VALUES "
            "(?, 'PROJ', 'Task', ?, 'Done', 'done', '2026-01-02', ?)",
            [key, f"a {key} ticket", list(names)])
    return project


def test_the_page_offers_a_checkbox_for_every_component_in_the_mirror():
    registry = test_helpers.registry()
    _with_components(test_helpers.synced(registry),
                     **{"PROJ-1": ["TEAM", "OTHER"], "PROJ-2": ["TEAM"]})
    body = test_helpers.client(registry).get("/alpha/").get_data(as_text=True)
    assert 'name="component" value="TEAM"' in body
    assert 'name="component" value="OTHER"' in body


def test_a_component_parameter_filters_the_page():
    registry = test_helpers.registry()
    _with_components(test_helpers.synced(registry),
                     **{"PROJ-1": ["TEAM", "OTHER"], "PROJ-2": ["TEAM"]})
    client = test_helpers.client(registry)
    whole = client.get("/alpha/").get_data(as_text=True)
    sliced = client.get("/alpha/?component=OTHER").get_data(as_text=True)
    assert "2 tickets" in whole
    assert "1 tickets" in sliced
    assert "Showing component OTHER" in sliced
    assert 'value="OTHER" checked' in sliced


def test_a_component_parameter_does_not_change_the_stored_default():
    """Same reason as the window: two browser tabs must not fight over each
    other\'s slice, and the CLI stays the way a default is changed."""
    registry = test_helpers.registry()
    project = _with_components(test_helpers.synced(registry), **{"PROJ-1": ["TEAM"]})
    test_helpers.client(registry).get("/alpha/?component=TEAM")
    assert urd.load_scope(project.con)["report_components"] is None


def test_the_checkboxes_survive_their_own_filter():
    registry = test_helpers.registry()
    _with_components(test_helpers.synced(registry),
                     **{"PROJ-1": ["TEAM", "OTHER"], "PROJ-2": ["TEAM"]})
    body = test_helpers.client(registry).get(
        "/alpha/?component=OTHER").get_data(as_text=True)
    assert 'value="TEAM"' in body, "no way back to the other slice"


def test_a_mirror_with_no_components_offers_no_checkboxes():
    """Nothing to choose between, so the control is not drawn at all."""
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    body = test_helpers.client(registry).get("/alpha/").get_data(as_text=True)
    # The markup, not the word: the stylesheet names the class too.
    assert 'class="boxes"' not in body, "an empty control still asks to be understood"


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


def test_each_tab_renders_only_its_own_section():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    browser = test_helpers.client(registry)
    seen = []
    real = urd.run_chart

    def recording(con, chart, tiers=None):
        seen.append(chart.section)
        return real(con, chart, tiers)

    urd.run_chart = recording
    try:
        for slug, title in urd.SECTION_TABS:
            seen.clear()
            response = browser.get(f"/alpha/?section={slug}")
            assert response.status_code == 200, slug
            assert f"<h2>{title}</h2>" in response.get_data(as_text=True), slug
            if slug != "capacity":
                assert seen and set(seen) == {title}, (slug, set(seen))
    finally:
        urd.run_chart = real


def test_tab_titles_match_the_chart_sections():
    chart_tabs = [title for slug, title in urd.SECTION_TABS if slug != "capacity"]
    assert tuple(chart_tabs) == chart_specs.SECTIONS


def test_the_page_contains_exactly_the_fragment():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    browser = test_helpers.client(registry)
    for slug, _ in urd.SECTION_TABS:
        query = f"since=2026-01-01&section={slug}"
        page = browser.get(f"/alpha/?{query}").get_data(as_text=True)
        fragment = browser.get(f"/alpha/sections/{slug}?since=2026-01-01")
        assert fragment.status_code == 200
        assert f'<div id="report">{fragment.get_data(as_text=True)}</div>' in page, slug


def test_unknown_sections_fall_back_on_the_page_and_404_as_a_fragment():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    browser = test_helpers.client(registry)
    page = browser.get("/alpha/?section=nope").get_data(as_text=True)
    assert "<h2>Attention today</h2>" in page
    assert browser.get("/alpha/sections/nope").status_code == 404


def test_tabs_carry_every_filter_value():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    browser = test_helpers.client(registry)
    page = browser.get("/alpha/?component=A&component=B&since=2026-01-01&section=flow")
    body = page.get_data(as_text=True)
    tab = re.search(r'<a href="([^"]*section=commitments[^"]*)"', body).group(1)
    assert "component=A&amp;component=B" in tab, tab
    assert "since=2026-01-01" in tab, tab
    fragment = re.search(r'hx-get="([^"]*sections/commitments[^"]*)"', body).group(1)
    assert "component=A&amp;component=B" in fragment and "section=" not in fragment, fragment
    assert 'aria-current="page"' in re.search(r"<a [^>]*section=flow[^>]*>", body).group(0)


def test_history_restore_gets_the_full_page():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    response = test_helpers.client(registry).get(
        "/alpha/?section=flow", headers={"HX-History-Restore-Request": "true"})
    assert response.get_data(as_text=True).startswith("<!doctype html>")


def test_the_controls_keep_the_selected_tab():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    body = test_helpers.client(registry).get("/alpha/?section=flow").get_data(as_text=True)
    assert '<input type="hidden" name="section" value="flow">' in body


def test_a_section_fragment_does_not_change_the_stored_default():
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    before = urd.load_scope(project.con)["report_since"]
    test_helpers.client(registry).get("/alpha/sections/flow?since=2030-01-01")
    assert urd.load_scope(project.con)["report_since"] == before


def test_save_as_default_persists_every_filter():
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    response = test_helpers.client(registry).post("/alpha/defaults", data={
        "since": "2026-02-01", "exclude_epic": "PROJ-9", "component": "TEAM",
        "threshold": "default=0.5", "section": "flow"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/alpha/?section=flow")
    scope = urd.load_scope(project.con)
    assert scope["report_since"] == "2026-02-01"
    assert urd.stored_excluded_epics(project.con) == ["PROJ-9"]
    assert urd.stored_report_components(project.con) == ["TEAM"]
    assert urd.stored_thresholds(project.con)["default"] == 0.5


def test_save_as_default_with_an_invalid_value_saves_nothing():
    registry = test_helpers.registry()
    project = test_helpers.synced(registry)
    before = urd.load_scope(project.con)
    response = test_helpers.client(registry).post("/alpha/defaults", data={
        "since": "yesterday", "component": "TEAM", "threshold": "default=0.5"})
    assert response.status_code == 302
    assert "since=yesterday" in response.headers["Location"]
    assert urd.load_scope(project.con) == before


def test_the_controls_offer_save_as_default():
    registry = test_helpers.registry()
    test_helpers.synced(registry)
    body = test_helpers.client(registry).get("/alpha/").get_data(as_text=True)
    assert 'formaction="/alpha/defaults"' in body and 'formmethod="post"' in body


def test_save_as_default_with_every_box_unticked_clears_the_components():
    registry = test_helpers.registry()
    project = _with_components(test_helpers.synced(registry), **{"PROJ-1": ["TEAM"]})
    client = test_helpers.client(registry)
    client.post("/alpha/defaults", data={"component": "TEAM"})
    assert urd.stored_report_components(project.con) == ["TEAM"]
    # What the form sends once the hidden empty input is in it and no box is ticked.
    client.post("/alpha/defaults", data={"component": ""})
    assert urd.stored_report_components(project.con) == []
    body = client.get("/alpha/").get_data(as_text=True)
    assert '<input type="hidden" name="component" value="">' in body


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
