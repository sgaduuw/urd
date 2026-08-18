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
_REQUIRED = ("site", "email", "project", "since", "status_order", "start_status")


def validate(proposal, token, opener=None):
    missing = [f for f in _REQUIRED if not (getattr(proposal, f) or "").strip()]
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
