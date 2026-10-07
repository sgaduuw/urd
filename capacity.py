"""Local team availability and immutable sprint plan revisions."""
import copy
import json
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS capacity_observations (
    key VARCHAR PRIMARY KEY, updated VARCHAR NOT NULL, observed_at VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS capacity_metadata (
    kind VARCHAR, id VARCHAR, payload VARCHAR, PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS capacity_pruned (key VARCHAR PRIMARY KEY, removed_at VARCHAR);
CREATE TABLE IF NOT EXISTS capacity_teams (
    id VARCHAR PRIMARY KEY, version INTEGER NOT NULL, payload VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS capacity_sprints (
    id BIGINT PRIMARY KEY, payload VARCHAR NOT NULL, fetched_at VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS capacity_boards (
    id BIGINT PRIMARY KEY, payload VARCHAR NOT NULL, fetched_at VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS capacity_plans (
    team_id VARCHAR, sprint_id BIGINT, version INTEGER NOT NULL,
    confirmation INTEGER, PRIMARY KEY (team_id, sprint_id)
);
CREATE TABLE IF NOT EXISTS capacity_revisions (
    team_id VARCHAR, sprint_id BIGINT, version INTEGER, created_at VARCHAR NOT NULL,
    action VARCHAR NOT NULL, reason VARCHAR NOT NULL, confirmation INTEGER,
    payload VARCHAR NOT NULL, PRIMARY KEY (team_id, sprint_id, version)
);
"""


class Conflict(ValueError):
    """An older form cannot overwrite a newer saved value."""


def now():
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017


def init_db(con):
    con.execute(SCHEMA)


def _json(value):
    return json.dumps(value, allow_nan=False, sort_keys=True)


def number(value, label, maximum=None, missing=False):
    if value is None or value == "":
        if missing:
            return None
        raise ValueError(f"Missing {label}.")
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number.")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not math.isfinite(result) or result < 0 or (maximum is not None and result > maximum):
        limit = f" between 0 and {maximum}" if maximum is not None else " at least 0"
        raise ValueError(f"{label} must be finite and{limit}.")
    return result


def _split(value, label="Daily"):
    if not isinstance(value, dict):
        raise ValueError(f"Enter {label.lower()} Work and Meetings hours.")
    result = {field: number(value.get(field), f"{label} {field} hours", 24, missing=True)
              for field in ("work", "meetings")}
    if None not in result.values() and sum(result.values()) > 24:
        raise ValueError(f"{label} Work + Meetings hours must not exceed 24.")
    return result


def _id(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise ValueError("A positive Jira ID is required.")
    return int(value)


def days(start, end):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        raise ValueError("Enter valid start and last dates.") from None
    if last < first:
        raise ValueError("Last date cannot precede start date.")
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def _members(members, patterns=False):
    result, identities = [], set()
    for entry in members:
        member = copy.deepcopy(entry)
        member["id"] = str(member.get("id") or uuid.uuid4())
        member["name"] = str(member.get("name") or "").strip()
        if not member["name"] or member["id"] in identities:
            raise ValueError("Each member needs a name and distinct identity.")
        identities.add(member["id"])
        if patterns:
            if len(member.get("weekly", [])) != 7:
                raise ValueError("Enter a normal pattern for all seven days.")
            member["weekly"] = [_split(v, "Weekly") for v in member["weekly"]]
        result.append(member)
    return result


def teams(con):
    return [dict(json.loads(payload), id=identity, version=version)
            for identity, version, payload in con.execute(
                "SELECT id, version, payload FROM capacity_teams ORDER BY id").fetchall()]


def get_team(con, team_id):
    return next((team for team in teams(con) if team["id"] == team_id), None)


def save_team(con, data, team_id=None, expected_version=0):
    value = copy.deepcopy(data)
    value["name"] = str(value.get("name") or "").strip()
    value["components"] = sorted(set(str(c).strip() for c in value.get("components", [])
                                     if str(c).strip()))
    if not value["name"] or not value["components"]:
        raise ValueError("A team name and at least one component are required.")
    try:
        ZoneInfo(value.get("timezone", ""))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("Choose a valid planning timezone.") from None
    value["boards"] = sorted({_id(b) for b in value.get("boards", [])})
    value["members"] = _members(value.get("members", []), patterns=True)
    identity = team_id or str(uuid.uuid4())
    con.execute("BEGIN")
    try:
        old = con.execute("SELECT version FROM capacity_teams WHERE id = ?", [identity]).fetchone()
        if (old[0] if old else 0) != expected_version:
            raise Conflict("This team changed. Review the latest version before saving.")
        value.update(id=identity, version=expected_version + 1)
        con.execute("INSERT INTO capacity_teams VALUES (?, ?, ?) ON CONFLICT (id) "
                    "DO UPDATE SET version = excluded.version, payload = excluded.payload",
                    [identity, value["version"], _json(value)])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return value


def cache_sprint(con, value):
    identity = _id(value["id"])
    con.execute("INSERT INTO capacity_sprints VALUES (?, ?, ?) ON CONFLICT (id) "
                "DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
                [identity, _json(value), now()])


def sprint(con, sprint_id):
    row = con.execute("SELECT payload, fetched_at FROM capacity_sprints WHERE id = ?",
                      [_id(sprint_id)]).fetchone()
    return dict(json.loads(row[0]), fetched_at=row[1]) if row else None


def sprints(con, board_ids=None):
    result = [dict(json.loads(payload), fetched_at=fetched)
              for payload, fetched in con.execute(
                  "SELECT payload, fetched_at FROM capacity_sprints ORDER BY id DESC").fetchall()]
    if board_ids is not None:
        result = [s for s in result if set(s.get("boards", [])) & set(board_ids)]
    return result


def new_plan(con, team_id, sprint_id, start, end):
    team = get_team(con, team_id)
    selected = sprint(con, sprint_id)
    if team is None or selected is None:
        raise ValueError("Choose an existing team and cached Jira sprint.")
    calendar = days(start, end)
    members = []
    for member in team["members"]:
        members.append({
            "id": member["id"], "name": member["name"],
            "account_id": member.get("account_id"),
            "daily": {day: copy.deepcopy(member["weekly"][date.fromisoformat(day).weekday()])
                      for day in calendar},
        })
    return {"team_id": team_id, "sprint_id": _id(sprint_id), "settings": copy.deepcopy(team),
            "sprint": selected, "dates": {"start": start, "end": end}, "members": members,
            "focus": None, "rate": {"kind": "none", "value": None, "sources": []},
            "version": 0, "confirmation": None}


def totals(payload, complete=False):
    calendar = days(payload["dates"]["start"], payload["dates"]["end"])
    members = _members(payload.get("members", []))
    missing, rows, available, work = [], [], 0.0, 0.0
    for member in members:
        if not isinstance(member.get("daily"), dict):
            raise ValueError("Enter daily Work and Meetings hours.")
        # Validate retained dates too, but only count the planning range.
        cells = {day: _split(cell) for day, cell in member["daily"].items()}
        values = [cells.get(day, {"work": None, "meetings": None}) for day in calendar]
        incomplete = any(None in cell.values() for cell in values)
        subtotal = sum(v for cell in values for v in cell.values() if v is not None)
        row_work = None if incomplete else sum(cell["work"] for cell in values)
        meetings = (None if any(cell["meetings"] is None for cell in values)
                    else sum(cell["meetings"] for cell in values))
        if incomplete:
            missing.append(member["name"])
        rows.append({"id": member["id"], "available_hours": subtotal,
                     "work_hours": row_work, "meetings": meetings})
        available += subtotal
        work += row_work or 0
    focus = number(payload.get("focus"), "Focus percentage", 100, missing=True)
    if focus is None:
        missing.append("focus percentage")
    if complete and missing:
        raise ValueError("Missing inputs: " + ", ".join(missing))
    return {"available_hours": available, "work_hours": None if missing else work,
            "focus_hours": None if missing else work * focus / 100, "missing": missing,
            "members": rows}


def history_rate(sources):
    if not sources:
        return {"kind": "none", "value": None, "sources": []}
    points, hours = 0.0, 0.0
    for source in sources:
        contribution = number(source.get("focus_hours"), "Historical focus hours")
        if contribution <= 0:
            raise ValueError("Historical focus hours must be positive.")
        points += number(source.get("points"), "Historical points")
        hours += contribution
    return {"kind": "history", "value": points / hours, "sources": copy.deepcopy(sources)}


def forecast(focus_hours, rate):
    kind = rate.get("kind", "none")
    if kind not in ("none", "manual", "history"):
        raise ValueError("Choose a manual or historical rate.")
    value = number(rate.get("value"), "Rate", missing=True)
    if kind == "none" or value is None or focus_hours is None:
        return None
    return number(focus_hours, "Focus hours") * value


def get_plan(con, team_id, sprint_id, version=None):
    if version is None:
        row = con.execute("SELECT version FROM capacity_plans WHERE team_id = ? AND sprint_id = ?",
                          [team_id, _id(sprint_id)]).fetchone()
        if not row:
            return None
        version = row[0]
    row = con.execute(
        "SELECT payload, created_at, action, reason, confirmation FROM capacity_revisions "
        "WHERE team_id = ? AND sprint_id = ? AND version = ?",
        [team_id, _id(sprint_id), version]).fetchone()
    if not row:
        return None
    return dict(json.loads(row[0]), version=version, created_at=row[1], action=row[2],
                reason=row[3], confirmation=row[4])


def revisions(con, team_id, sprint_id):
    versions = con.execute("SELECT version FROM capacity_revisions WHERE team_id = ? "
                           "AND sprint_id = ? ORDER BY version",
                           [team_id, _id(sprint_id)]).fetchall()
    return [get_plan(con, team_id, sprint_id, version) for (version,) in versions]


def _replacement(con, link):
    source = get_plan(con, link["team_id"], link["sprint_id"], link["confirmation"])
    history = revisions(con, link["team_id"], link["sprint_id"])
    if source is None or source["action"] != "confirm" or not any(
        r["action"] == "void" and r.get("voids") == link["confirmation"] for r in history
    ):
        raise ValueError("A replacement must refer to a voided confirmation.")
    return dict(team_id=source["team_id"], sprint_id=source["sprint_id"],
                confirmation=source["version"])


def save_plan(con, payload, expected_version, action="save", reason="", baseline=None,
              replaces=None):
    if action not in ("save", "confirm", "void"):
        raise ValueError("Unknown plan action.")
    team_id, sprint_id = payload["team_id"], _id(payload["sprint_id"])
    reason = reason.strip()
    con.execute("BEGIN")
    try:
        old = get_plan(con, team_id, sprint_id)
        version = old["version"] if old else 0
        if version != expected_version:
            raise Conflict("This plan changed. Review the latest version before saving.")
        confirmation = old["confirmation"] if old else None
        if (confirmation is not None or action == "void") and not reason:
            raise ValueError("A reason is required after confirmation.")
        if action == "confirm" and confirmation is not None:
            raise ValueError("This plan already has a valid confirmation.")
        if action == "void" and confirmation is None:
            raise ValueError("There is no valid confirmation to void.")
        value = {k: copy.deepcopy(v) for k, v in (old if action == "void" else payload).items()
                 if k not in ("version", "created_at", "action", "reason", "confirmation",
                              "baseline", "voids", "totals", "forecast")}
        if get_team(con, team_id) is None or sprint(con, sprint_id) is None:
            raise ValueError("Choose an existing team and cached Jira sprint.")
        if confirmation is not None:
            for field in ("components", "timezone", "id"):
                if value["settings"].get(field) != old["settings"].get(field):
                    raise ValueError("Void the confirmation before replacing its team scope.")
        value["totals"] = totals(value, complete=action == "confirm" or confirmation is not None)
        rate = value.get("rate", {})
        if rate.get("kind") == "history":
            if (not rate.get("sources")
                    or history_rate(rate["sources"])["value"] != rate.get("value")):
                raise ValueError("The recorded rate does not match its historical source values.")
            if not old or rate != old.get("rate"):
                for source in rate["sources"]:
                    current = get_plan(con, source["team_id"], source["sprint_id"])
                    if current is None or current["confirmation"] != source.get("confirmation"):
                        raise ValueError("A selected historical source was voided or replaced.")
        value["forecast"] = forecast(value["totals"]["focus_hours"], value.get("rate", {}))
        if action == "confirm":
            if baseline is None or not baseline.get("coherent"):
                raise ValueError("Confirmation needs a coherent Jira preview.")
            value["baseline"] = copy.deepcopy(baseline)
            confirmation = version + 1
            if replaces is None and old:
                voids = [r for r in revisions(con, team_id, sprint_id) if r["action"] == "void"]
                if voids:
                    replaces = {"team_id": team_id, "sprint_id": sprint_id,
                                "confirmation": voids[-1]["voids"]}
            if replaces:
                value["replaces"] = _replacement(con, replaces)
        if action == "void":
            # A void copies the confirmed plan; the link it carried belongs to that confirmation.
            value.pop("replaces", None)
            value["voids"] = confirmation
            confirmation = None
        timestamp = now()
        con.execute("INSERT INTO capacity_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [team_id, sprint_id, version + 1, timestamp, action, reason, confirmation,
                     _json(value)])
        con.execute("INSERT INTO capacity_plans VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (team_id, sprint_id) DO UPDATE SET version = excluded.version, "
                    "confirmation = excluded.confirmation",
                    [team_id, sprint_id, version + 1, confirmation])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return get_plan(con, team_id, sprint_id)


def boards(con):
    return [dict(json.loads(payload), fetched_at=fetched)
            for payload, fetched in con.execute(
                "SELECT payload, fetched_at FROM capacity_boards ORDER BY id").fetchall()]


def refresh_catalogue(con, jira, project, board_ids=None):
    found = {}
    for project_key in (p.strip() for p in project.split(",") if p.strip()):
        for board in jira.boards(project_key):
            found[board["id"]] = board
    selected = {_id(b) for b in (board_ids or [])}
    if selected - found.keys():
        raise ValueError("A selected board is no longer visible. Cached plans have been retained.")
    discovered = {}
    for board_id in sorted(selected):
        for record in jira.sprints(board_id):
            record = copy.deepcopy(record)
            for field in ("startDate", "endDate", "completeDate"):
                if record.get(field):
                    try:
                        parsed = datetime.fromisoformat(record[field])
                        if parsed.tzinfo is None:
                            raise ValueError()
                    except (TypeError, ValueError):
                        raise ValueError("Jira returned an invalid sprint date.") from None
            identity = record["id"]
            if identity in discovered:
                old = discovered[identity]
                if any(old.get(f) != record.get(f) for f in
                       ("name", "state", "startDate", "endDate", "completeDate")):
                    raise ValueError("Sprint metadata changed during discovery. Retry the refresh.")
                old["boards"].append(board_id)
            else:
                discovered[identity] = dict(record, boards=[board_id])
    fetched = now()
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM capacity_boards")
        for identity, board in found.items():
            con.execute("INSERT INTO capacity_boards VALUES (?, ?, ?)",
                        [identity, _json(board), fetched])
        # Keep sprint identities referenced by saved plans, even if a board loses them.
        for existing in sprints(con):
            identity = existing["id"]
            remaining = set(existing.get("boards", [])) - selected
            if identity in discovered:
                discovered[identity]["boards"] = sorted(
                    remaining | set(discovered[identity]["boards"]))
            elif selected & set(existing.get("boards", [])):
                existing["boards"] = sorted(remaining)
                existing.pop("fetched_at", None)
                con.execute("UPDATE capacity_sprints SET payload = ? WHERE id = ?",
                            [_json(existing), identity])
        for identity, record in discovered.items():
            con.execute("INSERT INTO capacity_sprints VALUES (?, ?, ?) ON CONFLICT (id) "
                        "DO UPDATE SET payload = excluded.payload, "
                        "fetched_at = excluded.fetched_at",
                        [identity, _json(record), fetched])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return len(discovered)


def history_candidates(con, target):
    import capacity_history as history

    result = []
    target_sprint = sprint(con, target["sprint_id"]) or target["sprint"]
    target_start = history.timestamp(target_sprint.get("startDate"))
    if target_start is None:
        target_start = datetime.fromisoformat(target["dates"]["start"]).replace(
            tzinfo=ZoneInfo(target["settings"]["timezone"]))
    rows = con.execute("SELECT sprint_id FROM capacity_plans WHERE team_id = ? AND sprint_id != ?",
                       [target["team_id"], target["sprint_id"]]).fetchall()
    for (sprint_id,) in rows:
        value = get_plan(con, target["team_id"], sprint_id)
        metadata = sprint(con, sprint_id)
        hours = totals(value)["focus_hours"]
        closed_at = history.timestamp(metadata.get("completeDate"))
        reason, points, kind = "", None, None
        if value["confirmation"] is None:
            reason = "Voided or not confirmed."
        elif metadata.get("state") != "closed" or closed_at is None:
            reason = "Sprint is not closed with a known closure."
        elif closed_at > target_start:
            reason = "This is not a previous sprint."
        elif hours is None or hours <= 0:
            reason = "Revised focus hours must be positive and complete."
        else:
            original = get_plan(con, value["team_id"], sprint_id, value["confirmation"])
            comparison = history.review(con, value, original["baseline"])
            kind = original["baseline"]["kind"]
            points = comparison["delivered_points"]
            if not comparison["rate_eligible"]:
                reason = ("; ".join(comparison["coverage"])
                          or "Delivery or point evidence is incomplete.")
        result.append({
            "team_id": value["team_id"], "sprint_id": sprint_id, "version": value["version"],
            "confirmation": value["confirmation"], "points": points, "focus_hours": hours,
            "cutoff": metadata.get("completeDate"), "kind": kind,
            "label": metadata["name"], "eligible": not reason,
            "reason": reason or f"{points:g} points / {hours:g} focus hours; revision "
                               f"{value['version']}; {kind}; closed {metadata.get('completeDate')}",
        })
    return result


def _rate_warnings(con, rate):
    warnings = []
    for source in rate.get("sources", []):
        current = get_plan(con, source["team_id"], source["sprint_id"])
        if current is None or current["confirmation"] != source["confirmation"]:
            warnings.append(f"Rate source sprint {source['sprint_id']} was voided or replaced. "
                            "The recorded rate and forecast are unchanged.")
        elif current["version"] != source["version"]:
            warnings.append(f"Rate source sprint {source['sprint_id']} was later revised. "
                            "The recorded source values are unchanged.")
    return warnings


def report_section(con, include_grid=False, plan=None):
    import capacity_history as history
    import render
    import urd

    def display(value):
        return "unavailable" if value is None else (
            f"{value:.4g}" if isinstance(value, float) else render.esc(value))

    if plan is None:
        plans = [get_plan(con, tid, sid) for tid, sid in con.execute(
            "SELECT team_id, sprint_id FROM capacity_plans WHERE confirmation IS NOT NULL "
            "ORDER BY team_id, sprint_id").fetchall()]
    else:
        plans = [plan]
    sections = []
    scope = urd.load_scope(con)
    filtered = set(urd.stored_report_components(con))
    epics = urd.stored_excluded_epics(con)
    for value in plans:
        if value.get("confirmation") is None:
            continue
        if (plan is None and scope.get("report_since")
                and value["dates"]["end"] < scope["report_since"]):
            continue
        original = get_plan(con, value["team_id"], value["sprint_id"], value["confirmation"])
        baseline = original["baseline"]
        comparison = history.review(con, value, baseline)
        current = totals(value)
        name = value["sprint"]["name"]
        body = f'<h3>{render.esc(value["settings"]["name"])} · {render.esc(name)}</h3>'
        body += (f'<p>{render.esc(", ".join(value["settings"]["components"]))}; '
                 f'{render.esc(value["settings"]["timezone"])}; planning dates '
                 f'{value["dates"]["start"]} through {value["dates"]["end"]}. '
                 f'{render.esc(baseline["kind"])}'
                 f'{"; late confirmation" if baseline.get("late") else ""}.</p>')
        body += '<table class="urd"><tr><th>Measure</th><th>Original</th><th>Revised</th></tr>'
        for label, first, latest in (
            ("Available hours", original["totals"]["available_hours"], current["available_hours"]),
            ("Work hours excluding meetings", original["totals"]["work_hours"],
             current["work_hours"]),
            ("Planned focus hours", original["totals"]["focus_hours"], current["focus_hours"]),
            ("Forecast points", original.get("forecast"),
             forecast(current["focus_hours"], value.get("rate", {}))),
        ):
            body += f"<tr><th>{label}</th><td>{display(first)}</td><td>{display(latest)}</td></tr>"
        body += "</table>"
        body += (f'<p>Commitment: {display(baseline.get("known_points"))} known points; '
                 f'{len(baseline.get("unestimated", []))} unestimated or uncertain tickets. '
                 f'Delivered: {display(comparison["delivered_points"])} known points'
                 f'{" (partial evidence)" if not comparison["points_complete"] else ""}. '
                 f'Main tickets: {comparison["delivered_counts"]["main"]}; '
                 f'subtasks: {comparison["delivered_counts"]["subtask"]}.</p>')
        body += (f'<p>Original delivery: {len(comparison["original_delivered"])} tickets; '
                 f'added delivery: {len(comparison["added_delivered"])}; '
                 f'unclassified delivery: {len(comparison["unclassified_delivered"])}. '
                 f'Added work: {len(comparison["added"])}.</p>')
        if not comparison["attribution_complete"]:
            body += '<p class="warn">Original/added attribution is incomplete.</p>'
        body += "<p>Original outcomes: " + render.esc(", ".join(
            f'{key.replace("_", " ")} {len(keys)}'
            for key, keys in comparison["original_outcomes"].items())) + ".</p>"
        rate = value.get("rate", {})
        body += (f'<p>Rate: {display(rate.get("value"))} points per focus hour '
                 f'({render.esc(rate.get("kind", "none"))}). Forecasts are planning estimates.</p>')
        for source in rate.get("sources", []):
            body += (f'<p>Rate source sprint {source["sprint_id"]}, capacity revision '
                     f'{source["version"]}, confirmation {source["confirmation"]}: '
                     f'{display(source["points"])} points / {display(source["focus_hours"])} '
                     f'focus hours; cutoff {display(source.get("cutoff"))}.</p>')
        warnings = _rate_warnings(con, rate) + comparison["coverage"] + baseline.get("coverage", [])
        for warning in warnings:
            body += f'<p class="warn">{render.esc(warning)}</p>'
        others = [get_plan(con, tid, value["sprint_id"]) for (tid,) in con.execute(
            "SELECT team_id FROM capacity_plans WHERE sprint_id = ? AND team_id != ? "
            "AND confirmation IS NOT NULL", [value["sprint_id"], value["team_id"]]).fetchall()]
        overlaps = any(set(other["settings"]["components"]) & (
            set(value["settings"]["components"]) | {
                name for ticket in comparison["tickets"].values() if ticket["delivered"]
                for name in ticket["components"].values()}) for other in others)
        if overlaps:
            body += ('<p class="warn">Team scopes overlap. These totals must not be added '
                     'as unique project delivery.</p>')
        body += (f'<p>Baseline cutoff: {display(baseline.get("cutoff"))}. '
                 f'Confirmed: {display(original["created_at"])}. '
                 f'Revision {value["version"]}: {display(value["created_at"])}. '
                 f'Review cutoff: {display(comparison["cutoff"])}. '
                 f'Sync completed: {display(baseline.get("sync_completed_at"))}. '
                 'Issue observations may differ from sync completion.</p>')
        if include_grid:
            calendar = days(value["dates"]["start"], value["dates"]["end"])
            body += '<table class="urd"><tr><th>Person</th>'
            body += "".join(f"<th>{day}</th>" for day in calendar) + "</tr>"
            for member in value["members"]:
                body += f'<tr><th>{render.esc(member["name"])}</th>'
                for day in calendar:
                    cell = member["daily"].get(day, {})
                    body += (f'<td>Work: {display(cell.get("work"))} h<br>'
                             f'Meetings: {display(cell.get("meetings"))} h</td>')
                body += "</tr>"
            body += "</table>"
        incompatible = plan is None and (epics or (
            filtered and not set(value["settings"]["components"]).issubset(filtered)))
        if incompatible:
            anchor = f'capacity-{value["team_id"]}-{value["sprint_id"]}'
            body = (f'<p>Report filters differ from this saved plan. <a href="#{anchor}">'
                    f'View full capacity comparison for {render.esc(name)}</a>.</p>'
                    f'<details id="{anchor}"><summary>Full saved team scope</summary>'
                    f'{body}</details>')
        sections.append(body)
    return "".join(sections)
