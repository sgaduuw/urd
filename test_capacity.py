"""Synthetic checks for durable sprint capacity inputs and audit history."""
import copy
import json
import math

import capacity
import urd


def team(name="North"):
    return {
        "name": name, "components": ["Engine"], "timezone": "Europe/Amsterdam", "boards": [1],
        "members": [
            {"id": "person-a", "name": "Aster",
             "weekly": [{"work": 7, "meetings": 1} for _ in range(4)]
             + [{"work": 0, "meetings": 0} for _ in range(3)]},
            {"id": "person-b", "name": "Birch",
             "weekly": [{"work": 3.5, "meetings": 0.5} for _ in range(4)]
             + [{"work": 0, "meetings": 0} for _ in range(3)]},
        ],
    }


def database():
    con = urd.open_db(":memory:")
    for sid in (11, 12):
        capacity.cache_sprint(con, {
            "id": sid, "name": f"Cycle {sid}", "state": "future", "boards": [1],
        })
    return con


def prepared(con, saved_team, sid=11):
    plan = capacity.new_plan(con, saved_team["id"], sid, "2026-10-05", "2026-10-08")
    plan["focus"] = 75
    return plan


def rejects(fn, message):
    try:
        fn()
    except ValueError as exc:
        assert message.lower() in str(exc).lower(), str(exc)
    else:
        raise AssertionError(f"expected rejection: {message}")


def test_capacity_and_isolation():
    con = database()
    north = capacity.save_team(con, team())
    south = capacity.save_team(con, team("South"))
    plan = prepared(con, north)
    assert capacity.totals(plan, complete=True)["focus_hours"] == 31.5
    first = capacity.save_plan(con, plan, 0)
    assert first["version"] == 1
    changed = copy.deepcopy(first)
    changed["members"][0]["daily"]["2026-10-05"]["work"] = 3
    second = capacity.save_plan(con, changed, 1)
    assert capacity.totals(second)["focus_hours"] == 28.5
    assert capacity.totals(capacity.get_plan(con, north["id"], 11, version=1))[
        "focus_hours"] == 31.5
    other_sprint = prepared(con, north, 12)
    other_team = prepared(con, south)
    assert capacity.totals(other_sprint)["focus_hours"] == 31.5
    capacity.save_plan(con, other_sprint, 0)
    capacity.save_plan(con, other_team, 0)
    assert capacity.get_plan(con, south["id"], 11)["version"] == 1
    north["members"][0]["weekly"] = [{"work": 1, "meetings": 0} for _ in range(7)]
    capacity.save_team(con, north, north["id"], north["version"])
    assert capacity.get_plan(con, north["id"], 11)["members"][0]["daily"][
        "2026-10-06"] == {"work": 7, "meetings": 1}
    rejects(lambda: capacity.save_plan(con, first, 1), "changed")
    con.close()


def test_validation():
    con = database()
    saved_team = capacity.save_team(con, team())
    plan = prepared(con, saved_team)
    for bad in (-1, 25, math.nan, math.inf, "bad"):
        altered = copy.deepcopy(plan)
        altered["members"][0]["daily"]["2026-10-05"]["work"] = bad
        rejects(lambda altered=altered: capacity.totals(altered), "hours")
    for invalid_daily in (None, [], 8):
        altered = copy.deepcopy(plan)
        if invalid_daily is None:
            del altered["members"][0]["daily"]
        else:
            altered["members"][0]["daily"] = invalid_daily
        rejects(lambda altered=altered: capacity.totals(altered), "daily")
    for bad in (-1, 101, math.nan):
        altered = copy.deepcopy(plan)
        altered["focus"] = bad
        rejects(lambda altered=altered: capacity.totals(altered), "focus")
    blank = copy.deepcopy(plan)
    blank["members"][0]["daily"]["2026-10-05"]["work"] = None
    assert capacity.totals(blank)["focus_hours"] is None
    capacity.save_plan(con, blank, 0)
    rejects(lambda: capacity.totals(blank, complete=True), "missing")
    blank["members"][0]["daily"]["2026-10-05"]["work"] = 0
    assert capacity.totals(blank, complete=True)["focus_hours"] == 26.25
    blank["members"][0]["daily"]["2026-10-05"]["meetings"] = 25
    rejects(lambda: capacity.totals(blank), "meetings")
    rejects(lambda: capacity.new_plan(con, saved_team["id"], 11, "", ""), "date")
    invalid_team = team()
    invalid_team["timezone"] = "Imaginary/Place"
    rejects(lambda: capacity.save_team(con, invalid_team), "timezone")
    con.close()


def test_confirmation_revisions_void_and_forecast():
    con = database()
    saved_team = capacity.save_team(con, team())
    plan = prepared(con, saved_team)
    baseline = {"cutoff": "2026-10-05T09:00:00Z", "kind": "observed",
                "issues": {"EX-1": {"points": 3}}, "coherent": True}
    confirmed = capacity.save_plan(con, plan, 0, action="confirm", baseline=baseline)
    assert confirmed["confirmation"] == 1
    changed = copy.deepcopy(confirmed)
    changed["members"][0]["daily"]["2026-10-05"]["work"] = 3
    rejects(lambda: capacity.save_plan(con, changed, 1), "reason")
    revised = capacity.save_plan(con, changed, 1, reason="Schedule changed")
    baseline["issues"]["EX-1"]["points"] = 8
    assert capacity.get_plan(con, saved_team["id"], 11, 1)["baseline"]["issues"][
        "EX-1"]["points"] == 3
    rejects(lambda: capacity.save_plan(con, revised, 2, action="void"), "reason")
    voided = capacity.save_plan(con, revised, 2, action="void", reason="Wrong starting scope")
    assert voided["confirmation"] is None
    rejects(lambda: capacity.save_plan(con, revised, 2, action="void", reason="stale"), "changed")
    replacement = capacity.save_plan(con, voided, 3, action="confirm", baseline=baseline)
    assert replacement["confirmation"] == 4
    assert replacement["replaces"] == {"team_id": saved_team["id"], "sprint_id": 11,
                                        "confirmation": 1}
    assert [r["version"] for r in capacity.revisions(con, saved_team["id"], 11)] == [1, 2, 3, 4]
    sources = [{"team_id": saved_team["id"], "sprint_id": 1, "confirmation": 1, "version": 2,
                "points": 20, "focus_hours": 100},
               {"team_id": saved_team["id"], "sprint_id": 2, "confirmation": 1, "version": 1,
                "points": 10, "focus_hours": 25}]
    rate = capacity.history_rate(sources)
    assert rate["value"] == 0.24
    assert capacity.forecast(50, rate) == 12
    sources[0]["points"] = 200
    assert rate["sources"][0]["points"] == 20
    assert capacity.forecast(50, {"kind": "none", "value": None}) is None
    assert capacity.forecast(50, {"kind": "manual", "value": 0}) == 0
    rejects(lambda: capacity.history_rate([{"points": 0, "focus_hours": 0}]), "positive")
    con.close()


def test_daily_arithmetic_and_outside_dates():
    value = {"dates": {"start": "2026-10-05", "end": "2026-10-06"}, "focus": 75,
             "members": [{"id": "one", "name": "Aster", "daily": {
                 "2026-10-05": {"work": 7, "meetings": 1},
                 "2026-10-06": {"work": 3.5, "meetings": 0.5},
                 "2026-10-07": {"work": 20, "meetings": 4}}}]}
    result = capacity.totals(value)
    assert result["available_hours"] == 12, result
    assert result["work_hours"] == 10.5 and result["focus_hours"] == 7.875, result
    value["dates"]["end"] = "2026-10-07"
    assert capacity.totals(value)["focus_hours"] == 22.875
    for field in ("work", "meetings"):
        for bad in (-1, 25, math.nan, math.inf, True, "bad"):
            invalid = copy.deepcopy(value)
            invalid["members"][0]["daily"]["2026-10-05"][field] = bad
            rejects(lambda invalid=invalid: capacity.totals(invalid), "hours")
        invalid = copy.deepcopy(value)
        invalid["members"][0]["daily"]["2026-10-05"][field] = ""
        assert capacity.totals(invalid)["focus_hours"] is None
        rejects(lambda invalid=invalid: capacity.totals(invalid, complete=True), "missing")
    invalid = copy.deepcopy(value)
    invalid["members"][0]["daily"]["2026-10-05"] = {"work": 23, "meetings": 2}
    rejects(lambda invalid=invalid: capacity.totals(invalid), "24")
    value["members"][0]["daily"]["2026-10-05"] = {"work": 0, "meetings": 0}
    assert capacity.totals(value, complete=True)["focus_hours"] == 17.625


def test_weekly_split_copy_and_validation():
    con = database()
    data = team()
    data["members"][0]["weekly"][6] = {"work": None, "meetings": 0}
    saved_team = capacity.save_team(con, data)
    first = capacity.new_plan(con, saved_team["id"], 11, "2026-10-05", "2026-10-18")
    assert first["members"][0]["daily"]["2026-10-05"] == {"work": 7, "meetings": 1}
    assert first["members"][0]["daily"]["2026-10-11"] == {"work": None, "meetings": 0}
    first["members"][0]["daily"]["2026-10-05"]["meetings"] = 2
    assert first["members"][0]["daily"]["2026-10-12"]["meetings"] == 1
    second = capacity.new_plan(con, saved_team["id"], 12, "2026-10-05", "2026-10-18")
    assert second["members"][0]["daily"]["2026-10-05"]["meetings"] == 1
    data["members"][0]["weekly"][0] = {"work": 23, "meetings": 2}
    rejects(lambda: capacity.save_team(con, data), "24")
    data["members"][0]["weekly"][0] = 8
    rejects(lambda: capacity.save_team(con, data), "hours")
    con.close()


def test_blank_weekly_cells_stay_unentered():
    con = database()
    saved_team = capacity.save_team(con, team())
    confirmed = capacity.save_plan(con, prepared(con, saved_team), 0, action="confirm",
                                   baseline={"coherent": True})
    blank = {"work": None, "meetings": None}
    saved_team["members"][0]["weekly"] = [blank] * 7
    saved_team["members"][1]["weekly"] = [{"work": 6.25, "meetings": 0}, blank,
                                         {"work": 0, "meetings": 0}] + [blank] * 4
    con.execute("UPDATE capacity_teams SET payload = ? WHERE id = ?",
                [json.dumps(saved_team), saved_team["id"]])
    before = con.execute("SELECT * FROM capacity_teams").fetchall()
    loaded = capacity.get_team(con, saved_team["id"])
    assert loaded["members"][0]["weekly"] == [{"work": None, "meetings": None}] * 7
    assert loaded["members"][1]["weekly"][0] == {"work": 6.25, "meetings": 0}
    draft = capacity.new_plan(con, saved_team["id"], 12, "2026-10-05", "2026-10-11")
    assert draft["members"][0]["daily"]["2026-10-05"] == {"work": None, "meetings": None}
    assert draft["members"][1]["daily"]["2026-10-05"] == {"work": 6.25, "meetings": 0}
    assert draft["members"][1]["daily"]["2026-10-07"] == {"work": 0, "meetings": 0}
    draft["focus"] = 100
    assert capacity.totals(draft)["focus_hours"] is None
    rejects(lambda: capacity.totals(draft, complete=True), "missing")
    draft["members"][0]["daily"]["2026-10-05"]["work"] = 2
    assert draft["members"][0]["daily"]["2026-10-06"]["work"] is None
    assert loaded["members"][0]["weekly"][0]["work"] is None
    assert con.execute("SELECT * FROM capacity_teams").fetchall() == before
    assert capacity.get_plan(con, saved_team["id"], 11) == confirmed
    assert capacity.get_plan(con, saved_team["id"], 12) is None
    con.close()


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("capacity checks passed")
