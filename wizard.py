"""Validate a proposed scope against Jira before writing it anywhere.

This exists because the two ways a first run goes wrong are indistinguishable
minutes later: a truncated token and a mistyped component both end in an empty
database. Checking the credential first, then the scope, makes the message say
which one it was.

The token is never a field here. It arrives from the environment through
urd.token() and is passed in, so nothing in this module can write it to a
database, a form, or a log line.
"""
from typing import NamedTuple

import urd


class Proposal(NamedTuple):
    site: str
    email: str
    project: str
    component: str
    since: str
    status_order: str
    start_status: str
    review_status: str
    abandoned_status: str


class Result(NamedTuple):
    ok: bool
    who: str = ""
    issues: int = 0
    problem: str = ""


# Empty component is legitimate: a whole project is a valid scope. Empty
# abandoned_status is too, and means nothing is treated as dropped.
# What a scope check needs. The workflow fields are not here: whether a scope
# works against Jira has nothing to do with how its statuses are ordered, and
# page one of the form has not asked about them yet.
REQUIRED_FOR_SCOPE = ("site", "email", "project", "since")
# What derive refuses to run without. Checked when the scope is written, not
# when it is validated.
REQUIRED_FOR_DERIVE = ("status_order", "start_status")


def validate(proposal, token, opener=None):
    missing = [f for f in REQUIRED_FOR_SCOPE if not (getattr(proposal, f) or "").strip()]
    if missing:
        # Refused before any request: a half-filled form should not cost a round
        # trip, and the message names the field rather than the failure.
        return Result(False, problem=f"missing: {', '.join(missing)}")
    if not token:
        return Result(False, problem="no API token in the environment; set URD_TOKEN")

    jira = urd.Jira(proposal.site, proposal.email, token, opener=opener)
    try:
        me = jira.get("/myself")
    except SystemExit as exc:
        # Jira's client raises SystemExit on a non-200. Credential first, so a
        # rejected token never gets misread as an empty scope.
        return Result(False, problem=f"could not authenticate: {exc}")

    jql = urd.build_jql(proposal.project, proposal.component, proposal.since)
    try:
        count = sum(1 for _ in jira.search(jql))
    except SystemExit as exc:
        return Result(False, who=me.get("displayName", ""),
                      problem=f"scope query failed: {exc}")
    if count == 0:
        return Result(False, who=me.get("displayName", ""),
                      problem="that scope matches 0 issues; check project, "
                              "component and the since date")
    return Result(True, who=me.get("displayName", ""), issues=count)


def apply(con, proposal):
    """Write the proposal. Only ever called after validate returned ok."""
    urd.save_scope(
        con,
        site=proposal.site,
        email=proposal.email,
        project=proposal.project,
        component=proposal.component or None,
        earliest_since=proposal.since,
        status_order=proposal.status_order,
        start_status=proposal.start_status,
        review_status=proposal.review_status or None,
        abandoned_status=proposal.abandoned_status or None,
    )


_CATEGORY_ORDER = ("new", "indeterminate", "done")
# Substring matches on a status name, lowercased. Guesses, presented as guesses.
_REVIEW_HINTS = ("review", "qa")
_ABANDONED_HINTS = ("won't do", "wont do", "will not do", "rejected",
                    "cancelled", "canceled", "declined", "duplicate")


class Status(NamedTuple):
    name: str
    category: str


class Discovery(NamedTuple):
    statuses: list
    problem: str = ""


def discover(proposal, token, opener=None):
    """The project's statuses with their categories, from two calls.

    /project/{key}/statuses says which statuses this workflow uses but is not
    relied on for categories; /status carries statusCategory for the whole
    instance. Intersecting the two gives a list that is both scoped to the
    project and categorised, without sampling tickets and without needing the
    admin-only transition graph.

    Never raises. Either call can 403 on a restricted project, and a lost hint
    must not stop someone finishing setup.
    """
    jira = urd.Jira(proposal.site, proposal.email, token, opener=opener)
    try:
        workflow = jira.project_statuses(proposal.project.split(",")[0].strip())
        instance = jira.statuses()
    except SystemExit as exc:
        return Discovery([], f"could not read the workflow's statuses: {exc}")

    category = {}
    for entry in instance:
        name = entry.get("name")
        if name:
            category[name] = ((entry.get("statusCategory") or {}).get("key") or "")

    names = []
    for issue_type in workflow:
        for status in issue_type.get("statuses", []):
            name = status.get("name")
            if name and name not in names:
                names.append(name)
    return Discovery([Status(n, category.get(n, "")) for n in names])


def propose(statuses):
    """Prefill values for the four workflow fields.

    Only status_order is derived: it is every status the workflow uses, in
    category order. The other three are name guesses, and the page says so.
    Ordering within a category needs the transition graph, which needs admin
    rights, so derive's own listing after the first sync is what refines it.

    Uncategorised statuses sort last rather than being guessed into a bucket.
    """
    def rank(status):
        try:
            return _CATEGORY_ORDER.index(status.category)
        except ValueError:
            return len(_CATEGORY_ORDER)

    ordered = sorted(statuses, key=rank)
    moving = [s for s in ordered if s.category == "indeterminate"]
    done = [s for s in ordered if s.category == "done"]

    def first_hint(candidates, hints):
        for status in candidates:
            if any(h in status.name.lower() for h in hints):
                return status.name
        return ""

    review = first_hint(moving, _REVIEW_HINTS)
    return {
        "status_order": ",".join(s.name for s in ordered),
        "start_status": moving[0].name if moving else "",
        "review_status": review,
        "abandoned_status": ",".join(
            s.name for s in done
            if any(h in s.name.lower() for h in _ABANDONED_HINTS)),
    }
