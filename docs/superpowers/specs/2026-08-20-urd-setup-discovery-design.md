# Setup that does not require prior knowledge: design

Three of the setup form's ten fields cannot be filled by someone who has not
already run `urd` from the CLI, and a fourth asks for something the app can
derive. Split the form in two and have the app find the answers.

## The problem, precisely

| Field | Can the operator answer it? |
| --- | --- |
| site, email, project, since | Yes. They know these. |
| component | Only if they remember the exact string. |
| slug | It is a filename and a URL segment. `seed_from_env` already derives it from the project key. Asking is asking the operator to guess what the code would compute. |
| status order, start status, review status, abandoned status | No. These describe a workflow the app has not looked at yet. |

The last row is the real gap. `derive` refuses without `status_order`, so a
first run that leaves it blank syncs and then dead-ends on "synced but not
derived", whose only offered action is Refresh, which repeats.

## The flow

Two pages, no server-side session. State carries in hidden fields, which is
what the existing confirm step already does.

**Page 1: connection and scope.** site, email, project, component, since.
Submitting validates as it does today (credential first, then the scope's JQL
for a count) and additionally discovers the workflow.

**Page 2: workflow.** Shows what was found, with the four status fields
prefilled and editable, and the issue count from page 1. Confirming writes the
scope and creates the database, exactly as today.

Slug disappears as a question: derived from the project key the same way
`seed_from_env` does it, shown on page 2 as part of what will be created, and
editable there for the one case that needs it (two databases for one project
key).

## Discovery, from one call that already exists

Already implemented on the client and already called by `sync`, so this adds
no new Jira surface: `GET /project/{key}/statuses` via `Jira.project_statuses`
tells us which statuses this project's workflow actually uses, and needs no
admin rights.

An earlier version of this design also called `GET /status`, the
instance-wide status list, and intersected the two by matching status *name*,
on the belief that only the instance-wide call carried `statusCategory`. That
belief was never checked against a real instance, and it was wrong on both
counts. Measured against one:

- `/project/{key}/statuses` already carries `statusCategory` on every status
  it returns, so the second call added nothing.
- Status names are not unique on the instance: 323 statuses total, 25 names
  used more than once, at least two of them spanning different categories. The
  intersection built `{name: category}` from the instance-wide list, so the
  last occurrence of a repeated name won, and which occurrence is last is not
  something the code controls. On the probed workflow this already produced a
  wrong answer for a real status: the project's own response put it in `new`,
  the name-keyed lookup put it in `indeterminate`, and `status_order` sorted
  it accordingly with nothing to flag the disagreement.

So the two-call, intersect-by-name design is deleted rather than kept as an
option. There is one call, and the category comes from the status the project
itself reports. Sampling tickets was considered and rejected on the same
grounds it always was: one request beats a search plus a fetch per issue,
and the project's own answer is authoritative rather than inferred.

Note `statusCategory` can be `null` in real responses; `test_urd.py` already
pins that case. An uncategorised status sorts last and is called out on the
page rather than being silently placed.

## What is derived, and what is only guessed

Say which is which on the page. A guess presented as an answer is worse than a
blank field.

- **status order**: categories in flow order, `new` then `indeterminate` then
  `done`. Derived, and complete: every status the workflow uses appears.
- **start status**: the first `indeterminate` status. A guess.
- **review status**: an `indeterminate` status whose name contains "review" or
  "QA", else blank. A guess.
- **abandoned status**: `done`-category statuses whose names read as rejection
  (won't do, wont do, will not do, rejected, cancelled, canceled, declined,
  duplicate). A guess.

**Ordering within a category cannot be derived here.** That needs the
transition graph from `/workflow/search`, which requires admin. So a parking
status such as Blocked lands wherever its category puts it, which may be wrong.

That is acceptable because the refinement already exists: after the first sync,
`derive` prints the observed status table ordered by median time-to-reach, with
a `--status-order` line to paste. The page says so in one sentence, so the
operator knows the guess is a starting point rather than a verdict.

## Degradation

The statuses call must never block setup. It can 403 on a restricted project,
and `_refresh_workflow_statuses` already treats that as a lost hint rather
than a failed run; page 2 does the same. If discovery fails, page 2
renders with empty fields, the reason, and the note that `derive` will propose
values after the first sync. Setup still completes.

If discovery succeeds but returns nothing usable, that is the same path.

## Out of scope

- A component picker. `GET /project/{key}/components` would prefill it and it
  is the same class of problem, but it is a separate field with separate
  failure handling. Worth doing next, not here.
- Reconfiguring an existing project. Still CLI only, as today.
- The transition graph, and anything needing admin rights.

## Testing

- Page 1 validates before writing, as today, and creates no database when the
  credential is rejected. Existing tests cover this and must keep passing.
- The category comes from the status the project's own call reports. A status
  whose `statusCategory` is `null` there ends up uncategorised rather than
  guessed at.
- Two statuses sharing a name across issue types, with different categories
  each, must not let one overwrite the other: the regression a name-keyed
  lookup would reintroduce.
- An uncategorised status sorts last and is flagged.
- Each of the three guesses, including the case where the guess finds nothing
  and the field is left blank.
- A 403 on the discovery call still reaches page 2 with a stated reason, and
  confirming still writes the scope.
- The slug derived from a project key matches what `seed_from_env` produces for
  the same key. One assertion, so the two cannot drift.
- No test reaches the network. The suite refuses outbound requests at import.

## Open decisions

1. **Is page 2 skippable?** A "set these up later" path lands on "synced but
   not derived", which a review already called a dead end. Recommendation: no
   skip, since the fields are prefilled and accepting the guess is one click.
2. **Does page 2 show the evidence?** The full status list with categories and
   which are in the workflow is useful and is a table the report already knows
   how to render. Recommendation: yes, collapsed under the fields.

Decision 3 as originally framed, "two calls or one, given `GET /status` can be
large on a big instance", is withdrawn rather than resolved: there is no
instance-wide call left to be large. Measured against a real instance, that
call would have returned 323 statuses, so the size concern was real, but the
fix is that discovery never makes the call, not a size threshold for when to
skip it.
