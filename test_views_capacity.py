"""Native form flows for local capacity planning."""
import json
from html.parser import HTMLParser

import capacity
import views_capacity
from test_capacity import team
from test_helpers import client, registry, synced


class Form(HTMLParser):
    def __init__(self, html, identity="plan"):
        super().__init__()
        self.identity, self.active, self.values = identity, False, {}
        self.select = None
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "form":
            self.active = attrs.get("id") == self.identity
        if self.active and tag == "select":
            self.select = attrs.get("name")
        if (self.select and tag == "option"
                and (self.select not in self.values or "selected" in attrs)):
            self.values[self.select] = attrs.get("value", "")
        if (self.active and tag == "input" and attrs.get("name") and "disabled" not in attrs
                and (attrs.get("type") not in ("checkbox", "radio") or "checked" in attrs)):
            name, value = attrs["name"], attrs.get("value", "")
            if name in self.values:
                previous = self.values[name]
                self.values[name] = (previous if isinstance(previous, list) else [previous])
                self.values[name].append(value)
            else:
                self.values[name] = value

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None
        if tag == "form":
            self.active = False


def setup():
    reg = registry()
    project = synced(reg)
    saved_team = capacity.save_team(project.con, team())
    capacity.cache_sprint(project.con, {"id": 11, "name": "Empty future", "state": "future",
                                       "boards": [1]})
    return project, client(reg), saved_team


def plan_form(browser, path):
    response = browser.get(path + "?start=2026-10-05&end=2026-10-08")
    assert response.status_code == 200, response.get_data(as_text=True)
    form = Form(response.get_data(as_text=True)).values
    assert "payload" in form, response.get_data(as_text=True)
    form.update({"focus": "75", "rate_mode": "keep"})
    return form


def test_plan_confirm_revise_void_and_conflict():
    project, browser, saved_team = setup()
    path = f"/alpha/capacity/plan/{saved_team['id']}/11"
    assert browser.get("/alpha/capacity/").status_code == 200
    assert "Capacity" in browser.get("/alpha/").get_data(as_text=True)
    form = plan_form(browser, path)
    form["action"] = "confirm"
    response = browser.post(path, data=form, follow_redirects=True)
    assert response.status_code == 200, response.get_data(as_text=True)
    saved = capacity.get_plan(project.con, saved_team["id"], 11)
    assert saved["confirmation"] == 1
    assert saved["totals"]["focus_hours"] == 31.5
    original = capacity.get_plan(project.con, saved_team["id"], 11, 1)
    form = Form(response.get_data(as_text=True)).values
    form.update({"action": "save", "reason": "Availability adjusted",
                 "work:person-a:2026-10-05": "3", "rate_mode": "keep"})
    response = browser.post(path, data=form, follow_redirects=True)
    assert response.status_code == 200
    assert capacity.get_plan(project.con, saved_team["id"], 11)["totals"]["focus_hours"] == 28.5
    form["work:person-a:2026-10-05"] = "3.25"
    form["meetings:person-a:2026-10-05"] = "1.250"
    response = browser.post(path, data=form)
    assert response.status_code == 409
    retained = Form(response.get_data(as_text=True)).values
    assert retained["work:person-a:2026-10-05"] == "3.25"
    assert retained["meetings:person-a:2026-10-05"] == "1.250"
    current = Form(browser.get(path).get_data(as_text=True)).values
    current.update(action="void", reason="Incorrect commitment")
    response = browser.post(path, data=current, follow_redirects=True)
    assert response.status_code == 200
    assert capacity.get_plan(project.con, saved_team["id"], 11)["confirmation"] is None
    current = Form(response.get_data(as_text=True)).values
    current.update(action="confirm", rate_mode="keep")
    response = browser.post(path, data=current, follow_redirects=True)
    assert response.status_code == 200
    assert capacity.get_plan(project.con, saved_team["id"], 11)["confirmation"] == 4
    assert capacity.get_plan(project.con, saved_team["id"], 11, 1) == original
    project.con.close()


def _post(browser, path, form, **changes):
    response = browser.post(path, data=dict(form, **changes), follow_redirects=True)
    assert response.status_code == 200, response.get_data(as_text=True)
    return Form(response.get_data(as_text=True)).values


def test_second_replacement_links_the_latest_voided_confirmation():
    project, browser, saved_team = setup()
    path = f"/alpha/capacity/plan/{saved_team['id']}/11"
    form = _post(browser, path, plan_form(browser, path), action="confirm")
    form = _post(browser, path, form, action="void", reason="Wrong scope")
    form = _post(browser, path, form, action="confirm")
    third = capacity.get_plan(project.con, saved_team["id"], 11)
    assert third["confirmation"] == 3 and third["replaces"]["confirmation"] == 1
    form = _post(browser, path, form, action="void", reason="Still wrong")
    _post(browser, path, form, action="confirm")
    fifth = capacity.get_plan(project.con, saved_team["id"], 11)
    project.con.close()
    assert fifth["confirmation"] == 5, fifth["confirmation"]
    assert fifth["replaces"]["confirmation"] == 3, fifth["replaces"]


def test_grid_escapes_day_keys_when_dates_are_invalid():
    value = {"dates": {"start": "", "end": ""}, "members": [{
        "id": "person-a", "name": "Aster", "daily": {"<i>day</i>": {"work": 1, "meetings": 0}}}]}
    html = views_capacity._grid(value)
    assert "&lt;i&gt;day" in html and "<i>" not in html, html


def test_changed_preview_busy_refresh_and_invalid_inputs_preserve_form():
    project, browser, saved_team = setup()
    path = f"/alpha/capacity/plan/{saved_team['id']}/11"
    form = plan_form(browser, path)
    form.update(action="confirm")
    project.con.execute("INSERT INTO fields VALUES ('newfield', 'New metadata', NULL)")
    response = browser.post(path, data=form)
    assert response.status_code == 409
    assert "preview" in response.get_data(as_text=True).lower()
    assert capacity.get_plan(project.con, saved_team["id"], 11) is None
    form = plan_form(browser, path)
    form.update(action="save", focus="63.25")
    project.lock.acquire()
    try:
        response = browser.post(path, data=form)
        assert response.status_code == 409
        assert Form(response.get_data(as_text=True)).values["focus"] == "63.25"
    finally:
        project.lock.release()
    form["focus"] = "nan"
    response = browser.post(path, data=form)
    assert response.status_code == 400
    assert Form(response.get_data(as_text=True)).values["focus"] == "nan"
    assert capacity.get_plan(project.con, saved_team["id"], 11) is None
    project.con.close()


def test_team_forms_security_and_undated_sprint():
    project, browser, saved_team = setup()
    response = browser.get("/alpha/capacity/teams/new")
    assert response.status_code == 200
    form = Form(response.get_data(as_text=True), "team").values
    form.update(name="West <safe>", components="Engine", timezone="UTC",
                action="add_member", new_name="Cedar")
    response = browser.post("/alpha/capacity/teams/new", data=form)
    assert response.status_code == 200
    form = Form(response.get_data(as_text=True), "team").values
    payload = json.loads(form["payload"])
    member = payload["members"][0]["id"]
    form.update({f"weekly:work:{member}:{d}": "4" for d in range(7)})
    form.update({f"weekly:meetings:{member}:{d}": "1" for d in range(7)})
    form.update(action="save", name="West <safe>", components="Engine", timezone="UTC")
    response = browser.post("/alpha/capacity/teams/new", data=form, follow_redirects=True)
    assert response.status_code == 200
    assert "West &lt;safe&gt;" in response.get_data(as_text=True)
    assert "West <safe>" not in response.get_data(as_text=True)
    west = next(t for t in capacity.teams(project.con) if t["name"] == "West <safe>")
    assert west["members"][0]["weekly"][0] == {"work": 4, "meetings": 1}
    path = f"/alpha/capacity/plan/{saved_team['id']}/11"
    response = browser.get(path)
    assert "planning dates" in response.get_data(as_text=True).lower()
    assert browser.post(path, data={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert browser.get(path, headers={"Host": "collector.invalid"}).status_code == 403
    project.con.close()



def test_date_reconciliation_and_invalid_dates_keep_hours():
    project, browser, saved_team = setup()
    path = f"/alpha/capacity/plan/{saved_team['id']}/11"
    form = plan_form(browser, path)
    form.update(action="save")
    assert browser.post(path, data=form).status_code == 302
    capacity.cache_sprint(project.con, {"id": 11, "name": "Dated", "state": "future",
                                       "boards": [1], "startDate": "2026-10-06T09:00:00Z",
                                       "endDate": "2026-10-08T17:00:00Z"})
    form = Form(browser.get(path).get_data(as_text=True)).values
    form.update(action="save", rate_mode="keep")
    assert browser.post(path, data=form).status_code == 400
    form.update(action="preview", start="2026-10-08", end="2026-10-06")
    response = browser.post(path, data=form)
    assert response.status_code == 400
    invalid = Form(response.get_data(as_text=True)).values
    assert float(invalid["work:person-a:2026-10-05"]) == 7
    assert float(invalid["meetings:person-a:2026-10-05"]) == 1
    form.update(action="save", start="2026-10-06", end="2026-10-08", reconcile_dates="yes")
    response = browser.post(path, data=form)
    assert response.status_code == 302
    saved = capacity.get_plan(project.con, saved_team["id"], 11)
    cell = saved["members"][0]["daily"]["2026-10-05"]
    assert (float(cell["work"]), float(cell["meetings"])) == (7, 1)
    outside = browser.get(path).get_data(as_text=True)
    assert "Retained hours outside" in outside and "2026-10-05" in outside
    form = Form(outside).values
    form.update(action="preview", start="2026-10-05")
    restored = Form(browser.post(path, data=form).get_data(as_text=True)).values
    assert float(restored["work:person-a:2026-10-05"]) == 7
    assert float(restored["meetings:person-a:2026-10-05"]) == 1
    assert saved["totals"]["available_hours"] == 36
    project.con.close()


def test_replacement_cannot_overwrite_valid_destination():
    project, browser, saved_team = setup()
    path = f"/alpha/capacity/plan/{saved_team['id']}/11"
    form = plan_form(browser, path)
    form.update(action="confirm")
    assert browser.post(path, data=form).status_code == 302
    form = Form(browser.get(path).get_data(as_text=True)).values
    form.update(action="void", reason="Wrong team")
    assert browser.post(path, data=form).status_code == 302
    other = capacity.save_team(project.con, team("West"))
    replace = {"source_team": saved_team["id"], "source_sprint": "11", "confirmation": "1",
               "team_id": other["id"], "sprint_id": "11"}
    response = browser.get("/alpha/capacity/replace", query_string=replace)
    assert response.status_code == 302
    target = response.headers["Location"]
    response = browser.get(target + "&start=2026-10-05&end=2026-10-08")
    form = Form(response.get_data(as_text=True)).values
    form.update(action="confirm", focus="75")
    target_path = target.split("?")[0]
    assert browser.post(target_path, data=form).status_code == 302
    saved = capacity.get_plan(project.con, other["id"], 11)
    assert saved["replaces"]["team_id"] == saved_team["id"]
    assert browser.get("/alpha/capacity/replace", query_string=replace).status_code == 409
    assert capacity.get_plan(project.con, other["id"], 11) == saved
    project.con.close()


def test_daily_grid_and_complete_split_before_confirming():
    project, browser, saved_team = setup()
    path = f"/alpha/capacity/plan/{saved_team['id']}/11"
    fields = plan_form(browser, path)
    assert fields.get("work:person-a:2026-10-05") == "7.0", fields
    assert fields.get("meetings:person-a:2026-10-05") == "1.0", fields
    assert "meetings:person-a" not in fields
    for missing in ("work", "meetings"):
        fields[f"{missing}:person-a:2026-10-05"] = ""
        fields.update(action="confirm")
        response = browser.post(path, data=fields)
        assert response.status_code == 400
        assert capacity.get_plan(project.con, saved_team["id"], 11) is None
        fields = plan_form(browser, path)
    fields.update(action="confirm", **{"work:person-a:2026-10-05": "23",
                                      "meetings:person-a:2026-10-05": "2"})
    response = browser.post(path, data=fields)
    assert response.status_code == 400 and "24" in response.get_data(as_text=True)
    project.con.close()


def test_weekly_inputs_survive_intermediate_form_actions():
    for action in ("add_member", "invalid", "busy", "conflict"):
        project, browser, saved_team = setup()
        path = f"/alpha/capacity/teams/{saved_team['id']}"
        fields = Form(browser.get(path).get_data(as_text=True), "team").values
        fields.update(action="add_member" if action == "add_member" else "save",
                      new_name="Cedar", **{"weekly:work:person-a:0": "6.250",
                                           "weekly:meetings:person-a:0": "0.750"})
        if action == "invalid":
            fields["timezone"] = "Imaginary/Place"
        if action == "busy":
            project.lock.acquire()
        if action == "conflict":
            project.con.execute("UPDATE capacity_teams SET version = 2 WHERE id = ?",
                                [saved_team["id"]])
        before = capacity.get_team(project.con, saved_team["id"])
        try:
            response = browser.post(path, data=fields)
        finally:
            if action == "busy":
                project.lock.release()
        assert response.status_code == {"add_member": 200, "invalid": 400,
                                        "busy": 409, "conflict": 409}[action]
        html = response.get_data(as_text=True)
        retained = Form(html, "team").values
        assert retained["weekly:work:person-a:0"] == "6.250", action
        assert retained["weekly:meetings:person-a:0"] == "0.750", action
        assert capacity.get_team(project.con, saved_team["id"]) == before, action
        project.con.close()


def test_partial_weekly_pattern_survives_save_reopen():
    project, browser, saved_team = setup()
    path = f"/alpha/capacity/teams/{saved_team['id']}"
    fields = Form(browser.get(path).get_data(as_text=True), "team").values
    fields.update(action="save", **{"weekly:work:person-a:0": "6.250",
                                    "weekly:meetings:person-a:0": ""})
    assert browser.post(path, data=fields).status_code == 302
    html = browser.get(path).get_data(as_text=True)
    saved = capacity.get_team(project.con, saved_team["id"])
    assert saved["members"][0]["weekly"][0] == {"work": 6.25, "meetings": None}
    assert saved["members"][0]["weekly"][1] == {"work": 7, "meetings": 1}
    fields = Form(html, "team").values
    for member in saved["members"]:
        for day in range(7):
            fields[f"weekly:work:{member['id']}:{day}"] = "7"
            fields[f"weekly:meetings:{member['id']}:{day}"] = "1"
    fields.update(action="save", **{"weekly:work:person-a:0": "24"})
    response = browser.post(path, data=fields)
    assert response.status_code == 400
    retained = Form(response.get_data(as_text=True), "team").values
    assert retained["weekly:work:person-a:0"] == "24"
    assert retained["weekly:meetings:person-a:0"] == "1"
    assert capacity.get_team(project.con, saved_team["id"]) == saved
    fields["weekly:work:person-a:0"] = "7"
    assert browser.post(path, data=fields).status_code == 302
    saved = capacity.get_team(project.con, saved_team["id"])
    assert saved["members"][0]["weekly"][0] == {"work": 7, "meetings": 1}
    project.con.close()


def test_empty_weekly_cells_render_blank_team_and_plan_inputs():
    project, browser, saved_team = setup()
    for member in saved_team["members"]:
        member["weekly"] = [None] * 7
    saved_team["members"][1]["weekly"][0] = {"work": 6.25, "meetings": 0}
    project.con.execute("UPDATE capacity_teams SET payload = ? WHERE id = ?",
                        [json.dumps(saved_team), saved_team["id"]])
    tables = ("capacity_teams", "capacity_plans", "capacity_revisions")
    before = {table: project.con.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    path = f"/alpha/capacity/plan/{saved_team['id']}/11"
    response = browser.get(path + "?start=2026-10-05&end=2026-10-11")
    assert response.status_code == 200
    fields = Form(response.get_data(as_text=True)).values
    assert fields["work:person-a:2026-10-05"] == ""
    assert fields["meetings:person-a:2026-10-05"] == ""
    assert fields["work:person-b:2026-10-05"] == "6.25"
    assert fields["meetings:person-b:2026-10-05"] == "0"
    assert "Incomplete" in response.get_data(as_text=True)
    response = browser.get(f"/alpha/capacity/teams/{saved_team['id']}")
    assert response.status_code == 200
    weekly = Form(response.get_data(as_text=True), "team").values
    assert weekly["weekly:work:person-a:0"] == ""
    assert weekly["weekly:meetings:person-a:0"] == ""
    assert weekly["weekly:work:person-b:0"] == "6.25"
    assert weekly["weekly:meetings:person-b:0"] == "0"
    fields.update(action="confirm", focus="75")
    response = browser.post(path, data=fields)
    assert response.status_code == 400 and "Missing inputs" in response.get_data(as_text=True)
    after = {table: project.con.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    assert after == before
    project.con.close()


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("capacity route checks passed")
