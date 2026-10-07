"""Forecast provenance, private exports and whole-volume recovery."""
import copy
import json
import math
import shutil
import tempfile
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch

import capacity
import capacity_history as history
import urd
import webapp
from test_capacity import rejects, team
from test_capacity_history import SPRINT, SPRINT_FIELD, fixture, issue, put
from test_views_capacity import Form


def setup(path=":memory:"):
    second = dict(SPRINT, id=12, name="Cycle 12", startDate="2026-02-02T09:00:00Z",
                  endDate="2026-02-13T17:00:00Z", completeDate="2026-02-13T17:00:00Z")
    records = [issue("EX-1", 20), issue("EX-2", 10, sprint_id=12,
                                      completion="2026-02-10T12:00:00Z")]
    records[1]["fields"][SPRINT_FIELD] = [second]
    for record in records:
        record["fields"]["updated"] = "2026-03-01T12:00:00Z"
    con = fixture(records, path)
    con.execute("UPDATE raw_issues SET fetched_at = '2026-03-01T12:00:00'")
    urd.save_scope(con, project="EX", last_sync_at="2026-03-01T12:00:00Z",
                   derived_sync_at="2026-03-01T12:00:00Z", component="")
    capacity.cache_sprint(con, second)
    target_sprint = {"id": 13, "name": "Cycle 13", "state": "future", "boards": [1],
                     "startDate": "2026-03-02T09:00:00Z", "endDate": "2026-03-06T17:00:00Z"}
    capacity.cache_sprint(con, target_sprint)
    data = team()
    data["members"][0]["name"] = "PRIVATE_MEMBER_SENTINEL"
    saved_team = capacity.save_team(con, data)

    def plan(sprint_id, start, end, hours):
        value = capacity.new_plan(con, saved_team["id"], sprint_id, start, end)
        value["focus"] = 100
        for n, member in enumerate(value["members"]):
            member["daily"] = {d: {"work": hours if n == 0 else 0, "meetings": 0}
                               for d in member["daily"]}
        return value

    first = plan(11, "2026-01-05", "2026-01-09", 20)
    second_plan = plan(12, "2026-02-02", "2026-02-06", 5)
    for value in (first, second_plan):
        capacity.save_plan(con, value, 0, action="confirm", baseline=history.baseline(con, value))
    target = plan(13, "2026-03-02", "2026-03-06", 10)
    return con, saved_team, target


def test_history_rate_and_export_privacy():
    con, saved_team, target = setup()
    candidates = capacity.history_candidates(con, target)
    assert all(c["eligible"] for c in candidates), candidates
    target["rate"] = capacity.history_rate(candidates)
    assert target["rate"]["value"] == 0.24
    saved = capacity.save_plan(con, target, 0, action="confirm",
                               baseline=history.baseline(con, target))
    assert saved["forecast"] == 12
    revised = capacity.save_plan(con, saved, 1, reason="PRIVATE_REASON_SENTINEL")
    html = capacity.report_section(con)
    assert "PRIVATE_MEMBER_SENTINEL" not in html
    assert "PRIVATE_REASON_SENTINEL" not in html
    assert "12" in html and "focus hours" in html and "Cycle 13" in html
    detail = capacity.report_section(con, include_grid=True, plan=revised)
    assert "PRIVATE_MEMBER_SENTINEL" in detail
    assert "PRIVATE_REASON_SENTINEL" not in detail
    assert "Work: 10" in detail and "Meetings: 0" in detail
    assert "<th>Meetings</th>" not in detail

    source = capacity.get_plan(con, saved_team["id"], 11)
    original_source = copy.deepcopy(source)
    source["members"][1]["daily"] = {d: {"work": 20, "meetings": 0}
                                       for d in source["members"][1]["daily"]}
    capacity.save_plan(con, source, source["version"], reason="Additional reconstructed hours")
    assert capacity.get_plan(con, saved_team["id"], 11, 1) == original_source
    assert capacity.get_plan(con, saved_team["id"], 13) == revised
    assert next(c for c in revised["rate"]["sources"] if c["sprint_id"] == 11)["focus_hours"] == 100
    source = capacity.get_plan(con, saved_team["id"], 12)
    capacity.save_plan(con, source, source["version"], action="void", reason="Invalid source")
    assert not next(c for c in capacity.history_candidates(con, target)
                    if c["sprint_id"] == 12)["eligible"]
    assert "voided" in capacity.report_section(con, plan=revised).lower()
    assert capacity.get_plan(con, saved_team["id"], 13)["rate"] == target["rate"]
    capacity.cache_sprint(con, {"id": 14, "name": "Cycle 14", "state": "future", "boards": [1]})
    draft = copy.deepcopy(target)
    draft["sprint_id"] = 14
    draft["sprint"] = capacity.sprint(con, 14)
    rejects(lambda: capacity.save_plan(con, draft, 0), "voided")
    con.close()


def test_closed_volume_backup_restore_keeps_local_records():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        volume = root / "live"
        volume.mkdir()
        con, saved_team, target = setup(str(volume / "example.duckdb"))
        original = capacity.get_plan(con, saved_team["id"], 11)
        target["rate"] = capacity.history_rate(capacity.history_candidates(con, target))
        saved = capacity.save_plan(con, target, 0, action="confirm",
                                   baseline=history.baseline(con, target))
        capacity.save_plan(con, saved, 1, reason="PRIVATE_REASON_SENTINEL")
        source = capacity.get_plan(con, saved_team["id"], 12)
        voided = capacity.save_plan(con, source, 1, action="void", reason="Corrected baseline")
        capacity.save_plan(con, voided, 2, action="confirm",
                           baseline=history.baseline(con, voided))
        tables = ("capacity_teams", "capacity_plans", "capacity_revisions")
        before = {table: con.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()
                  for table in tables}
        con.close()
        backup, restored = root / "backup", root / "restored"
        shutil.copytree(volume, backup)
        shutil.copytree(backup, restored)
        con = urd.open_db(str(restored / "example.duckdb"))
        urd.derive(con, "Ready,Finished,Dropped", "Ready", "Finished", "Dropped")
        con.close()
        con = urd.open_db(str(restored / "example.duckdb"))
        after = {table: con.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()
                 for table in tables}
        assert after == before
        assert capacity.get_plan(con, saved_team["id"], 13)["forecast"] == 12
        assert capacity.get_plan(con, saved_team["id"], 11) == original
        con.close()



def browser(con):
    app = webapp.create_app({
        "alpha": SimpleNamespace(con=con, lock=Lock(), configured=lambda: True)})
    client = app.test_client()
    client.environ_base = {"HTTP_SEC_FETCH_SITE": "same-origin"}
    return client


def form(client, saved_team, target):
    path = f"/alpha/capacity/plan/{saved_team['id']}/13"
    fields = Form(client.get(path).get_data(as_text=True)).values
    fields.update(focus="100", rate_mode="keep")
    for index, member in enumerate(target["members"]):
        for day in member["daily"]:
            fields[f"work:{member['id']}:{day}"] = "10" if index == 0 else "0"
            fields[f"meetings:{member['id']}:{day}"] = "0"
    return path, fields



def test_busy_save_retains_manual_rate_for_retry():
    con, saved_team, target = setup()
    client = browser(con)
    path, fields = form(client, saved_team, target)
    fields.update(action="save", rate_mode="manual", manual_rate="0.4200",
                  **{"work:person-a:2026-03-02": "8.750", "meetings:person-a:2026-03-02": "1.250"})
    lock = client.application.config["REGISTRY"]["alpha"].lock
    lock.acquire()
    try:
        response = client.post(path, data=fields)
    finally:
        lock.release()
    assert response.status_code == 409
    retained = Form(response.get_data(as_text=True)).values
    assert (retained["rate_mode"], retained["manual_rate"]) == ("manual", "0.4200"), retained
    assert retained["work:person-a:2026-03-02"] == "8.750"
    assert retained["meetings:person-a:2026-03-02"] == "1.250"
    assert capacity.get_plan(con, saved_team["id"], 13) is None
    assert json.loads(retained["payload"])["rate"]["kind"] == "none"
    retained["action"] = "save"
    assert client.post(path, data=retained).status_code == 302
    saved = capacity.get_plan(con, saved_team["id"], 13)
    assert saved["rate"] == {"kind": "manual", "value": 0.42, "sources": []}
    assert math.isclose(saved["forecast"], 20.475)
    assert saved["totals"]["available_hours"] == 50
    con.close()


def test_invalid_rate_inputs_are_retained_without_saving():
    for manual_rate, focus in (("-0.42", "100"), ("0.4200", "101")):
        con, saved_team, target = setup()
        client = browser(con)
        path, fields = form(client, saved_team, target)
        fields.update(action="save", rate_mode="manual", manual_rate=manual_rate, focus=focus)
        response = client.post(path, data=fields)
        assert response.status_code == 400
        retained = Form(response.get_data(as_text=True)).values
        assert (retained["rate_mode"], retained["manual_rate"]) == ("manual", manual_rate)
        assert capacity.get_plan(con, saved_team["id"], 13) is None
        con.close()


def test_form_actions_retain_pending_rate_controls():
    for action in ("add_member", "review_latest", "team_setup", "preview"):
        con, saved_team, target = setup()
        client = browser(con)
        path, fields = form(client, saved_team, target)
        fields.update(action=action, new_name="Cedar", rate_mode="manual", manual_rate="0.4200",
                      history=["11", "12"])
        response = client.post(path, data=fields)
        assert response.status_code == 200
        retained = Form(response.get_data(as_text=True)).values
        assert (retained["rate_mode"], retained["manual_rate"]) == ("manual", "0.4200"), action
        assert set(retained["history"]) == {"11", "12"}, action
        assert capacity.get_plan(con, saved_team["id"], 13) is None
        con.close()


def test_ineligible_selected_history_is_retained_and_rejected_on_retry():
    con, saved_team, target = setup()
    client = browser(con)
    path, fields = form(client, saved_team, target)
    fields.update(action="save", rate_mode="history", history=["11", "12"])
    source = capacity.get_plan(con, saved_team["id"], 12)
    capacity.save_plan(con, source, source["version"], action="void", reason="Mistaken source")
    response = client.post(path, data=fields)
    assert response.status_code == 409
    retained = Form(response.get_data(as_text=True)).values
    assert retained["rate_mode"] == "history"
    assert set(retained["history"]) == {"11", "12"}
    assert "Voided or not confirmed" in response.get_data(as_text=True)
    retained["action"] = "save"
    response = client.post(path, data=retained)
    assert response.status_code == 400
    retained = Form(response.get_data(as_text=True)).values
    assert retained["rate_mode"] == "history" and set(retained["history"]) == {"11", "12"}
    assert capacity.get_plan(con, saved_team["id"], 13) is None
    retained.update(action="save", history="11")
    assert client.post(path, data=retained).status_code == 302
    saved = capacity.get_plan(con, saved_team["id"], 13)
    assert saved["rate"]["value"] == 0.2
    assert [source["sprint_id"] for source in saved["rate"]["sources"]] == [11]
    con.close()


def test_changed_historical_capacity_requires_fresh_review():
    for action in ("confirm", "save"):
        con, saved_team, target = setup()
        client = browser(con)
        path, fields = form(client, saved_team, target)
        fields.update(action=action, rate_mode="history", history=["11", "12"])
        source = capacity.get_plan(con, saved_team["id"], 11)
        for member in source["members"]:
            for cell in member["daily"].values():
                cell["work"] /= 2
        capacity.save_plan(con, source, source["version"], reason="Correct capacity")
        response = client.post(path, data=fields)
        saved = capacity.get_plan(con, saved_team["id"], 13)
        actual = None if saved is None else saved["rate"]["value"]
        assert response.status_code == 409 and saved is None, (response.status_code, actual)
        retained = Form(response.get_data(as_text=True)).values
        assert retained["rate_mode"] == "history"
        assert set(retained["history"]) == {"11", "12"}
        retained["action"] = action
        assert client.post(path, data=retained).status_code == 302
        saved = capacity.get_plan(con, saved_team["id"], 13)
        assert saved["rate"]["value"] == 0.4
        assert {source["sprint_id"] for source in saved["rate"]["sources"]} == {11, 12}
        con.close()


def test_post_render_preview_and_token_use_same_snapshot():
    con, saved_team, target = setup()
    client = browser(con)
    path, fields = form(client, saved_team, target)
    fields.update(action="add_member", new_name="Cedar")
    baseline = history.baseline

    def refresh_after_preview(cursor, value, *args, **kwargs):
        preview = baseline(cursor, value, *args, **kwargs)
        # A second connection commits between the preview and its source token.
        put(con, [issue("EX-99", 7, sprint_id=13)])
        return preview

    with patch.object(history, "baseline", side_effect=refresh_after_preview):
        response = client.post(path, data=fields)
    fields = Form(response.get_data(as_text=True)).values
    value = json.loads(fields["payload"])
    fields.update(action="confirm", rate_mode="keep")
    for member in value["members"]:
        for day in capacity.days(value["dates"]["start"], value["dates"]["end"]):
            fields[f"meetings:{member['id']}:{day}"] = "0"
            key = f"work:{member['id']}:{day}"
            if not fields.get(key):
                fields[key] = "0"
    response = client.post(path, data=fields)
    saved = capacity.get_plan(con, saved_team["id"], 13)
    actual = None if saved is None else saved["baseline"]["known_points"]
    con.close()
    assert response.status_code == 409 and saved is None, (response.status_code, actual)


def test_local_review_warns_about_voided_rate_source():
    con, saved_team, target = setup()
    target["rate"] = capacity.history_rate(capacity.history_candidates(con, target))
    capacity.save_plan(con, target, 0, action="confirm",
                       baseline=history.baseline(con, target))
    source = capacity.get_plan(con, saved_team["id"], 12)
    capacity.save_plan(con, source, source["version"], action="void", reason="Mistaken source")
    client = browser(con)
    path = f"/alpha/capacity/plan/{saved_team['id']}/13"
    html = client.get(path).get_data(as_text=True)
    con.close()
    assert "was voided or replaced" in html, "Local review omits the source warning"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("capacity report and recovery checks passed")
