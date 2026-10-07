"""Offline Jira evidence for capacity comparisons, independent of report filters."""
import hashlib
import json
from datetime import UTC, datetime

import capacity
import urd

FIELDS = ("points", "sprints", "components", "type", "parent", "status")


def timestamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def source_id(con):
    digest = hashlib.sha256()
    # ponytail: hash the local mirror per preview; cache by sync generation if large mirrors lag.
    for row in con.execute(
            "SELECT key, updated, fetched_at, json FROM raw_issues ORDER BY key").fetchall():
        digest.update(repr(row).encode())
    digest.update(repr(con.execute("SELECT * FROM sync_state").fetchall()).encode())
    digest.update(repr(con.execute("SELECT id, name FROM fields ORDER BY id").fetchall()).encode())
    digest.update(repr(con.execute("SELECT * FROM capacity_metadata ORDER BY kind, id").fetchall())
                  .encode())
    for table in ("statuses", "capacity_pruned", "capacity_sprints", "capacity_observations"):
        digest.update(repr(con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()).encode())
    return digest.hexdigest()


def _remember(mapping, key, value):
    if key is not None and value is not None:
        key = str(key)
        mapping[key] = value if key not in mapping or mapping[key] == value else None


def context(con):
    records = {}
    observations = {k: (updated, timestamp(at)) for k, updated, at in con.execute(
        "SELECT key, updated, observed_at FROM capacity_observations").fetchall()}
    types, statuses, names, ids = {}, {}, {}, {}
    for kind, identity, value in con.execute(
            "SELECT kind, id, payload FROM capacity_metadata").fetchall():
        target = types if kind == "type" else statuses
        target[str(identity)] = json.loads(value)
    for name, category in con.execute("SELECT name, category FROM statuses").fetchall():
        _remember(names, name, category)
    for key, fetched, raw in con.execute("SELECT key, fetched_at, json FROM raw_issues").fetchall():
        record = json.loads(raw)
        record.setdefault("key", key)
        record["_fetched"] = timestamp(fetched)
        record["_verified"] = record["_fetched"]
        evidence = observations.get(key)
        if (evidence and evidence[0] == record.get("fields", {}).get("updated")
                and evidence[1] and evidence[1] > record["_fetched"]):
            record["_verified"] = evidence[1]
        records[key] = record
        ids[str(record.get("id"))] = key
        fields = record.get("fields", {})
        kind = fields.get("issuetype") or {}
        _remember(types, kind.get("id"), kind.get("subtask"))
        status = fields.get("status") or {}
        category = (status.get("statusCategory") or {}).get("key")
        _remember(statuses, status.get("id"), category)
        _remember(names, status.get("name"), category)
    return {"records": records, "types": types, "statuses": statuses, "names": names, "ids": ids,
            "points": urd.resolve_field(con, "Story Points"),
            "sprint": urd.resolve_field(con, "Sprint"), "scope": urd.load_scope(con)}


def observe(con, key, updated, observed_at):
    con.execute("INSERT INTO capacity_observations VALUES (?, ?, ?) ON CONFLICT (key) "
                "DO UPDATE SET updated = excluded.updated, observed_at = excluded.observed_at",
                [key, updated, observed_at])


def remember_metadata(con, kind, identity, value):
    if identity is None or value is None:
        return
    old = con.execute("SELECT payload FROM capacity_metadata WHERE kind = ? AND id = ?",
                      [kind, str(identity)]).fetchone()
    if old and json.loads(old[0]) != value:
        value = None
    con.execute("INSERT INTO capacity_metadata VALUES (?, ?, ?) ON CONFLICT (kind, id) "
                "DO UPDATE SET payload = excluded.payload",
                [kind, str(identity), json.dumps(value)])


def _field(item, ctx):
    identity, name = item.get("fieldId"), str(item.get("field", "")).lower()
    if identity == ctx["points"] and identity:
        return "points"
    if identity == ctx["sprint"] and identity:
        return "sprints"
    if identity and name in ("story points", "sprint"):
        return None
    return {"story points": "points", "sprint": "sprints", "components": "components",
            "component": "components", "issuetype": "type", "parent": "parent",
            "issueparentassociation": "parent", "status": "status"}.get(name)


def events(record, ctx):
    log = record.get("changelog")
    if not isinstance(log, dict) or not isinstance(log.get("histories"), list):
        return [], False
    histories = log["histories"]
    complete = log.get("total") == len(histories)
    seen, result = {}, []
    for history in histories:
        identity = str(history.get("id"))
        canonical = json.dumps(history, sort_keys=True)
        if identity in seen:
            if seen[identity] != canonical:
                complete = False
            continue
        seen[identity] = canonical
        at = timestamp(history.get("created"))
        if at is None:
            complete = False
            continue
        for position, item in enumerate(history.get("items", [])):
            field = _field(item, ctx)
            if field:
                result.append((at, identity, position, field, item))
    result.sort(key=lambda e: (e[0], int(e[1]) if e[1].isdigit() else 0, e[1], e[2]))
    return result, complete


def _set(value):
    if value in (None, ""):
        return set()
    parts = str(value).replace(" ", "").split(",")
    if any(not part.isdigit() for part in parts):
        raise ValueError("Unknown sprint IDs")
    return {str(int(part)) for part in parts}


def _point(value):
    return capacity.number(value, "Points", missing=True)


def _parent(identity, label, ctx):
    if identity in (None, "") and not label:
        return None
    key = ctx["ids"].get(str(identity))
    if key:
        return key
    if label and "-" in label and " " not in label:
        return label
    raise ValueError("Unknown parent identity")


def _endpoint(item, side, field, ctx):
    identity, label = item.get(side), item.get(side + "String")
    if field == "points":
        return _point(label if label is not None else identity)
    if field == "sprints":
        return _set(identity)
    if field == "parent":
        return _parent(identity, label, ctx)
    if field == "type":
        if identity is None:
            raise ValueError("Unknown issue type")
        return str(identity)
    if field == "status":
        if identity is None and not label:
            raise ValueError("Unknown status")
        return (str(identity) if identity is not None else None, label)
    raise ValueError("Unknown history field")


def category(status, ctx):
    identity, name = status or (None, None)
    return ctx["statuses"].get(identity) if identity in ctx["statuses"] else ctx["names"].get(name)


def _initial(record, ctx):
    fields = record.get("fields") or {}
    status = fields.get("status") or {}
    issue_type = fields.get("issuetype") or {}
    state = {"key": record["key"], "created": timestamp(fields.get("created")),
             "observed_at": record["_verified"], "fetched_at": record["_fetched"],
             "unknown": set(), "points": None,
             "type": str(issue_type["id"]) if issue_type.get("id") is not None else None,
             "status": (str(status["id"]) if status.get("id") is not None else None,
                        status.get("name")), "components": {}, "sprints": set(), "parent": None}
    for field in FIELDS:
        try:
            if field == "points":
                if not ctx["points"] or ctx["points"] not in fields:
                    raise ValueError()
                state[field] = _point(fields[ctx["points"]])
            elif field == "sprints":
                if not ctx["sprint"]:
                    raise ValueError()
                sprints = fields[ctx["sprint"]]
                if sprints is not None and not isinstance(sprints, list):
                    raise ValueError()
                state[field] = {str(s["id"]) for s in sprints or []}
            elif field == "components":
                if not isinstance(fields.get("components"), list):
                    raise ValueError()
                state[field] = {str(c["id"]): c["name"] for c in fields["components"]}
            elif field == "parent":
                parent = fields.get("parent") or {}
                state[field] = _parent(parent.get("id"), parent.get("key"), ctx)
                if "parent" not in fields and issue_type.get("subtask"):
                    raise ValueError()
            elif ((field == "type" and state["type"] is None)
                  or (field == "status" and not any(state["status"]))):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            state["unknown"].add(field)
    return state


def state_at(record, ctx, cutoff, inclusive=True, observed=False):
    state = _initial(record, ctx)
    changes, complete = events(record, ctx)
    state["history_complete"] = complete
    if not complete:
        state["unknown"].update(FIELDS)
    if cutoff is None or state["created"] is None:
        state["unknown"].update(FIELDS)
    state["exists"] = (None if cutoff is None or state["created"] is None else
                       state["created"] <= cutoff if inclusive else state["created"] < cutoff)
    if not observed:
        if cutoff is None or record["_verified"] is None or record["_verified"] < cutoff:
            state["unknown"].update(FIELDS)
        for at, _, _, field, item in reversed(changes):
            if cutoff is not None and (at <= cutoff if inclusive else at < cutoff):
                continue
            if field in state["unknown"]:
                continue
            try:
                if field == "components":
                    before, after = item.get("from"), item.get("to")
                    if before is None and after is None:
                        raise ValueError()
                    if after is not None:
                        if str(after) not in state[field]:
                            raise ValueError()
                        del state[field][str(after)]
                    if before is not None:
                        if str(before) in state[field] or not item.get("fromString"):
                            raise ValueError()
                        state[field][str(before)] = item["fromString"]
                else:
                    before = _endpoint(item, "from", field, ctx)
                    after = _endpoint(item, "to", field, ctx)
                    matches = state[field] == after
                    if field == "status":
                        matches = (state[field][0] == after[0] if after[0] is not None else
                                   state[field][1] == after[1])
                    if not matches:
                        raise ValueError()
                    state[field] = before
            except (ValueError, TypeError, KeyError):
                state["unknown"].add(field)
    state["is_subtask"] = ctx["types"].get(state["type"])
    if state["is_subtask"] is None:
        state["unknown"].add("type")
    state["category"] = category(state["status"], ctx)
    if state["category"] is None:
        state["unknown"].add("status")
    inventory = (record.get("fields") or {}).get("subtasks")
    state["inventory_complete"] = isinstance(inventory, list) and all(
        child.get("key") in ctx["records"] for child in inventory)
    return state


def in_scope(state, settings, sprint_id):
    if state["exists"] is False:
        return False
    component = bool(set(state["components"].values()) & set(settings["components"]))
    member = str(sprint_id) in state["sprints"]
    if "components" not in state["unknown"] and not component:
        return False
    if "sprints" not in state["unknown"] and not member:
        return False
    if state["exists"] is None or {"components", "sprints"} & state["unknown"]:
        return None
    return component and member


def select_points(states):
    children = {}
    uncertain_parent = False
    uncertain_parents = set()
    for state in states.values():
        if state["exists"] is False:
            continue
        # An unknown parent could be anyone's subtask. An unknown type with a known parent
        # only makes that one parent uncertain; with no parent it cannot be a subtask.
        if "parent" in state["unknown"]:
            uncertain_parent = True
        elif "type" in state["unknown"] and state["parent"]:
            uncertain_parents.add(state["parent"])
        if state["is_subtask"] and "parent" not in state["unknown"] and state["parent"]:
            children.setdefault(state["parent"], []).append(state)
    for key, state in states.items():
        if state["is_subtask"] is True:
            state["point_source"] = True
            continue
        family = children.get(key, [])
        estimated = any("points" not in c["unknown"] and c["points"] is not None for c in family)
        unknown = (uncertain_parent or key in uncertain_parents or not state["inventory_complete"]
                   or any("points" in c["unknown"] for c in family))
        state["point_source"] = False if estimated else None if unknown else True
    for state in states.values():
        if state["is_subtask"] and state["points"] is None and "points" not in state["unknown"]:
            parent = states.get(state["parent"])
            if parent and parent["point_source"] is True and parent["points"] is not None:
                state["point_source"] = False
        if "type" in state["unknown"] or "parent" in state["unknown"]:
            state["point_source"] = None


def _public(state):
    return {k: sorted(v) if isinstance(v, set) else v.isoformat() if isinstance(v, datetime) else v
            for k, v in state.items()}


def _coverage(con, ctx, start=None):
    reasons = []
    if ctx["scope"].get("component"):
        reasons.append("The source mirror is restricted by component.")
    if con.execute("SELECT count(*) FROM sync_errors").fetchone()[0]:
        reasons.append("The mirror has unresolved sync errors.")
    pruned = con.execute("SELECT removed_at FROM capacity_pruned").fetchall()
    if any(start is None or timestamp(removed) is None or timestamp(removed) >= start
           for (removed,) in pruned):
        reasons.append("Issues have been pruned from this mirror.")
    since = ctx["scope"].get("earliest_since")
    if start and since and since[:10] > start.date().isoformat():
        reasons.append("The mirror starts after this sprint began.")
    if ctx["scope"].get("sync_started_at"):
        reasons.append("A source refresh is incomplete.")
    return reasons


def snapshot(con, settings, sprint, cutoff=None, inclusive=True, observed=False):
    ctx = context(con)
    at = timestamp(cutoff)
    states = {key: state_at(record, ctx, at, inclusive, observed)
              for key, record in ctx["records"].items()}
    select_points(states)
    reasons = _coverage(con, ctx, timestamp(sprint.get("startDate")))
    issues, unknown = {}, []
    for key, state in states.items():
        membership = in_scope(state, settings, sprint["id"])
        state["membership"] = membership
        if membership:
            issues[key] = _public(state)
        elif membership is None:
            unknown.append(key)
    points = sum(s["points"] for s in issues.values()
                 if s["point_source"] is True and "points" not in s["unknown"]
                 and s["points"] is not None)
    missing = [k for k, s in issues.items() if s["point_source"] is None or (
        s["point_source"] is True and ("points" in s["unknown"] or s["points"] is None))]
    return {"issues": issues, "unknown_membership": unknown, "known_points": points,
            "unestimated": missing, "points_complete": not (missing or unknown or reasons),
            "cutoff": cutoff, "inclusive": inclusive, "source_id": source_id(con),
            "sync_completed_at": ctx["scope"].get("last_sync_at"), "coverage": reasons,
            "coherent": bool(ctx["scope"].get("derived_sync_at")) and
            ctx["scope"].get("derived_sync_at") == ctx["scope"].get("last_sync_at") and
            not ctx["scope"].get("sync_started_at")}


def baseline(con, payload, now=None):
    sprint = capacity.sprint(con, payload["sprint_id"]) or payload["sprint"]
    current = timestamp(now or capacity.now())
    close, start = timestamp(sprint.get("completeDate")), timestamp(sprint.get("startDate"))
    retrospective = bool(close and current >= close) or sprint.get("state") == "closed"
    cutoff = (sprint.get("startDate") if retrospective else
              urd.load_scope(con).get("derived_sync_at"))
    result = snapshot(con, payload["settings"], sprint, cutoff, observed=not retrospective)
    result.update(kind="retrospective" if retrospective else "observed",
                  late=bool(start and current > start), sprint=sprint)
    return result


def completion(record, state, ctx, start, cutoff, inclusive=False):
    if start is None or cutoff is None or "status" in state["unknown"]:
        return None
    if state["category"] != "done":
        return False
    changes, complete = events(record, ctx)
    uncertain, qualified, current = not complete, False, state["status"]
    for at, _, _, field, item in reversed(changes):
        if field != "status" or at < start or (at > cutoff if inclusive else at >= cutoff):
            continue
        try:
            before = _endpoint(item, "from", "status", ctx)
            after = _endpoint(item, "to", "status", ctx)
        except ValueError:
            return None
        if (current[0] != after[0] if after[0] is not None else current[1] != after[1]):
            return None
        before_category, after_category = category(before, ctx), category(after, ctx)
        if before_category is None or after_category is None:
            uncertain = True
        elif before_category != "done" and after_category == "done":
            qualified = True
        current = before
    created = state["created"]
    if created is not None and start <= created and (created <= cutoff if inclusive else
                                                     created < cutoff):
        initial = state_at(record, ctx, created)
        if "status" in initial["unknown"]:
            uncertain = True
        elif initial["category"] == "done":
            qualified = True
    return None if uncertain else qualified


def _first_entry(record, ctx, settings, sprint_id, after, before, inclusive):
    if after is None or before is None:
        return None
    candidates = [record.get("fields", {}).get("created")]
    candidates += [e[0] for e in events(record, ctx)[0] if e[3] in ("components", "sprints")]
    for at in sorted({timestamp(t) for t in candidates if timestamp(t) is not None}):
        if at <= after or (at > before if inclusive else at >= before):
            continue
        previous = state_at(record, ctx, at, inclusive=False)
        following = state_at(record, ctx, at)
        if (in_scope(previous, settings, sprint_id) is False
                and in_scope(following, settings, sprint_id) is True):
            return following
    return None


def review(con, payload, baseline):
    ctx = context(con)
    sprint = capacity.sprint(con, payload["sprint_id"]) or payload["sprint"]
    closed = sprint.get("state") == "closed"
    cutoff_text = sprint.get("completeDate") if closed else ctx["scope"].get("derived_sync_at")
    cutoff, start = timestamp(cutoff_text), timestamp(sprint.get("startDate"))
    baseline_at = timestamp(baseline.get("cutoff"))
    states = {key: state_at(record, ctx, cutoff, inclusive=not closed, observed=not closed)
              for key, record in ctx["records"].items()}
    select_points(states)
    coverage = _coverage(con, ctx, start)
    if start is None or cutoff is None:
        coverage.append("Actual sprint boundaries are unavailable.")
    original = baseline.get("issues", {})
    outcomes = {name: [] for name in ("removed", "transferred_out", "dropped", "unfinished",
                                      "delivered", "already_completed", "unknown")}
    result = {"tickets": {}, "original_outcomes": outcomes,
              "original_counts": {"main": 0, "subtask": 0},
              "delivered_counts": {"main": 0, "subtask": 0}, "delivered_points": 0,
              "points_complete": not coverage, "coverage": coverage, "cutoff": cutoff_text,
              "original_delivered": [], "added": [], "added_delivered": [],
              "unclassified_delivered": [], "attribution_complete": True,
              "closed": closed, "unestimated": []}
    for old in original.values():
        if old.get("is_subtask") is not None:
            result["original_counts"]["subtask" if old["is_subtask"] else "main"] += 1
    dropped = {s.strip() for s in (ctx["scope"].get("abandoned_status") or "").split(",") if s}
    for key in sorted(set(states) | set(original)):
        state, record = states.get(key), ctx["records"].get(key)
        old = original.get(key)
        if state is None:
            if old:
                outcomes["unknown"].append(key)
                result["attribution_complete"] = False
                result["points_complete"] = False
            continue
        membership = in_scope(state, payload["settings"], sprint["id"])
        past = state_at(record, ctx, baseline_at, inclusive=baseline.get("inclusive", True))
        past_member = in_scope(past, payload["settings"], sprint["id"])
        baseline_conflict = bool(old and (
            past_member is not True or any(
                field in past["unknown"] or (
                    field not in old.get("unknown", []) and old.get(field) != past[field])
                for field in ("points", "type", "parent", "components"))))
        if baseline_conflict:
            result["attribution_complete"] = False
        cohort = "original" if old else "unclassified"
        entry = old
        if not old and past_member is False:
            first = _first_entry(record, ctx, payload["settings"], sprint["id"],
                                 baseline_at, cutoff, not closed)
            if first:
                cohort, entry = "added", _public(first)
                result["added"].append(key)
        relevant = (bool(old) or membership is not False or past_member is not False
                    or cohort == "added")
        if not relevant:
            continue
        if cohort == "unclassified":
            result["attribution_complete"] = False
        qualifies = completion(record, state, ctx, start, cutoff, inclusive=not closed)
        abandoned = state["status"][1] in dropped
        delivered = membership is True and qualifies is True and not abandoned
        if old:
            if "sprints" not in state["unknown"] and str(sprint["id"]) not in state["sprints"]:
                disposition = "removed"
            elif ("components" not in state["unknown"]
                  and not set(state["components"].values()) &
                  set(payload["settings"]["components"])):
                disposition = "transferred_out"
            elif membership is None or qualifies is None:
                disposition = "unknown"
            elif abandoned:
                disposition = "dropped"
            elif state["category"] != "done":
                disposition = "unfinished"
            elif delivered:
                disposition = "delivered"
            else:
                disposition = "already_completed"
            outcomes[disposition].append(key)
        if membership is None or (membership and (qualifies is None or "type" in state["unknown"])):
            result["points_complete"] = False
        if delivered:
            if state["is_subtask"] is not None:
                result["delivered_counts"]["subtask" if state["is_subtask"] else "main"] += 1
            result[cohort + "_delivered"].append(key)
            if state["point_source"] is None or (state["point_source"] is True and (
                    state["points"] is None or "points" in state["unknown"])):
                result["unestimated"].append(key)
                result["points_complete"] = False
            elif state["point_source"] is True:
                result["delivered_points"] += state["points"]
        before_points = (entry.get("points") if entry and
                         "points" not in entry.get("unknown", []) else None)
        after_points = state["points"] if "points" not in state["unknown"] else None
        result["tickets"][key] = {
            **_public(state), "cohort": cohort, "delivered": delivered, "membership": membership,
            "entry_points": before_points, "closing_points": after_points,
            "estimate_change": (after_points - before_points if
                                before_points is not None and after_points is not None else None),
            "baseline_conflict": baseline_conflict,
            "converted": bool(old and old.get("is_subtask") != state["is_subtask"]),
            "point_source_changed": bool(old and old.get("point_source") != state["point_source"]),
        }
    result["rate_eligible"] = closed and result["points_complete"]
    return result
