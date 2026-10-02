# CONTEXT.md

Design rationale for urd. [`AGENTS.md`](AGENTS.md) says what to do;
this file says why.

**Provenance.** This is distilled from the design documents the author
wrote under `docs/superpowers/specs/`, chiefly
`2026-08-13-design.md`, plus `2026-08-18-urd-web-design.md` and
`2026-08-20-urd-setup-discovery-design.md`. Those remain the
long-form source and go further than this file does. Nothing here is
reconstructed from reading the code alone; where a decision is
recorded only as code, it is not claimed as rationale.

## What urd is for

A local tool that mirrors one Jira project's activity into DuckDB and
renders a single self-contained HTML report, serving four readers from
one page:

1. **Flow health for a team lead**: where work piles up, what is aging.
2. **Reporting outward to stakeholders**: delivered versus open per release.
3. **Retro material for the team**: rework, carry-over, cycle time trend.
4. **Individual contribution evidence**: throughput, review load, handoffs.

The report is shared as the HTML file itself. There is no server, no
per-reader variant, and no scheduled run.

## Why not Jira's own reports

Jira already draws velocity, burndown, control and cumulative flow
charts. Three gaps justify a separate tool:

- **Transition authorship is not exposed in any built-in report.** So
  review load and handoffs, which are most of a reviewer's
  contribution, are invisible. This is the largest gap and the hardest
  to work around.
- **Nothing in Jira reports coverage.** A chart over a field a third
  of the team fills looks identical to one over a field everybody
  fills.
- **Jira's reports are board scoped, one per page.** A component slice
  of a project needs a board whose filter happens to match, and the
  result still cannot be put on one page or handed to someone as a
  file.

The corollary is a scope rule: **anything Jira already does well is
out of scope.** In particular, no burndown.

## Why the credential destination is immutable

urd holds a Jira API token, and the single worst failure available to
it is sending that token somewhere else. So the trusted hostname is
read from `URD_JIRA_HOST` in the process environment, deliberately
independent of the form input and of anything stored in a database,
and the requested site must match exactly. Redirects are refused even
when the hostname matches, because a redirect is precisely the
mechanism that would move a credential off the host that was
authorised to receive it.

The multi-tenant answer is a second process with its own host and
token, not a setting. A setting is a thing an attacker can try to
influence; a process boundary is not.

`test_security.py` pins each of these as a named invariant rather than
leaving them to be re-derived, which is what makes them safe to
refactor around.

## What must never enter the repository

The report output, the DuckDB file and any captured API response all
contain real ticket keys, display names and email addresses. All three
are gitignored from the first commit.

**Test fixtures are hand-written and synthetic rather than captured
and scrubbed, because scrubbing is a step that only has to be
forgotten once.** That is the whole argument, and it generalises: a
safety measure that depends on remembering to apply it will eventually
not be applied.

Status names are configuration, not constants, for the same reason in
a quieter form. `STATUS_ORDER` and the review status are supplied by
the user, so a real workflow's vocabulary is never baked into source
that gets committed.

## Why scope is project plus component, and nothing else

Following people across projects was considered and rejected. Scope is
one project, optionally narrowed by component, bounded by `updated`.

The accepted cost is that **work in the project without the component
set is invisible**. That is measurable (compare the headline count
against a project-wide count) and it is deliberately not fixed in
code: component hygiene is a team habit the tool depends on and cannot
repair.

## Why one sync rule rather than window arithmetic

One rule, applied per issue key:

> Fetch it if the key is absent, or if its `updated` value differs
> from the stored one.

This handles both directions at once, newer changes and an earlier
`--since`, with no window arithmetic to get wrong. Two passes: a JQL
search for `key` and `updated` only, then one issue fetch per key the
rule selects.

Fetching the changelog per issue rather than via `expand` on the
search endpoint avoids depending on whether the enhanced JQL search
endpoint honours `expand=changelog`, and brings fields and history
back in one request instead of two.

`sync_state` holds the scope, the earliest `--since` synced and the
last successful sync time, so a bare `urd sync` continues correctly.
A failed issue fetch is retried once, then recorded in `sync_errors`
and skipped, so **a killed backfill is continued by running it
again**. Partial data is never presented as complete: the report
header carries the sync timestamp and any error count.

## Why custom fields are resolved by name, never hardcoded

Field ids are read from `/rest/api/3/field` and cached in a `fields`
table. No hardcoded `customfield_10016`.

**This is not tidiness, it is a measured finding.** Story Points is a
per-instance custom field, and JQL emptiness predicates on it cannot
be trusted: on a real instance, `"Story Points" is not EMPTY` and
`"Story Points" is EMPTY` returned every issue and no issues
respectively, a flat contradiction, while the field id commonly cited
for it was null on every issue sampled.

So `derive` resolves the id, measures its null rate, and reports both,
and charts depending on an optional field render only when the null
rate clears the chart's threshold. The same shape covers the Sprint
field, whose id differs per board or project.

## Why coverage is a first-class output

Several of the risks the design enumerates share one failure mode: a
report that reads as precise when its coverage is partial. The
mitigations are therefore all about making incompleteness visible
rather than about achieving completeness: a coverage denominator per
chart, threshold warning strips, the sync timestamp in the header,
unknown statuses ranked last and printed by `derive`, and the count of
issues that had no sprint field at all.

A tool whose output is used as contribution evidence has to be honest
about what it did not see.

## Why no release machinery

urd is spun up locally when needed. It has no deployment, no release
cadence, and no consumer pinning a version, so the portfolio's
git-flow, CHANGELOG and 12-step release flow would be ceremony with no
beneficiary. The design document says as much, listing `RELEASING.md`
as out of scope "until there is something to release".

This is a deliberate divergence from the sibling projects rather than
drift. The trigger to revisit is urd acquiring a deployment or an
external consumer.
