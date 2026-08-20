"""`/setup`: add a project after proving its scope works.

The token is not a field here. It comes from the environment through urd.token(),
so a form post can never carry a credential and nothing written from this page can
contain one. The form says where the token comes from, since otherwise its absence
looks like an omission.
"""
import flask

import render
import urd
import wizard

bp = flask.Blueprint("wizard", __name__)

_FIELDS = ("slug", "site", "email", "project", "component", "since",
           "status_order", "start_status", "review_status", "abandoned_status")


def _form(values=None, message="", confirmed=None):
    values = values or {}
    rows = "".join(
        f'<label>{render.esc(name.replace("_", " "))} '
        f'<input name="{name}" value="{render.esc(values.get(name, ""))}"></label>'
        for name in _FIELDS
    )
    hidden = ""
    button = "Check"
    if confirmed:
        hidden = "".join(
            f'<input type="hidden" name="{name}" value="{render.esc(values.get(name, ""))}">'
            for name in _FIELDS
        ) + '<input type="hidden" name="confirm" value="yes">'
        rows = ""
        button = "Confirm and add"
    note = (f'<p class="warn">{render.esc(message)}</p>' if message else "")
    return render.notice(
        "Add a project",
        ["The API token comes from URD_TOKEN in the environment, never from this "
         "form.",
         "Status order, start status and review status cannot be guessed before a "
         "first sync; derive lists what it found afterwards."],
    ).replace(
        "</body>",
        f'{note}<form method="post" action="/setup">{rows}{hidden}'
        f'<button type="submit">{button}</button></form></body>',
        1,
    )


@bp.get("/setup")
def form():
    return _form()


@bp.post("/setup")
def submit():
    registry = flask.current_app.config["REGISTRY"]
    values = {name: (flask.request.form.get(name) or "").strip() for name in _FIELDS}
    proposal = wizard.Proposal(
        site=values["site"], email=values["email"], project=values["project"],
        component=values["component"], since=values["since"],
        status_order=values["status_order"], start_status=values["start_status"],
        review_status=values["review_status"],
        abandoned_status=values["abandoned_status"],
    )
    try:
        token = urd.token()
    except SystemExit as exc:
        return _form(values, str(exc))

    result = wizard.validate(proposal, token)
    if not result.ok:
        return _form(values, result.problem)

    if not flask.request.form.get("confirm"):
        # Validated, not yet written. A mistyped component is caught here, in
        # seconds, rather than minutes into a sync.
        return _form(values,
                     f"authenticated as {result.who}, {result.issues} issues in scope",
                     confirmed=True)

    try:
        project = registry.add(values["slug"])
    except ValueError as exc:
        return _form(values, str(exc))
    wizard.apply(project.con, proposal)
    return flask.redirect(f"/{values['slug']}/")
