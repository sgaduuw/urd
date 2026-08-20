"""`/setup`: add a project after proving its scope works.

The token is not a field here. It comes from the environment through urd.token(),
so a form post can never carry a credential and nothing written from this page can
contain one. The form says where the token comes from, since otherwise its absence
looks like an omission.

Split into two pages so nobody has to know their workflow's status names before
using the tool: page one asks only what the operator already knows, page two shows
what discovery found (or why it did not) and lets them confirm or correct it.
"""
import flask

import render
import urd
import wizard

bp = flask.Blueprint("wizard", __name__)

_SCOPE_FIELDS = ("site", "email", "project", "component", "since")
_WORKFLOW_FIELDS = ("slug", "status_order", "start_status", "review_status",
                    "abandoned_status")
# Derived from the names, not asked for, so the page marks them as guesses
# where the operator is actually looking rather than only in prose above the
# form. status_order and slug are not here: one is a derived listing, the
# other a filename, neither is a guess about the workflow.
_GUESSED_FIELDS = ("start_status", "review_status", "abandoned_status")


def _inputs(names, values, guesses=()):
    return "".join(
        f'<label>{render.esc(name.replace("_", " "))}'
        f'{" (guess)" if name in guesses else ""} '
        f'<input name="{name}" value="{render.esc(values.get(name, ""))}"></label>'
        for name in names
    )


def _hidden(names, values):
    return "".join(
        f'<input type="hidden" name="{name}" value="{render.esc(values.get(name, ""))}">'
        for name in names
    )


def _page(title, lines, body, message=""):
    note = f'<p class="warn">{render.esc(message)}</p>' if message else ""
    return render.notice(title, lines).replace(
        "</body>",
        f'{note}<form method="post" action="/setup">{body}</form></body>', 1)


def _scope_page(values=None, message=""):
    return _page(
        "Add a project",
        ["The API token comes from URD_TOKEN in the environment, never from this "
         "form.",
         "The next page shows the statuses this project uses, so you do not have "
         "to know them now."],
        _inputs(_SCOPE_FIELDS, values or {}) + '<button type="submit">Check</button>',
        message,
    )


def _workflow_page(values, found, message=""):
    """found is None on a re-render that ran no discovery of its own (a missing
    required field, or a slug the registry refuses): say nothing about
    discovery rather than reaching for a value that was never asked for. A
    real Discovery with no problem and no statuses is the same story the
    design assigns to a failed lookup, since there is equally nothing to show.
    """
    lines = []
    if found is not None and (found.problem or not found.statuses):
        if found.problem:
            lines.append(found.problem)
        lines.append("Type the workflow fields yourself, or leave them and let "
                     "derive list what it finds after the first sync.")
    elif found is not None:
        lines.append("Status order is every status this project uses, in category "
                     "order. Start, review and abandoned status are guesses from "
                     "the names; check them.")
        lines.append("Ordering inside a category needs the transition graph, which "
                     "needs admin rights, so derive prints a better order to the "
                     "terminal running urd serve after the first sync.")
    table = ""
    if found is not None and found.statuses:
        rows = "".join(
            f"<tr><td>{render.esc(s.name)}</td>"
            f"<td>{render.esc(s.category or 'no category')}</td></tr>"
            for s in found.statuses
        )
        table = ("<details><summary>What was found</summary>"
                 '<table class="urd"><thead><tr><th>status</th><th>category</th>'
                 f"</tr></thead><tbody>{rows}</tbody></table></details>")
    return _page(
        "Confirm the workflow", lines,
        table + _inputs(_WORKFLOW_FIELDS, values, guesses=_GUESSED_FIELDS)
        + _hidden(_SCOPE_FIELDS, values)
        + '<input type="hidden" name="confirm" value="yes">'
        + '<button type="submit">Confirm and add</button>',
        message,
    )


def _proposal_from(values):
    return wizard.Proposal(
        site=values["site"], email=values["email"], project=values["project"],
        component=values["component"], since=values["since"],
        status_order=values.get("status_order", ""),
        start_status=values.get("start_status", ""),
        review_status=values.get("review_status", ""),
        abandoned_status=values.get("abandoned_status", ""),
    )


@bp.get("/setup")
def form():
    return _scope_page()


@bp.post("/setup")
def submit():
    registry = flask.current_app.config["REGISTRY"]
    fields = _SCOPE_FIELDS + _WORKFLOW_FIELDS
    values = {name: (flask.request.form.get(name) or "").strip() for name in fields}
    try:
        token = urd.token()
    except SystemExit as exc:
        return _scope_page(values, str(exc))

    result = wizard.validate(_proposal_from(values), token)
    if not result.ok:
        return _scope_page(values, result.problem)

    if not flask.request.form.get("confirm"):
        # Validated, not written. Discovery happens here rather than on page one
        # so a rejected credential costs nothing, and its failure is carried on
        # the page rather than raised.
        found = wizard.discover(_proposal_from(values), token)
        values = {**values, **wizard.propose(found.statuses),
                  "slug": urd.project_slug(values["project"])}
        return _workflow_page(
            values, found,
            f"authenticated as {result.who}, {result.issues} issues in scope")

    missing = [f for f in wizard.REQUIRED_FOR_DERIVE if not values.get(f)]
    if missing:
        # derive refuses without these, and a project that cannot derive lands on
        # a page whose only offered action repeats the failure.
        return _workflow_page(
            values, None,
            "missing: " + ", ".join(f.replace("_", " ") for f in missing))

    if registry.get(values["slug"]) is not None:
        # registry.add returns the existing project for a slug already taken
        # rather than refusing, so confirming here without a check would
        # upsert this proposal's scope over that project's in place. The slug
        # used to be typed; now it arrives prefilled, so accepting the default
        # twice is an ordinary thing to do, not a typo. The design does want
        # two databases for one project key to be possible, so this points at
        # editing the slug rather than forbidding the second database.
        return _workflow_page(
            values, None,
            f'the slug "{values["slug"]}" is already in use by another '
            "project; edit it below and confirm again")

    try:
        project = registry.add(values["slug"])
    except ValueError as exc:
        return _workflow_page(values, None, str(exc))
    wizard.apply(project.con, _proposal_from(values))
    return flask.redirect(f"/{values['slug']}/")
