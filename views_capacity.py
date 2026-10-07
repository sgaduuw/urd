"""Native local forms for team and sprint capacity planning."""
import copy
import hashlib
import json
import uuid
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import flask

import capacity
import capacity_history as history
import render
import urd
import webapp

bp = flask.Blueprint("capacity", __name__)

GRID_CSS = """
.capacity-scroll { overflow: auto; max-height: 65vh; margin: 16px 0; }
.capacity-grid thead th { position: sticky; top: 0; background: var(--surface); z-index: 1; }
.capacity-grid tbody th { position: sticky; background: var(--surface); left: 0; z-index: 2; }
.capacity-grid input { width: 5.5rem; color: var(--text-primary);
    background: var(--surface); border: 1px solid var(--border); padding: 6px; }
.capacity-grid input:focus { outline: 2px solid var(--s1); }
.capacity-grid td label { display: flex; align-items: center; gap: 6px;
    justify-content: space-between; white-space: nowrap; }
.capacity-actions { display: flex; gap: 8px; flex-wrap: wrap; }
"""


def _project(slug):
    project = webapp.slug_or_404(flask.current_app.config["REGISTRY"], slug)
    if project.con is None or not project.configured():
        flask.abort(404)
    return project


def _page(slug, title, body, message=""):
    notice = f'<p class="warn" role="alert">{render.esc(message)}</p>' if message else ""
    nav = (f'<nav><a href="/{slug}/">Report</a> · '
           f'<a href="/{slug}/capacity/">Capacity</a></nav>')
    return render.notice(title, []).replace("</style>", GRID_CSS + "</style>", 1).replace(
        "</body>", nav + notice + body + "</body>", 1)


def _input(name, value="", kind="text", label=None, extra=""):
    value = "" if value is None else value
    element = (f'<input type="{kind}" name="{render.esc(name)}" '
               f'value="{render.esc(value)}" {extra}>')
    return f"<label>{render.esc(label)} {element}</label>" if label else element


def _hidden(value):
    return _input("payload", json.dumps(value, allow_nan=False), "hidden")


def _payload():
    try:
        value = json.loads(flask.request.form.get("payload", "{}"))
    except (ValueError, TypeError):
        raise ValueError("The form data is invalid. Reload the saved plan.") from None
    if not isinstance(value, dict):
        raise ValueError("The form data is invalid.")
    return value


def _people(con):
    if not con.execute("SELECT count(*) FROM information_schema.tables "
                       "WHERE table_name = 'people'").fetchone()[0]:
        return {}
    return dict(con.execute("SELECT account_id, display_name FROM people ORDER BY display_name")
                .fetchall())


def _add_member(con, value, patterns=False):
    name = flask.request.form.get("new_name", "").strip()
    account = flask.request.form.get("new_account", "")
    if account:
        people = _people(con)
        if account not in people:
            raise ValueError("Choose a person already known to this project.")
        member = {"id": "jira:" + account, "name": people[account], "account_id": account}
    elif name:
        member = {"id": str(uuid.uuid4()), "name": name}
    else:
        raise ValueError("Choose a Jira person or enter a local name.")
    if any(m["id"] == member["id"] for m in value["members"]):
        raise ValueError("This person is already in the roster.")
    member.update({"weekly": [{"work": None, "meetings": None} for _ in range(7)]}
                  if patterns else {"daily": {}})
    value["members"].append(member)


def _member_picker(con):
    options = '<option value="">Choose a Jira person (optional)</option>' + "".join(
        f'<option value="{render.esc(account)}">{render.esc(name)}</option>'
        for account, name in _people(con).items())
    return ('<fieldset><legend>Add a person</legend><label>Jira person '
            f'<select name="new_account">{options}</select></label>'
            + _input("new_name", label="Or local display name")
            + '<button name="action" value="add_member">Add person</button></fieldset>')


@bp.get("/<slug>/capacity/")
def index(slug):
    project = _project(slug)
    with project.con.cursor() as con:
        body = ('<p>Plan availability per team and sprint. Jira access stays read only.</p>'
                f'<p><a href="/{slug}/capacity/teams/new">Add team</a></p>'
                f'<form method="post" action="/{slug}/capacity/catalogue">'
                '<button>Discover Scrum boards</button></form>')
        for team in capacity.teams(con):
            tid = team["id"]
            body += (f'<h2>{render.esc(team["name"])}</h2>'
                     f'<p><a href="/{slug}/capacity/teams/{tid}">Team and normal hours</a></p>'
                     f'<form method="post" action="/{slug}/capacity/catalogue">'
                     + _input("team_id", tid, "hidden")
                     + '<button>Refresh selected boards and sprints</button></form>')
            selected = {s["id"]: s for s in capacity.sprints(con, team["boards"])}
            for (sid,) in con.execute("SELECT sprint_id FROM capacity_plans WHERE team_id = ?",
                                      [tid]).fetchall():
                selected.setdefault(sid, capacity.sprint(con, sid))
            if not selected:
                body += '<p>No cached sprints. Select boards in team setup, then refresh.</p>'
            for sprint in selected.values():
                body += (f'<p><a href="/{slug}/capacity/plan/{tid}/{sprint["id"]}">'
                         f'{render.esc(sprint["name"])} (#{sprint["id"]})</a> · '
                         f'{render.esc(sprint["state"])} · cached '
                         f'{render.esc(sprint.get("fetched_at", ""))}</p>')
        return _page(slug, "Capacity", body)


@bp.post("/<slug>/capacity/catalogue")
def catalogue(slug):
    project = _project(slug)
    if not project.lock.acquire(blocking=False):
        return _page(slug, "Capacity", "", "Refresh is busy. Retry discovery."), 409
    try:
        with project.con.cursor() as con:
            scope = urd.load_scope(con)
            team = capacity.get_team(con, flask.request.form.get("team_id"))
            jira = urd.Jira(scope["site"], scope["email"], urd.token())
            capacity.refresh_catalogue(
                con, jira, scope["project"], team["boards"] if team else None)
    except (ValueError, SystemExit) as exc:
        return _page(slug, "Catalogue refresh failed",
                     "<p>Previously cached metadata and saved plans remain available.</p>",
                     str(exc)), 400
    finally:
        project.lock.release()
    return flask.redirect(f"/{slug}/capacity/")


def _team_form(slug, con, value, message=""):
    body = ('<form id="team" method="post">' + _hidden(value)
            + _input("name", value.get("name"), label="Team name")
            + _input("components", ", ".join(value.get("components", [])),
                     label="Jira components (comma separated, match any)")
            + _input("timezone", value.get("timezone"), label="Planning timezone, for example UTC"))
    body += '<fieldset class="boxes"><legend>Scrum boards for sprint discovery</legend>'
    found = {b["id"]: b["name"] for b in capacity.boards(con)}
    for identity in value.get("boards", []):
        found.setdefault(identity, f"Cached board {identity}")
    for identity, name in found.items():
        checked = "checked" if identity in value.get("boards", []) else ""
        body += ('<label>' + _input("boards", identity, "checkbox", extra=checked)
                 + render.esc(name) + "</label>")
    body += ('</fieldset><p id="weekly-hours-help">Normal Work and Meetings hours are copied '
             'into new sprints. Work excludes meetings; focus applies only to Work. '
             'Enter 0 for none; blank means not entered. Work + Meetings must not exceed '
             '24 hours per day.</p>')
    body += ('<div class="capacity-scroll"><table class="urd capacity-grid">'
             '<thead><tr><th>Person</th>')
    body += "".join(f"<th>{d}</th>" for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
    body += "<th>Remove</th></tr></thead><tbody>"
    for member in value.get("members", []):
        body += f'<tr><th scope="row">{render.esc(member["name"])}</th>'
        for index, cell in enumerate(member.get("weekly", [{} for _ in range(7)])):
            body += "<td>"
            for field, label in (("work", "Work"), ("meetings", "Meetings")):
                body += _input(
                    f'weekly:{field}:{member["id"]}:{index}', cell.get(field), "number", label,
                    extra=f'min="0" max="24" step="any" aria-label="{render.esc(member["name"])} '
                          f'day {index + 1} {label} hours" aria-describedby="weekly-hours-help"')
            body += "</td>"
        body += "<td>" + _input(f'remove:{member["id"]}', "yes", "checkbox") + "</td></tr>"
    body += "</tbody></table></div>" + _member_picker(con)
    body += '<button name="action" value="save">Save team</button></form>'
    return _page(slug, "Team and normal working hours", body, message)


@bp.route("/<slug>/capacity/teams/<team_id>", methods=["GET", "POST"])
def team_form(slug, team_id):
    project = _project(slug)
    with project.con.cursor() as con:
        value = capacity.get_team(con, team_id) if team_id != "new" else {
            "name": "", "components": [], "timezone": "", "boards": [], "members": [], "version": 0}
        if value is None:
            flask.abort(404)
        if flask.request.method == "GET":
            return _team_form(slug, con, value)
        try:
            value = _payload()
            value.update(name=flask.request.form.get("name", ""),
                         components=[c.strip() for c in flask.request.form.get(
                             "components", "").split(",") if c.strip()],
                         timezone=flask.request.form.get("timezone", ""),
                         boards=[capacity._id(b) for b in flask.request.form.getlist("boards")])
            value["members"] = [m for m in value.get("members", [])
                                if not flask.request.form.get(f'remove:{m["id"]}')]
            if any(f'weekly:{field}:{member["id"]}:{i}' not in flask.request.form
                   for member in value["members"] for i in range(7)
                   for field in ("work", "meetings")):
                raise ValueError("Enter weekly Work and Meetings hours for all seven days.")
            for member in value["members"]:
                member["weekly"] = [
                    {field: flask.request.form[f'weekly:{field}:{member["id"]}:{i}']
                     for field in ("work", "meetings")} for i in range(7)]
            if flask.request.form.get("action") == "add_member":
                _add_member(con, value, patterns=True)
                return _team_form(slug, con, value)
            if not project.lock.acquire(blocking=False):
                raise capacity.Conflict("Refresh is busy. Your entries are retained; retry saving.")
            try:
                capacity.save_team(con, value, None if team_id == "new" else team_id,
                                   int(value.get("version", 0)))
            finally:
                project.lock.release()
            return flask.redirect(f"/{slug}/capacity/")
        except (ValueError, KeyError, TypeError) as exc:
            return _team_form(slug, con, value, str(exc)), (
                409 if isinstance(exc, capacity.Conflict) else 400)


def _proposed_dates(sprint, settings):
    zone = ZoneInfo(settings["timezone"])
    return {key: history.timestamp(sprint.get(field)).astimezone(zone).date().isoformat()
            if history.timestamp(sprint.get(field)) else ""
            for key, field in (("start", "startDate"), ("end", "endDate"))}


def _date_mismatch(value, current):
    return any(value["sprint"].get(key) != current.get(key) for key in ("startDate", "endDate"))


def _preview_id(con, value):
    sources = con.execute("SELECT sprint_id, version, confirmation FROM capacity_plans "
                          "WHERE team_id = ? ORDER BY sprint_id", [value["team_id"]]).fetchall()
    scope = json.dumps([value["settings"], sources], sort_keys=True)
    return hashlib.sha256((history.source_id(con) + scope).encode()).hexdigest()


def _grid(value):
    try:
        calendar = capacity.days(value["dates"]["start"], value["dates"]["end"])
    except ValueError:
        calendar = sorted({d for m in value["members"]
                           for d in m.get("daily", {})})
    rows = ('<h2>Availability in hours</h2>'
            '<p id="daily-hours-help">Enter <strong>Work</strong> hours excluding meetings '
            'and <strong>Meetings</strong> hours for each day. Exclude leave and time allocated '
            'to other teams. Enter 0 for none; blank means not entered. '
            'Work + Meetings must not exceed 24 hours per day.</p>'
            '<p><strong>Calculated totals:</strong> Available adds Work and Meetings. '
            'Focus applies only to Work. Meetings are already excluded from Work. '
            'Use Preview to update the totals.</p>')
    rows += ('<div class="capacity-scroll"><table class="urd capacity-grid">'
             '<thead><tr><th>Person</th>')
    rows += "".join(f'<th scope="col">{d}</th>' for d in calendar)
    rows += ('<th>Available (h)</th><th>Work (h)</th>'
             '<th>Remove</th></tr></thead><tbody>')
    try:
        by_member = {m["id"]: m for m in capacity.totals(value)["members"]}
    except ValueError:
        by_member = {}
    for member in value["members"]:
        identity = member["id"]
        rows += f'<tr><th scope="row">{render.esc(member["name"])}</th>'
        for day in calendar:
            rows += "<td>"
            for field, label in (("work", "Work"), ("meetings", "Meetings")):
                rows += _input(
                    f"{field}:{identity}:{day}", member.get("daily", {}).get(day, {}).get(field),
                    "number", label,
                    extra=f'min="0" max="24" step="any" aria-label="{render.esc(member["name"])} '
                          f'{day} {label} hours" aria-describedby="daily-hours-help"')
            rows += "</td>"
        totals = by_member.get(identity, {})
        rows += (f'<td>{render.esc(totals.get("available_hours", ""))}</td>'
                 f'<td>{render.esc(totals.get("work_hours", ""))}</td><td>'
                 + _input(f"remove:{identity}", "yes", "checkbox",
                          extra=f'aria-label="Remove {render.esc(member["name"])}"') + "</td></tr>")
    rows += "</tbody></table></div>"
    outside = [(m["name"], d, cell) for m in value["members"]
               for d, cell in m.get("daily", {}).items() if d not in calendar]
    if outside:
        rows += ('<details open><summary>Retained hours outside the planning dates '
                 '(not counted)</summary>')
        rows += "".join(f'<p>{render.esc(n)} · {render.esc(d)} · '
                        f'Work: {render.esc(cell.get("work"))} h; '
                        f'Meetings: {render.esc(cell.get("meetings"))} h</p>'
                        for n, d, cell in outside) + "</details>"
    return rows


def _summary(con, value, preview):
    try:
        totals = capacity.totals(value)
        forecast = capacity.forecast(totals["focus_hours"], value.get("rate", {}))
        focus = (render.esc(totals["focus_hours"]) if totals["focus_hours"] is not None
                 else "Incomplete")
        body = (f'<p><strong>{focus}'
                f' focus hours</strong> · forecast: '
                f'{render.esc(forecast) if forecast is not None else "unavailable"} points</p>')
    except ValueError:
        body = "<p>Correct the highlighted inputs to calculate capacity.</p>"
    body += "".join(f'<p class="warn">{render.esc(warning)}</p>'
                    for warning in capacity._rate_warnings(con, value.get("rate", {})))
    if value.get("confirmation") is not None:
        original = capacity.get_plan(
            con, value["team_id"], value["sprint_id"], value["confirmation"])
        baseline = original["baseline"]
        body += (f'<p>Original: {render.esc(original["totals"]["focus_hours"])} focus hours. '
                 f'Confirmed {render.esc(original["created_at"])}. '
                 f'{render.esc(baseline["kind"])}'
                 f'{"; late" if baseline.get("late") else ""}.</p>')
        comparison = history.review(con, value, baseline)
        body += (f'<p>Delivered: {render.esc(comparison["delivered_points"])} known points'
                 f'{" (partial)" if not comparison["points_complete"] else ""}; '
                 f'{comparison["delivered_counts"]["main"]} main tickets and '
                 f'{comparison["delivered_counts"]["subtask"]} subtasks.</p>')
        body += '<details><summary>Ticket evidence</summary>'
        body += '<table class="urd"><tr><th>Ticket</th><th>Cohort</th><th>Delivered</th>'
        body += ('<th>Entry points</th><th>Closing points</th>'
                 '<th>Estimate change</th><th>Evidence</th></tr>')
        site = urd.load_scope(con).get("site") or ""
        for key, ticket in comparison["tickets"].items():
            evidence = list(ticket["unknown"])
            if ticket["baseline_conflict"]:
                evidence.append("baseline conflicts with later evidence")
            if ticket["converted"]:
                evidence.append("classification changed")
            if ticket["point_source_changed"]:
                evidence.append("counted point source changed")
            body += (f'<tr><td><a href="https://{render.esc(site)}/browse/{render.esc(key)}">'
                     f'{render.esc(key)}</a></td><td>{ticket["cohort"]}</td>'
                     f'<td>{ticket["delivered"]}</td><td>{render.esc(ticket["entry_points"])}</td>'
                     f'<td>{render.esc(ticket["closing_points"])}</td>'
                     f'<td>{render.esc(ticket["estimate_change"])}</td>'
                     f'<td>{render.esc(", ".join(evidence))}</td></tr>')
        body += "</table></details>"
        body += "".join(f'<p class="warn">{render.esc(reason)}</p>'
                        for reason in comparison["coverage"])
        body += "<p>Original outcomes: " + render.esc(
            ", ".join(f'{name.replace("_", " ")}: {len(keys)}'
                      for name, keys in comparison["original_outcomes"].items())) + "</p>"
        body += (f'<p>Added work: {len(comparison["added"])}; added delivery: '
                 f'{len(comparison["added_delivered"])}; unclassified delivery: '
                 f'{len(comparison["unclassified_delivered"])}.</p>')
    else:
        baseline = preview
        body += (f'<p>Commitment preview: {len(baseline["issues"])} known tickets, '
                 f'{render.esc(baseline["known_points"])} known points'
                 f'{" (partial)" if not baseline["points_complete"] else ""}. '
                 f'{render.esc(baseline["kind"])}'
                 f'{"; late" if baseline.get("late") else ""}.</p>')
        if not baseline["coherent"]:
            body += ('<p class="warn">Refresh and derive Jira before confirming. '
                     'Drafts can be saved.</p>')
    body += (f'<p>Baseline cutoff: {render.esc(baseline.get("cutoff"))}. '
             f'Sync completed: {render.esc(baseline.get("sync_completed_at"))}. '
             'Issue observations can differ from the sync completion time.</p>')
    return body


def _plan_form(slug, con, value, message="", reason=""):
    # Every rendering path must pair its displayed evidence and token in one snapshot.
    con.execute("BEGIN")
    try:
        return _render_plan_form(slug, con, value, message, reason)
    finally:
        con.execute("ROLLBACK")


def _render_plan_form(slug, con, value, message="", reason=""):
    current = capacity.sprint(con, value["sprint_id"])
    preview = history.baseline(con, value)
    body = _summary(con, value, preview)
    body += (f'<p>Team scope: {render.esc(", ".join(value["settings"]["components"]))}. '
             f'Timezone: {render.esc(value["settings"]["timezone"])}. '
             f'Jira metadata cached {render.esc(current.get("fetched_at"))}.</p>')
    body += '<form id="plan" method="post">' + _hidden(value)
    body += _input("source_id", _preview_id(con, value), "hidden")
    body += _input("start", value["dates"]["start"], "date", "Planning start date")
    body += _input("end", value["dates"]["end"], "date", "Planning last date")
    if _date_mismatch(value, current):
        dates = _proposed_dates(current, value["settings"])
        body += (f'<p class="warn">Jira dates changed to {render.esc(dates["start"])} through '
                 f'{render.esc(dates["end"])}. Review the dates and retained hours.</p>'
                 '<label>' + _input("reconcile_dates", "yes", "checkbox")
                 + "I reviewed these date changes and my planning dates</label>")
    body += _grid(value) + _member_picker(con)
    body += _input("focus", value.get("focus"), "number", "Focus percentage",
                   'min="0" max="100" step="any"')
    form = flask.request.form
    body += '<label>Rate source for this save <select name="rate_mode">'
    for mode, label in (("keep", "Keep the recorded rate"), ("none", "No rate"),
                        ("manual", "Manual rate"), ("history", "Selected history")):
        selected = "selected" if form.get("rate_mode", "keep") == mode else ""
        body += f'<option value="{mode}" {selected}>{label}</option>'
    body += '</select></label>'
    body += _input("manual_rate", form.get("manual_rate", value.get("rate", {}).get("value")),
                   "number", "Manual points per focus hour", 'min="0" step="any"')
    body += ('<p>Recorded rate: ' + render.esc(value.get("rate", {}).get("value"))
             + " (" + render.esc(value.get("rate", {}).get("kind", "none")) + ").</p>")
    body += '<details><summary>Historical sprints for a weighted rate</summary>'
    selected_history = set(form.getlist("history"))
    for candidate in capacity.history_candidates(con, value):
        checked = "checked" if str(candidate["sprint_id"]) in selected_history else ""
        # Keep invalid selections submitted until cleared, so retries cannot silently drop them.
        disabled = "" if candidate["eligible"] or checked else "disabled"
        body += ('<label>' + _input(
            "history", candidate["sprint_id"], "checkbox", extra=f"{checked} {disabled}")
                 + render.esc(candidate["label"]) + " · " + render.esc(candidate["reason"])
                 + "</label>")
    body += "</details>"
    body += _input("reason", reason, label="Reason (required for confirmed changes or voiding)")
    body += ('<div class="capacity-actions"><button name="action" value="preview">Preview</button>'
             '<button name="action" value="save">Save adjustment</button>'
             if value.get("confirmation") is not None else
             '<div class="capacity-actions"><button name="action" value="preview">Preview</button>'
             '<button name="action" value="save">Save draft</button>'
             '<button name="action" value="confirm">Confirm plan</button>')
    if value.get("confirmation") is not None:
        body += '<button name="action" value="void">Void confirmation</button>'
    body += ('<button name="action" value="review_latest">'
             'Review against latest saved version</button>')
    if value.get("confirmation") is None:
        body += '<button name="action" value="team_setup">Use current team setup</button>'
    body += "</div></form>"
    voids = [r for r in capacity.revisions(con, value["team_id"], value["sprint_id"])
             if r["action"] == "void"]
    if value.get("confirmation") is None and voids:
        body += (f'<form method="get" action="/{slug}/capacity/replace">'
                 + _input("source_team", value["team_id"], "hidden")
                 + _input("source_sprint", value["sprint_id"], "hidden")
                 + _input("confirmation", voids[-1]["voids"], "hidden")
                 + '<label>Replacement team <select name="team_id">'
                 + "".join(f'<option value="{t["id"]}">{render.esc(t["name"])}</option>'
                           for t in capacity.teams(con))
                 + '</select></label><label>Replacement sprint <select name="sprint_id">'
                 + "".join(f'<option value="{s["id"]}">{render.esc(s["name"])}</option>'
                           for s in capacity.sprints(con))
                 + '</select></label><button>Prepare replacement</button></form>')
    if value.get("confirmation") is not None:
        body += (f'<form method="get" action="/{slug}/capacity/plan/{value["team_id"]}/'
                 f'{value["sprint_id"]}/export"><label>'
                 + _input("grid", "yes", "checkbox")
                 + 'Include individual availability grid</label>'
                 + '<button>Export HTML</button></form>')
    body += '<details><summary>Revision history (local only)</summary>'
    for revision in capacity.revisions(con, value["team_id"], value["sprint_id"]):
        body += (f'<p>Revision {revision["version"]} · {render.esc(revision["created_at"])} · '
                 f'{revision["action"]} · {render.esc(revision["reason"])} · '
                 f'<a href="?version={revision["version"]}">View saved revision</a></p>')
    body += "</details>"
    return _page(slug, f'{value["settings"]["name"]}: {current["name"]}', body, message)


def _read_plan_inputs(value):
    form = flask.request.form
    value["dates"] = {"start": form.get("start", ""), "end": form.get("end", "")}
    value["focus"] = form.get("focus", "")
    value["members"] = [m for m in value.get("members", [])
                        if not form.get(f'remove:{m["id"]}')]
    for member in value["members"]:
        for field in ("work", "meetings"):
            prefix = f'{field}:{member["id"]}:'
            for name in form:
                if name.startswith(prefix):
                    member.setdefault("daily", {}).setdefault(name[len(prefix):], {})[field] = (
                        form[name])
    return value


@bp.route("/<slug>/capacity/plan/<team_id>/<int:sprint_id>", methods=["GET", "POST"])
def plan_form(slug, team_id, sprint_id):
    project = _project(slug)
    with project.con.cursor() as con:
        saved = capacity.get_plan(con, team_id, sprint_id)
        team, sprint = capacity.get_team(con, team_id), capacity.sprint(con, sprint_id)
        if team is None or sprint is None:
            flask.abort(404)
        if flask.request.method == "GET":
            version = flask.request.args.get("version", type=int)
            if version:
                value = capacity.get_plan(con, team_id, sprint_id, version)
                if value is None:
                    flask.abort(404)
                return _page(slug, f"Saved revision {version}",
                             "<pre>" + render.esc(json.dumps(value, indent=2)) + "</pre>")
            replacement = flask.request.args.get("replacement")
            link = None
            if replacement:
                try:
                    link = capacity._replacement(con, json.loads(replacement))
                    if saved and saved.get("confirmation") is not None:
                        raise capacity.Conflict("The destination already has a valid plan.")
                except (ValueError, KeyError, TypeError) as exc:
                    return _page(slug, "Replacement", "", str(exc)), 409
            if saved is None:
                dates = _proposed_dates(sprint, team)
                start, end = (flask.request.args.get(k, dates[k]) for k in ("start", "end"))
                if not start or not end:
                    body = ('<p>This sprint needs local planning dates.</p><form method="get">'
                            + _input("start", start, "date", "Planning start date")
                            + _input("end", end, "date", "Planning last date")
                            + (_input("replacement", replacement, "hidden") if replacement else "")
                            + "<button>Use planning dates</button></form>")
                    return _page(slug, sprint["name"], body)
                try:
                    saved = capacity.new_plan(con, team_id, sprint_id, start, end)
                except ValueError as exc:
                    return _page(slug, "Planning dates", "", str(exc)), 400
            if link:
                saved["replaces"] = link
            return _plan_form(slug, con, saved)
        value, reason = saved, flask.request.form.get("reason", "")
        try:
            value = _read_plan_inputs(_payload())
            if value.get("team_id") != team_id or value.get("sprint_id") != sprint_id:
                raise ValueError("The form belongs to a different team or sprint.")
            action = flask.request.form.get("action", "preview")
            if action == "add_member":
                _add_member(con, value)
                return _plan_form(slug, con, value, reason=reason)
            capacity.days(value["dates"]["start"], value["dates"]["end"])
            if flask.request.form.get("reconcile_dates"):
                value["sprint"] = copy.deepcopy(sprint)
            if not project.lock.acquire(blocking=False):
                raise capacity.Conflict("Refresh is busy. Your entries are retained; retry saving.")
            try:
                if action == "team_setup":
                    if value.get("confirmation") is not None:
                        raise ValueError("Void the confirmation before changing team scope.")
                    fresh = capacity.new_plan(con, team_id, sprint_id,
                                              value["dates"]["start"], value["dates"]["end"])
                    previous = {m["id"]: m for m in value["members"]}
                    for member in fresh["members"]:
                        if member["id"] in previous:
                            member["daily"] = previous[member["id"]]["daily"]
                    value.update(settings=fresh["settings"], members=fresh["members"])
                    return _plan_form(
                        slug, con, value, "Review the copied team settings and hours.",
                        reason=reason)
                if action == "review_latest":
                    latest = capacity.get_plan(con, team_id, sprint_id)
                    value.update(version=latest["version"] if latest else 0,
                                 confirmation=latest["confirmation"] if latest else None)
                    return _plan_form(slug, con, value, "Review your entries before saving.",
                                      reason=reason)
                mode = flask.request.form.get("rate_mode", "keep")
                if ((action == "confirm" or (action == "save" and mode == "history"))
                        and flask.request.form.get("source_id") != _preview_id(con, value)):
                    raise capacity.Conflict("The Jira preview or historical capacity changed. "
                                            "Review the new preview before saving. "
                                            "Your entries are retained.")
                if mode == "manual":
                    value["rate"] = {"kind": "manual", "value": capacity.number(
                        flask.request.form.get("manual_rate"), "Rate", missing=True), "sources": []}
                elif mode == "none":
                    value["rate"] = {"kind": "none", "value": None, "sources": []}
                elif mode == "history":
                    candidates = {str(c["sprint_id"]): c
                                  for c in capacity.history_candidates(con, value) if c["eligible"]}
                    selected = flask.request.form.getlist("history")
                    if not selected or any(s not in candidates for s in selected):
                        raise ValueError("Select eligible historical sprints.")
                    value["rate"] = capacity.history_rate(
                        [candidates[s] for s in sorted(set(selected))])
                elif mode != "keep":
                    raise ValueError("Choose a valid rate source.")
                capacity.totals(value)
                if action == "preview":
                    return _plan_form(slug, con, value, reason=reason)
                if action != "void" and _date_mismatch(value, sprint):
                    raise ValueError("Review and acknowledge the changed Jira dates first.")
                baseline = history.baseline(con, value) if action == "confirm" else None
                capacity.save_plan(con, value, int(value.get("version", 0)), action=action,
                                   reason=reason, baseline=baseline,
                                   replaces=value.get("replaces"))
            finally:
                project.lock.release()
            return flask.redirect(flask.request.path)
        except (ValueError, KeyError, TypeError) as exc:
            status = 409 if isinstance(exc, capacity.Conflict) else 400
            if value is None:
                return _page(slug, "Invalid plan", "", str(exc)), status
            try:
                return _plan_form(slug, con, value, str(exc), reason), status
            except (ValueError, KeyError, TypeError):
                return _page(slug, "Correct the plan", "<pre>" + render.esc(
                    json.dumps(value, indent=2)) + "</pre>", str(exc)), status


@bp.get("/<slug>/capacity/replace")
def replace_plan(slug):
    project = _project(slug)
    with project.con.cursor() as con:
        try:
            source = capacity._replacement(con, {
                "team_id": flask.request.args.get("source_team"),
                "sprint_id": capacity._id(flask.request.args.get("source_sprint")),
                "confirmation": int(flask.request.args.get("confirmation", "")),
            })
            team_id = flask.request.args.get("team_id")
            sprint_id = capacity._id(flask.request.args.get("sprint_id"))
            if capacity.get_team(con, team_id) is None or capacity.sprint(con, sprint_id) is None:
                raise ValueError("Choose an existing destination team and sprint.")
            destination = capacity.get_plan(con, team_id, sprint_id)
            if destination and destination.get("confirmation") is not None:
                raise capacity.Conflict("The destination already has a valid confirmation.")
            query = urlencode({"replacement": json.dumps(source)})
            return flask.redirect(f"/{slug}/capacity/plan/{team_id}/{sprint_id}?{query}")
        except (ValueError, KeyError, TypeError) as exc:
            return _page(slug, "Replacement", "", str(exc)), 409


@bp.get("/<slug>/capacity/plan/<team_id>/<int:sprint_id>/export")
def export_plan(slug, team_id, sprint_id):
    project = _project(slug)
    with project.con.cursor() as con:
        con.execute("BEGIN")
        try:
            value = capacity.get_plan(con, team_id, sprint_id)
            if value is None or value["confirmation"] is None:
                flask.abort(404)
            body = capacity.report_section(con, flask.request.args.get("grid") == "yes", value)
            html = render.notice("Sprint capacity", []).replace("</body>", body + "</body>", 1)
            return flask.Response(html, mimetype="text/html", headers={
                "Content-Disposition": f'attachment; filename="capacity-{sprint_id}.html"'})
        finally:
            con.execute("ROLLBACK")
