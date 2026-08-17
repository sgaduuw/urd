# urd as a web application: design

`urd serve` starts a local Flask server that renders the existing report live, with
the five report flags as on-page controls, a Refresh button that syncs from Jira in
the background, and one database per Jira project. It ships as a container someone
runs for themselves.

The CLI keeps working unchanged. `urd report` still writes a self-contained,
archivable file; the server is an addition, not a replacement.

## Scope

In:

- `urd serve`, a Flask app rendering the 20 existing charts per request.
- The five report-time flags as query parameters and page controls: `--since`,
  `--exclude-epic`, `--min-closed`, `--threshold`, and the derive-time
  `--abandoned-status` as configuration.
- A Refresh button running sync then derive on a background thread.
- One DuckDB file per Jira project, with a project switcher.
- A setup wizard for a project that has no configuration yet.
- A container image and a compose file.

Deliberately out, each a later slice:

- **Authentication.** The container is single-user by construction until it exists.
- **Multi-user.** Needs a decision about shared versus per-user databases that the
  single-writer lock makes consequential; not pre-empted here.
- **Any chart comparing projects.** Three projects mean three workflows; see
  "One database per project".
- **Drill-down** into the tickets behind a chart.
- **Hosting.** Where a container holding a mirror of internal tickets may run is
  an employer question, not a design one.

## Why these choices

### Flask, not FastAPI or the standard library

Every DuckDB call is blocking. FastAPI's async model buys nothing for a
synchronous query and would need `run_in_threadpool` around each one or it stalls
every other request. Flask returns HTML strings, which is exactly what
`render.page` already produces.

The standard library was the alternative and was rejected on the grounds that this
is expected to grow a hosted multi-user version, where a framework earns its keep.
It costs the project its one-dependency property, which had held for 99 commits.

### One database per project

`project` already accepts a comma-separated list, and `build_jql` and
`_refresh_workflow_statuses` already handle it, so fetching several projects into
one file mostly works. Everything downstream does not:

- `status_order` is a single table. Three projects are three workflows, and rework
  detection, cycle-time start and review status all read that one list. A status
  absent from it is silently excluded from rework.
- The component filter is shared, so one project's component would be applied to
  the others, which will not use it.
- Median days in status across three unrelated workflows is not a number.

So each project gets its own file, its own workflow configuration, its own
component filter and its own report flags. Nothing cross-project is invented. The
cost, accepted: no chart can compare two projects.

### The token stays in the environment

A WordPress-style wizard naturally wants to collect the API token, and that would
put a credential at rest in the same volume as a thousand tickets with real names
and titles. Today the token lives in a keychain or an environment variable and is never
on disk in this project.

The split is by secrecy, not convenience: the wizard collects site, project,
component, window and workflow settings, none of which are secrets. `URD_TOKEN`
supplies the token, which `token()` already reads before it ever reaches the
keychain, so the container needs no change to that function and the `security`
call is simply never used on Linux.

## Concurrency

Two measurements decided this, both run against DuckDB 1.x before the design was
written:

**Reads do not block writes within one process.** With a write transaction open on
the main connection, a second cursor read successfully and saw the pre-write
snapshot. So requests can be served from the existing data while a sync writes new
data.

**A second process cannot open the file at all while a writer holds it**, not even
read-only: `IOException: Conflicting lock is held`. So sync must run inside the
server process rather than as a subprocess, and the CLI verbs cannot run against a
database that `serve` has open.

The design that follows:

- One read-write connection per project file, held for the process's lifetime.
- Every request reads on `con.cursor()`, giving snapshot isolation from other
  requests and from a running sync.
- Refresh runs sync then derive on a single background thread per project, guarded
  by a per-project lock. A second click while one runs is told so; it is neither
  queued nor run twice.
- Job state (idle, running with its latest progress line, or failed with its
  message) lives in an in-memory object per project. A running sync reports
  itself without writing to the database.
- `serve` holding the lock is caught and reported as "another urd is holding this
  database" rather than letting DuckDB's lock error surface.

Rendering all 20 charts measures 194ms against a thousand-issue database, and the page is 210KB
of which 51KB is the vendored uPlot. That is fast enough that controls are a plain
page reload: no client-side fetching, no partial updates, no second rendering path.

## Components

`serve.py`, a new module rather than growth in `urd.py`, which is already about
1100 lines:

- **The app.** Flask routes, described below.
- **The registry.** Scans the volume at startup, opens a connection per
  `<slug>.duckdb`, and owns each project's lock and job state. One object, so
  "which projects exist" has a single answer.
- **The job runner.** Runs sync then derive for one project on a thread, updating
  that project's job state. Knows nothing about HTTP.

Changes to existing code, all small:

- `report()` gains a sibling returning HTML rather than writing a file. Both call
  the same `render.page`.
- `main()` gains the `serve` verb.
- Nothing in `derive()`, `token()`, `render.py` or the 20 chart specs changes.

## Routes

| Route | Does |
| --- | --- |
| `/` | Redirects to the first configured project, or `/setup` if there are none. |
| `/<slug>/` | The report. The five flags are query parameters, defaulting from that project's `sync_state`. |
| `/<slug>/refresh` | POST. Starts the job, redirects back. |
| `/<slug>/status` | The job's state, for the page to poll while a sync runs. |
| `/setup` | The wizard. Redirects to `/` once at least one project is configured. |

Flags are read from the query string and never written back to `sync_state` from a
request. Writing per-request would make two browser tabs fight over each other's
window. The CLI remains the way to change a default.

## Setup

Unconfigured, every route redirects to `/setup`. The form takes site, email,
project, component, initial `--since`, and the derive settings that cannot be
guessed: status order, start status, review status, abandoned statuses.

Submitting validates before saving anything:

1. `GET /myself` with the environment's token, to prove the credential works.
2. The scope's JQL, for a count.

The page returns "authenticated as <name>, N issues in scope" and a confirm
button. Only on confirm is `sync_state` written. This exists because environment
variables fail at the first sync, minutes in, whereas a form fails in seconds: a
truncated token and a mistyped component are both caught before anyone waits.

Once at least one project is configured, `/setup` still adds further projects, but
`/` no longer redirects to it, so a reachable port cannot be used to re-point an
existing instance's first project. Reconfiguring an existing project is not in the
UI for this version; the CLI verbs already do it.

Environment variables seed the first project only, so a compose file yields a
working instance. A configured database wins over a disagreeing environment, and
the page says so: restarting with a stale compose file must not silently rescope
someone's data.

## First-run states

Each is a page, not an exception:

- **No databases, no environment.** What is missing and the compose keys that
  supply it. Refresh disabled.
- **Configured, never synced.** "Never synced" with Refresh as the obvious action.
- **Configured, synced, not derived.** Refresh runs both, so this is only
  reachable by a CLI user; the page says which step is missing.

None of these reaches a chart, so `report()` never has to cope with an empty
database.

## Error handling

- A sync failure leaves the job state failed with its message, and the page shows
  it. Per-issue failures already land in `sync_errors` and the header already
  counts them.
- A malformed query parameter is reported on the page and the chart falls back to
  that project's stored default, rather than 500ing. The existing validators
  already exit on bad input; the server catches `SystemExit` and renders it.
- A database that fails to open is listed in the switcher as broken rather than
  crashing startup, so one bad file does not take out the other projects.

## Testing

The existing 305 tests must keep passing untouched: this adds a caller, it does
not change `derive` or the chart specs.

New tests, all against Flask's test client with no live server:

- Every first-run state renders a page rather than raising.
- An unconfigured instance redirects to `/setup`; a configured one does not.
- Setup validates before writing: a rejected credential leaves `sync_state`
  untouched.
- Flags in the query string change the rendered output and do not change
  `sync_state`.
- A second refresh while one runs is refused, not queued.
- A request served during a simulated write sees the pre-write snapshot.
- One broken database file does not prevent the other projects loading.
- The leak guard covers the Dockerfile and compose file, since a working example
  is exactly where a real site, email or project key gets hardcoded.

## Risks

**The port is unauthenticated.** Anyone who reaches it reads every ticket title and
can trigger a sync. Acceptable bound to localhost; a real exposure the moment the
container is published on a network. The server binds `127.0.0.1` by default so
exposing it is a deliberate act, and the README states it rather than implying it.

**Flask's development server** is what the container runs. For one reader that is
honest; a production WSGI dependency for a workload of one is not. The swap belongs
with authentication, when there is more than one reader.

**Single-writer lock.** While `serve` runs, the CLI cannot touch that database.
Reported clearly rather than left as a DuckDB error.
