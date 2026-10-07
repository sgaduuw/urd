"""Synthetic historical scope, completion and parent/subtask evidence."""
import json
from itertools import count

import capacity
import capacity_history as history
import urd

START = "2026-01-05T09:00:00Z"
CLOSE = "2026-01-16T17:00:00Z"
OBSERVED = "2026-01-20T12:00:00Z"
SPRINT = {"id": 11, "name": "Cycle 11", "state": "closed", "startDate": START,
          "endDate": CLOSE, "completeDate": CLOSE, "boards": [1]}
HISTORY_IDS = count(1)
SETTINGS = {"id": "team", "name": "North", "components": ["Engine"], "timezone": "UTC"}
POINTS, SPRINT_FIELD = "customfield_points", "customfield_sprint"


def event(at, field, before, after, before_text=None, after_text=None, identity=None):
    return {"id": identity or str(next(HISTORY_IDS)), "created": at, "items": [{
        "field": field, "fieldId": {"Story Points": POINTS, "Sprint": SPRINT_FIELD,
                                    "Component": "components"}.get(field),
        "from": before, "to": after, "fromString": before_text, "toString": after_text,
    }]}


def issue(key, points, *, parent=None, done=True, component="Engine", sprint_id=11,
          created="2026-01-01T09:00:00Z", completion="2026-01-10T12:00:00Z"):
    status = {"id": "done" if done else "open", "name": "Finished" if done else "Ready",
              "statusCategory": {"key": "done" if done else "new"}}
    changes = [event(completion, "status", "open", "done", "Ready", "Finished")] if done else []
    return {"id": key.replace("EX-", ""), "key": key, "fields": {
        "created": created, "updated": OBSERVED, "summary": "Synthetic work",
        "issuetype": {"id": "child" if parent else "main", "subtask": bool(parent)},
        "parent": {"id": parent.replace("EX-", ""), "key": parent} if parent else None,
        "status": status, "components": [{"id": component, "name": component}],
        POINTS: points, SPRINT_FIELD: [dict(SPRINT, id=sprint_id)] if sprint_id else [],
        "subtasks": [],
    }, "changelog": {"histories": changes, "total": len(changes)}}


def fixture(records, path=":memory:"):
    con = urd.open_db(path)
    con.execute("INSERT INTO fields VALUES (?, 'Story Points', NULL), (?, 'Sprint', NULL)",
                [POINTS, SPRINT_FIELD])
    con.execute("INSERT INTO statuses VALUES ('Ready', 'new'), ('Finished', 'done'), "
                "('Dropped', 'done')")
    urd.save_scope(con, last_sync_at=OBSERVED, derived_sync_at=OBSERVED,
                   earliest_since="2000-01-01", abandoned_status="Dropped")
    put(con, records)
    capacity.cache_sprint(con, SPRINT)
    return con


def put(con, records):
    keys = {r["key"]: r for r in records}
    for record in records:
        parent = record["fields"].get("parent")
        if (parent and parent["key"] in keys
                and "subtasks" in keys[parent["key"]]["fields"]):
            children = keys[parent["key"]]["fields"]["subtasks"]
            if not any(c["key"] == record["key"] for c in children):
                children.append({"id": record["id"], "key": record["key"]})
    for record in records:
        record["changelog"]["total"] = len(record["changelog"]["histories"])
        con.execute("INSERT OR REPLACE INTO raw_issues VALUES (?, ?, ?, ?)",
                    [record["key"], record["fields"]["updated"], urd._ts(OBSERVED),
                     json.dumps(record)])


def payload():
    return {"team_id": "team", "sprint_id": 11, "settings": SETTINGS, "sprint": SPRINT}


def review(con):
    baseline = history.baseline(con, payload(), now=OBSERVED)
    return history.review(con, payload(), baseline)



def test_null_sprint_is_known_backlog():
    backlog = issue("EX-2", 3, done=False, sprint_id=None)
    backlog["fields"][SPRINT_FIELD] = None
    con = fixture([issue("EX-1", 5), backlog])
    result = review(con)
    con.close()
    assert result["delivered_points"] == 5
    assert result["points_complete"] and result["rate_eligible"], result


def test_missing_malformed_or_unresolved_sprint_stays_unknown():
    for sprint_value in ("missing", "", {}, False, [{}]):
        backlog = issue("EX-2", 3, done=False, sprint_id=None)
        if sprint_value == "missing":
            del backlog["fields"][SPRINT_FIELD]
        else:
            backlog["fields"][SPRINT_FIELD] = sprint_value
        con = fixture([issue("EX-1", 5), backlog])
        result = review(con)
        con.close()
        assert result["delivered_points"] == 5
        assert not result["points_complete"] and not result["rate_eligible"], sprint_value
    con = fixture([issue("EX-1", 5)])
    con.execute("DELETE FROM fields WHERE id = ?", [SPRINT_FIELD])
    result = review(con)
    con.close()
    assert not result["points_complete"] and not result["rate_eligible"]


def test_point_sources_and_completion():
    records = [issue("EX-1", 8), issue("EX-2", 3, parent="EX-1"),
               issue("EX-3", 5, parent="EX-1")]
    con = fixture(records)
    result = review(con)
    assert result["delivered_points"] == 8, result
    assert result["points_complete"], result
    assert result["delivered_counts"] == {"main": 1, "subtask": 2}
    assert result["original_counts"] == {"main": 1, "subtask": 2}
    records[0] = issue("EX-1", 8, done=False)
    records[2] = issue("EX-3", 5, parent="EX-1", done=False)
    put(con, records)
    assert review(con)["delivered_points"] == 3
    records[0] = issue("EX-1", 8)
    records[1] = issue("EX-2", 0, parent="EX-1", component="Other")
    put(con, records)
    assert review(con)["delivered_points"] == 0
    records[1] = issue("EX-2", None, parent="EX-1")
    records[2] = issue("EX-3", None, parent="EX-1")
    put(con, records)
    result = review(con)
    assert result["delivered_points"] == 8 and result["points_complete"], result
    records[1] = issue("EX-2", 3, parent="EX-1")
    put(con, records)
    result = review(con)
    assert result["delivered_points"] == 3 and not result["points_complete"], result
    del records[0]["fields"]["subtasks"]
    put(con, records)
    result = review(con)
    assert not result["points_complete"]
    con.close()


def test_completion_boundaries_and_estimate_history():
    records = [
        issue("EX-1", 5, completion="2026-01-04T09:00:00Z"),
        issue("EX-2", 3, completion=START),
        issue("EX-3", 8, completion=CLOSE),
    ]
    records[1]["changelog"]["histories"].extend([
        event("2026-01-12T09:00:00Z", "Story Points", None, None, "1", "2"),
        event("2026-01-18T09:00:00Z", "Story Points", None, None, "2", "3"),
    ])
    con = fixture(records)
    result = review(con)
    assert result["delivered_points"] == 2, result
    assert result["original_outcomes"]["already_completed"] == ["EX-1"]
    assert result["original_outcomes"]["unfinished"] == ["EX-3"]
    assert result["tickets"]["EX-2"]["entry_points"] == 1
    assert result["tickets"]["EX-2"]["closing_points"] == 2
    assert result["tickets"]["EX-2"]["estimate_change"] == 1
    records[0]["changelog"]["histories"].extend([
        event("2026-01-07T09:00:00Z", "status", "done", "open", "Finished", "Ready"),
        event("2026-01-08T09:00:00Z", "status", "open", "done", "Ready", "Finished"),
    ])
    put(con, records)
    assert review(con)["delivered_points"] == 7
    con.close()


def test_post_close_scope_and_structure():
    child = issue("EX-2", 3, parent="EX-1", component="Other")
    child["changelog"]["histories"].extend([
        event("2026-01-18T09:00:00Z", "Component", "Engine", None, "Engine", None),
        event("2026-01-18T09:00:00Z", "Component", None, "Other", None, "Other"),
    ])
    con = fixture([issue("EX-1", 8), child])
    result = review(con)
    assert result["delivered_points"] == 3
    assert result["delivered_counts"]["subtask"] == 1
    child["fields"]["issuetype"] = {"id": "main", "subtask": False}
    child["fields"]["parent"] = None
    child["changelog"]["histories"].extend([
        event("2026-01-18T10:00:00Z", "issuetype", "child", "main"),
        event("2026-01-18T10:00:00.099Z", "IssueParentAssociation", "1", None, "EX-1", None),
    ])
    put(con, [child, issue("EX-3", None, parent="EX-1", done=False)])
    assert review(con)["delivered_counts"]["subtask"] == 1
    assert review(con)["delivered_points"] == 3
    con.close()




def test_cutoff_attribution_reentry_and_unknowns():
    baseline_time = "2026-01-06T10:00:00Z"
    join = "2026-01-06T11:00:00Z"
    added = issue("EX-1", 5)
    added["changelog"]["histories"].extend([
        event(join, "Sprint", None, "11"),
        event("2026-01-12T09:00:00Z", "Story Points", None, None, "3", "5"),
    ])
    con = fixture([added])
    saved = history.snapshot(con, SETTINGS, SPRINT, baseline_time)
    result = history.review(con, payload(), saved)
    assert saved["issues"] == {}
    assert result["added_delivered"] == ["EX-1"], result
    assert result["tickets"]["EX-1"]["entry_points"] == 3
    assert result["tickets"]["EX-1"]["estimate_change"] == 2
    added["changelog"]["histories"].extend([
        event("2026-01-02T09:00:00Z", "Sprint", None, "11"),
        event("2026-01-06T09:00:00Z", "Sprint", "11", None),
    ])
    put(con, [added])
    result = history.review(con, payload(), saved)
    assert result["added"] == ["EX-1"]
    assert result["tickets"]["EX-1"]["entry_points"] == 3
    # An omitted original proven present at the source cutoff stays unclassified.
    saved["cutoff"] = "2026-01-06T12:00:00Z"
    result = history.review(con, payload(), saved)
    assert result["unclassified_delivered"] == ["EX-1"]
    assert result["delivered_points"] == 5 and result["rate_eligible"], result
    assert not result["attribution_complete"]
    missing_start = dict(SPRINT, startDate=None)
    capacity.cache_sprint(con, missing_start)
    result = history.review(con, payload(), saved)
    assert not result["points_complete"] and not result["rate_eligible"]
    capacity.cache_sprint(con, SPRINT)
    changed = copy_record(added)
    changed["changelog"] = {"histories": [], "total": 5}
    con.execute("UPDATE raw_issues SET json = ? WHERE key = 'EX-1'", [json.dumps(changed)])
    assert not history.review(con, payload(), saved)["points_complete"]
    con.close()


def copy_record(record):
    return json.loads(json.dumps(record))


def test_completion_unknown_and_done_to_done():
    record = issue("EX-1", 5)
    record["changelog"]["histories"] = [
        event("2026-01-10T12:00:00Z", "status", "done", "done", "Finished", "Finished")]
    con = fixture([record])
    assert review(con)["delivered_points"] == 0
    record["changelog"]["histories"] = [
        event("2026-01-10T12:00:00Z", "status", None, "done", None, "Finished")]
    put(con, [record])
    assert not review(con)["points_complete"]
    record["changelog"]["histories"] = []
    record["fields"]["created"] = "2026-01-07T09:00:00Z"
    put(con, [record])
    assert review(con)["delivered_points"] == 5
    # A to-state contradicting the observed state cannot establish completion.
    record["fields"]["created"] = "2026-01-01T09:00:00Z"
    record["changelog"]["histories"] = [
        event("2026-01-10T12:00:00Z", "status", "open", "other-done", "Ready", "Other done")]
    con.execute("INSERT INTO capacity_metadata VALUES ('status', 'other-done', '\"done\"')")
    put(con, [record])
    assert not review(con)["points_complete"]
    con.close()


def test_history_field_identity_and_source_coverage():
    record = issue("EX-1", 5)
    unrelated = event("2026-01-18T09:00:00Z", "Story Points", None, None, "50", "60")
    unrelated["items"][0]["fieldId"] = "customfield_unrelated"
    record["changelog"]["histories"].append(unrelated)
    con = fixture([record])
    assert review(con)["delivered_points"] == 5
    assert review(con)["points_complete"], "Unrelated custom field contaminated points"
    urd.save_scope(con, earliest_since="2026-01-08")
    assert not review(con)["points_complete"], "A narrow mirror cannot prove earlier delivery"
    con.close()


def test_source_inventory_and_pruning():
    assert "subtasks" in urd.BASE_FIELDS
    from test_sync_safety import _sync
    con, jira, _ = _sync([{"issues": [], "isLast": True}])
    urd.sync(con, jira)
    assert con.execute("SELECT count(*) FROM capacity_pruned").fetchone()[0] == 2
    con.close()



def test_observed_baseline_conflict_and_duplicate_history():
    early = issue("EX-1", 3, done=False, sprint_id=None)
    con = fixture([early])
    active = dict(SPRINT, state="active", completeDate=None)
    capacity.cache_sprint(con, active)
    urd.save_scope(con, last_sync_at="2026-01-06T10:02:00Z",
                   derived_sync_at="2026-01-06T10:02:00Z")
    con.execute("UPDATE raw_issues SET fetched_at = '2026-01-06T10:00:00'")
    captured = history.baseline(con, payload(), now="2026-01-06T10:03:00Z")
    assert captured["issues"] == {}
    later = issue("EX-1", 5)
    later["changelog"]["histories"].extend([
        event("2026-01-06T10:01:00Z", "Sprint", None, "11"),
        event("2026-01-08T09:00:00Z", "Story Points", None, None, "3", "5"),
    ])
    duplicate = copy_record(later["changelog"]["histories"][0])
    later["changelog"]["histories"].append(duplicate)
    put(con, [later])
    capacity.cache_sprint(con, SPRINT)
    urd.save_scope(con, last_sync_at=OBSERVED, derived_sync_at=OBSERVED)
    result = history.review(con, payload(), captured)
    assert result["unclassified_delivered"] == ["EX-1"]
    assert result["delivered_points"] == 5 and result["rate_eligible"]
    # An original recorded from an older observation is also visibly uncertain.
    captured = history.snapshot(con, SETTINGS, SPRINT, "2026-01-09T10:00:00Z")
    captured["issues"]["EX-1"]["points"] = 3
    result = history.review(con, payload(), captured)
    assert result["original_delivered"] == ["EX-1"]
    assert result["tickets"]["EX-1"]["baseline_conflict"]
    assert not result["attribution_complete"]
    con.close()

def test_unchanged_issue_observation_is_not_its_old_fetch_time():
    record = issue("EX-1", 5)
    record["fields"]["updated"] = "2026-01-10T12:00:00Z"
    con = fixture([record])
    con.execute("UPDATE raw_issues SET fetched_at = '2026-01-11T09:00:00'")
    assert not review(con)["points_complete"]
    history.observe(con, "EX-1", record["fields"]["updated"], OBSERVED)
    assert review(con)["points_complete"]
    assert review(con)["delivered_points"] == 5
    history.observe(con, "EX-1", "2026-01-19T09:00:00Z", OBSERVED)
    assert not review(con)["points_complete"], "Changed, unfetched issue is not verified"
    con.close()



def test_conversion_preserves_original_cohort_and_classification():
    converted = issue("EX-2", 3, parent="EX-1")
    converted["changelog"]["histories"].extend([
        event("2026-01-08T09:00:00Z", "issuetype", "main", "child"),
        event("2026-01-08T09:00:00.099Z", "Parent", None, "1", None, "EX-1"),
    ])
    con = fixture([issue("EX-1", 8), converted,
                   issue("EX-3", None, parent="EX-1", done=False, component="Other")])
    result = review(con)
    assert result["original_counts"] == {"main": 2, "subtask": 0}
    assert result["delivered_counts"] == {"main": 1, "subtask": 1}
    assert result["delivered_points"] == 3
    assert result["tickets"]["EX-2"]["converted"]
    assert result["tickets"]["EX-2"]["cohort"] == "original"
    assert result["original_outcomes"]["delivered"] == ["EX-1", "EX-2"]
    con.close()


def test_known_subtask_points_survive_unrelated_unknown_type():
    unknown = issue("EX-3", 5, component="Other", sprint_id=99)
    unknown["fields"]["issuetype"] = {"id": "unclassified"}
    con = fixture([issue("EX-1", 8, done=False),
                   issue("EX-2", 3, parent="EX-1"), unknown])
    result = review(con)
    con.close()
    assert result["delivered_points"] == 3, result["delivered_points"]


def test_unrelated_unknown_type_keeps_the_parent_fallback():
    unrelated = issue("EX-9", 5, component="Other", sprint_id=99)
    unrelated["fields"]["issuetype"] = {"id": "unclassified"}
    con = fixture([issue("EX-1", 8), unrelated])
    result = review(con)
    con.close()
    assert result["delivered_points"] == 8, result["delivered_points"]
    assert result["points_complete"] and result["rate_eligible"], result["unestimated"]


def test_unknown_type_under_a_parent_leaves_only_that_parent_uncertain():
    child = issue("EX-2", None, parent="EX-1")
    child["fields"]["issuetype"] = {"id": "unclassified"}
    con = fixture([issue("EX-1", 8), child, issue("EX-3", 5)])
    result = review(con)
    con.close()
    assert result["tickets"]["EX-1"]["point_source"] is None
    assert result["tickets"]["EX-3"]["point_source"] is True
    assert result["delivered_points"] == 5 and not result["points_complete"]


def test_omitted_original_removed_later_stays_unclassified():
    record = issue("EX-1", 5, sprint_id=None)
    record["changelog"]["histories"].append(
        event("2026-01-12T09:00:00Z", "Sprint", "11", None))
    con = fixture([record])
    baseline = {"issues": {}, "cutoff": "2026-01-06T10:00:00Z", "inclusive": True}
    result = history.review(con, payload(), baseline)
    con.close()
    assert "EX-1" in result["tickets"], result["tickets"]
    assert result["tickets"]["EX-1"]["cohort"] == "unclassified"
    assert not result["attribution_complete"]


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("capacity history checks passed")
