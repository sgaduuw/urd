# AGENTS.md

Conventions for AI coding agents working on urd. Tracked in the repo
deliberately: a convention nobody else can read is a convention nobody
else follows. Read this before your first change.

This file says *what to do*. [`CONTEXT.md`](CONTEXT.md), beside it at
the repo root, says *why*. Both are distilled from the design
documents under `docs/superpowers/specs/`, which remain the long-form
source and are worth reading before any substantial change.

## What urd is

A **local** tool. It mirrors one Jira project's ticket history into a
DuckDB file and renders a self-contained HTML report, serving four
readers from one page: flow health, outward reporting, retro material,
and individual contribution evidence.

Three properties follow from "local", and they are design decisions
rather than things not got round to yet:

- **It is read only.** Every request to Jira is a GET. urd never
  writes anything back, and must not learn to.
- **There is no server, no scheduled run, and no per-reader variant.**
  `urd serve` renders over HTTP for convenience on your own machine;
  it is not a deployment. The report is shared by handing someone the
  HTML file.
- **It is not a released artifact.** No versioning, no release flow,
  no `develop` branch. See "Branching" below, which is a deliberate
  divergence from the rest of the portfolio.

## The two rules that matter most

### 1. Credentials go to exactly one host, and nothing in a request can change that

urd holds a Jira API token. The trusted hostname comes from
`URD_JIRA_HOST` in the process environment, **independently of the
form input and of anything stored in a database**, and the requested
site must match it exactly (case-insensitive). Redirects are blocked
even when the hostname matches.

This is a credential-exfiltration defence, and it is load-bearing
rather than decorative. `test_security.py` pins it:

- `test_setup_cannot_send_the_process_token_to_another_host`
- `test_site_syntax_cannot_change_the_credential_destination`
- `test_live_requests_require_a_valid_independent_trusted_host`
- `test_matching_hostname_sends_credentials_and_still_blocks_redirects`

**Do not weaken any of this for convenience**, and treat a change that
makes the trusted host depend on request data, stored settings, or a
redirect as a security regression regardless of how it is spelled. If
a second Jira tenant is needed, run a second process with its own
host and token. That is the supported answer.

`URD_JIRA_HOST` must be a bare hostname: no scheme, port, path or
credentials. Malformed input gets a clean error, never a 500
(`test_setup_reports_malformed_sites_without_a_server_error`).

### 2. Real data must never enter the repository

The report output, the DuckDB file and any captured API response all
contain real ticket keys, display names and email addresses. All are
gitignored, and the gitignore says why at the top. Keep it that way.

**Test fixtures are hand-written and synthetic, never captured and
scrubbed**, because scrubbing is a step that only has to be forgotten
once. See `tests/fixtures/README.md`.

**Status names are configuration, not constants.** `STATUS_ORDER`, the
review status and the abandoned status are supplied by the user, so a
real workflow's vocabulary is not baked into the source either. Do not
add instance-specific defaults.

## Running it

Dependencies are pinned in `requirements.txt` (compiled from
`requirements.in`); there is no package install step.

```sh
uv run --isolated --with-requirements requirements.txt python urd.py sync
uv run --isolated --with-requirements requirements.txt python urd.py derive
uv run --isolated --with-requirements requirements.txt python urd.py report
uv run --isolated --with-requirements requirements.txt python urd.py serve
uv run --isolated --with-requirements requirements.txt python urd.py sql
```

First run needs the full scope (`--site`, `--email`, `--project`,
optionally `--component`, `--since`) and `derive` needs the status
vocabulary. Every flag is remembered in `sync_state`, so from the
second run on the bare commands continue correctly.

`derive`, `report` and `sql` are offline and do not need
`URD_JIRA_HOST`. `sync` and the setup wizard do.

Tests and lint:

```sh
uv run --isolated --with-requirements requirements.txt python -m pytest
uv run ruff check .
```

## Layout

Flat modules at the repo root, one concern each:

- `urd.py` the CLI entry point: `sync`, `derive`, `report`, `sql`, `serve`.
- `projects.py` one Project per database file, and the registry owning them.
- `wizard.py` validate a proposed scope against Jira before writing it anywhere.
- `charts.py` chart specifications.
- `render.py` SVG primitives for the report.
- `webapp.py` the Flask app: wiring, and the states that are not a chart.
- `views_report.py` `/` and `/<slug>/`, and the flag controls.
- `views_jobs.py` refresh.
- `views_wizard.py` `/setup`: add a project after proving its scope works.

Tests sit beside the modules as `test_<module>.py`, plus
`test_security.py` (the credential-destination invariants above),
`test_sync_safety.py`, and `test_container.py`.

## Conventions

- **Adding a chart means writing SQL, nothing else.** `charts.py` is
  specifications; if a new chart needs Python, question the chart
  before adding the Python.
- **Custom fields are resolved by name, never hardcoded.** Field ids
  come from `/rest/api/3/field` and are cached in a `fields` table. No
  `customfield_10016` in source. This is not tidiness; CONTEXT.md has
  the measured reason.
- **A chart depending on an optional field renders only when its null
  rate clears the chart's threshold**, and coverage is reported. A
  chart over a field a third of the team fills must not look identical
  to one over a field everybody fills.
- **Partial data is never presented as complete.** The report header
  carries the sync timestamp and any error count. Preserve that when
  touching the header or the error path.
- **`sync` is resumable.** A failed issue fetch is retried once, then
  recorded in `sync_errors` and skipped; a killed backfill is
  continued by running it again. Do not replace this with a scheme
  that needs a clean run.
- **No em-dashes or double-dashes in prose**, anywhere: comments,
  docstrings, commit messages, PR bodies.

## Out of scope

From the design, and settled rather than open:

- Writing to Jira.
- Any server, live query surface, or scheduled run.
- Following people across projects. Scope is project plus component.
- Burndown and velocity charts, which Jira already draws well.
- Jira Server and Data Center. Cloud REST v3 only.

If a request implies one of these, say so rather than building it.

## Branching

**urd deliberately does not use the portfolio's git-flow**, because
git-flow exists to keep released code separate from in-progress work
and urd releases nothing. Work branches off `main` and merges back via
PR; `checks.yml` runs on pull requests and on pushes to `main`.

There is no `CHANGELOG.md`, no version, and no release flow, by the
same reasoning. The design document is explicit that `RELEASING.md`
waits "until there is something to release". If urd ever grows a
deployment or an external consumer, revisit this; until then, adding
the ceremony would cost maintenance and buy nothing.
