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

## Discovery, from two calls that already exist

Both are already implemented on the client and already called by `sync`, so
this adds no new Jira surface:

- `GET /project/{key}/statuses` via `Jira.project_statuses` tells us which
  statuses this project's workflow actually uses. Needs no admin rights.
- `GET /status` via `Jira.statuses` gives every status with its
  `statusCategory.key`, one of `new`, `indeterminate`, `done`.

Intersecting them gives the project's statuses with their categories. Sampling
tickets was the alternative and is worse on every axis: one request instead of
a search plus a fetch per issue, authoritative rather than inferred, and it
includes statuses no current ticket happens to occupy.

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
  (won't do, rejected, cancelled, declined). A guess.

**Ordering within a category cannot be derived here.** That needs the
transition graph from `/workflow/search`, which requires admin. So a parking
status such as Blocked lands wherever its category puts it, which may be wrong.

That is acceptable because the refinement already exists: after the first sync,
`derive` prints the observed status table ordered by median time-to-reach, with
a `--status-order` line to paste. The page says so in one sentence, so the
operator knows the guess is a starting point rather than a verdict.

## Degradation

The statuses calls must never block setup. Both can 403 on a restricted
project, and `_refresh_workflow_statuses` already treats that as a lost hint
rather than a failed run; page 2 does the same. If discovery fails, page 2
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
- Discovery intersects the two calls: a status in the project's workflow but
  absent from the global list, and one present globally but not in the
  workflow, each land where the design says.
- An uncategorised status sorts last and is flagged.
- Each of the three guesses, including the case where the guess finds nothing
  and the field is left blank.
- A 403 on either discovery call still reaches page 2 with a stated reason, and
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
3. **Two calls or one?** `GET /status` is instance-wide and can be large on a
   big instance. Recommendation: make both calls, since the intersection is the
   point, and revisit only if the response size is a real problem.
