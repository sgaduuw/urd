# htmx for report tabs and capacity forms

Issue: #38.

## Problem

Two things slow down everyday use of `urd serve`:

1. The report is one long page. All four sections render on every load, and
   the capacity comparison is appended at the bottom. Finding a section means
   scrolling, and every load computes every chart.
2. Every capacity action reloads the whole page: Preview, Add person, removing
   a person, changing the rate, Save. Each reload loses the scroll position in
   a grid that can be many days wide.

## Outcome

- The report shows one section at a time, as tabs.
- Editing a capacity plan or team never reloads the page. Grid totals and the
  forecast update while you type.
- Everything still works with JavaScript disabled, through links and full form
  posts.

## Decisions

| Decision | Choice | Rejected, and why |
| --- | --- | --- |
| Static `report.html` | Remove the `report` command and the file. | Keeping it means maintaining a second, one-page layout for a prototype feature. |
| Client library | htmx 4.0.0, vendored. It is a final release, although npm still tags it `next`, with 2.0.11 as `latest`. Starting on 4 avoids a later major upgrade of code written now. | htmx 2.0.11: the current `latest`, but 4 renames its events and changes inheritance and swapping. A CSS framework such as Bulma for tabs: it would restyle every element and fight the existing stylesheet. Vanilla JavaScript: possible, but htmx was the explicit choice. |
| Script safety | A `Content-Security-Policy: script-src 'self'` header, with no inline scripts on any page. | htmx 2's `allowEval` and `allowScriptTags` settings: htmx 4 removed both, always executes `<script>` in swapped content, and evaluates `hx-on` and `js:` with `new Function`. |
| Partial updates | Fragment routes per component (approach 2). | Swapping the whole page with `hx-boost`: less code, but less precise. |
| Drift between page and fragments | One render function per fragment. The full page is assembled from the same functions the fragment routes return. | Rendering a fragment separately from the page: the two drift apart. |
| Delivering htmx | Served from `/vendor/htmx.min.js`, so the browser caches it. | Inlining it: only the offline file needed that. |

## Report

### Tabs

| Tab | Slug | Content |
| --- | --- | --- |
| Attention today (default) | `attention` | the charts in that section of `charts.SECTIONS` |
| Flow over time | `flow` | ditto |
| Commitments | `commitments` | ditto |
| Retrospective | `retrospective` | ditto |
| Capacity | `capacity` | `capacity.report_section(con)`, moved out of `report_html` |

- `GET /<slug>/?section=<slug>` renders the full page with that tab selected.
  An unknown or missing slug falls back to `attention`.
- `GET /<slug>/sections/<slug>` returns the fragment for the `#report`
  container: the tab bar plus the one section. An unknown slug returns 404.
- Only the selected section's charts are computed.
- Each tab is an `<a href="/<slug>/?section=…">`. htmx adds `hx-get` to the
  fragment route, `hx-target="#report"` and `hx-push-url` with the `href`.
  The fragment includes the tab bar, so the active tab is always the selected
  one. Back and forward work through the pushed URL.
- The header and the filter controls stay outside `#report`. The controls
  remain a normal GET form that reloads the page (`id="controls"`). The
  selected tab reaches it through a hidden `section` input inside the tab bar,
  with `form="controls"`, so every tab swap updates it.
- Filters live in the query string (`since`, `exclude_epic`, `component`,
  `threshold`). `flags_from` applies them inside a transaction that is always
  rolled back, so reading never changes the stored defaults, as today. Every
  tab link and every fragment request carries the current filter query
  string. Otherwise switching tabs would silently reset the filters.

### Saving filters as the default

The removed `report` command was the only way to change the stored defaults.
Its replacement is a **Save as default** button next to Apply in the controls
form: `<button formmethod="post" formaction="/<slug>/defaults">`, native HTML
with no JavaScript.

- `POST /<slug>/defaults` reads the same fields from the form and applies them
  with the same `set_report_window`, `set_excluded_epics` and
  `set_report_components` calls, plus `save_scope(thresholds=…)`, under
  `_RENDER_LOCK`, then commits.
- If any value is invalid, nothing is saved. The route rolls back and
  redirects to the page with the submitted filters, which shows the same
  problems the Apply path shows.
- On success it redirects to `/<slug>/?section=<tab>` without filters, so the
  page shows the new defaults.
- The app-wide same-origin check covers it, like every POST.

### Charts and sorting after a swap

`PLOT_SCRIPT` and `SORT_SCRIPT` currently run once on load, inline. Both move
into one served file, `/static/urd.js`, as a function `init(root)`. It runs on
load for `document` and on `htmx:after:settle` for the swapped element. uPlot
moves from inline to `/vendor/uplot.min.js` and `/vendor/uplot.min.css`. Chart
data stays in the existing `<script type="application/json" class="plot-data">`
islands. These are data blocks, which the browser never executes, and the
Content-Security-Policy does not affect them.

### Styling

Tabs are a `nav.tabs` row of links: an underline and the primary colour
variable on the active one, using the existing light and dark theme variables.
About 15 lines of CSS in `render.CSS`.

## Capacity

An unsaved plan lives in the form: the hidden `payload` JSON field plus the
grid inputs. The server holds no working copy between saves. A fragment that
changes the roster therefore returns the updated hidden `payload` together
with it.

### Plan page fragments

| Element | Render function | Contents |
| --- | --- | --- |
| `#totals` | new `_totals_line` | focus hours and forecast, the part that updates while typing |
| summary (no element id of its own) | `_summary` | commitment preview and delivery review once confirmed; refreshed through `#plan-area` on Preview and save, because it reads the whole mirror |
| `#grid` | `_grid` | the roster and day cells, the person picker, the hidden `payload` |
| `#rate` | new `_rate` | rate mode, manual rate, history candidates |
| `#plan-area` | `_render_plan_form` | the above plus the form, actions, messages and revision history |

### Interactions

| Trigger | Request | Swaps | Writes |
| --- | --- | --- | --- |
| Typing in a grid cell or the focus field | `POST …/plan/<team>/<sprint>/totals`, `hx-trigger="input delay:400ms"`, whole form included | per-person totals (out of band) and `#totals` | none |
| Add person, Remove (a button per row, applied immediately to the working copy) | `POST …/plan/<team>/<sprint>/members` | `#grid` including its new `payload` | none |
| Rate mode, manual rate, history selection | `POST …/plan/<team>/<sprint>/rate` | `#rate` and `#totals` (out of band) | none |
| Save, Confirm, Void, Review latest, Use team setup | the existing `POST` to the plan URL, `hx-select="#plan-area"` | `#plan-area` | unchanged |

- The three fragment routes only calculate from the submitted form. They take
  no `project.lock`, write nothing, and do not change the stale-preview token
  (`source_id`).
- Saves keep their route and logic. A successful save still redirects; htmx
  follows the redirect and selects `#plan-area` from the returned page.
- Preview stays as the no-JavaScript path. It also refreshes the Jira
  commitment preview and the stale-preview token, which live totals leave
  alone.

### Team page

Same pattern: `#weekly` holds the weekly grid, the person picker and the
hidden `payload`. `POST /<slug>/capacity/teams/<id>/members` adds or removes a
person in the working copy without writing. Save stays a normal form submit:
it is rare, and it ends on the capacity index anyway. The off-screen default
submit button in the team form is disabled, so Enter in a field submits nothing.

## Errors

- Fragment routes put validation messages inside the fragment they return,
  for example "Work + Meetings hours must not exceed 24" in `#totals`, with
  status 200: the request succeeded, the input is incomplete.
- Save errors keep their status codes, 400 for invalid input and 409 for a
  conflict. The re-rendered `#plan-area` carries the message.
- htmx 4 swaps every response except 204 and 304 (its `noSwap` default), so
  400 and 409 responses swap in without extra configuration.

## htmx configuration and security

- Vendored at `vendor/htmx.min.js`, version 4.0.0, licence 0BSD, SHA-256
  `e484d9171a9db30a39c8f16e3d709d4137f3211c659f8e6125816635033d593f` as
  published in the npm package `htmx.org@4.0.0` (`dist/htmx.min.js`, 36,716
  bytes). Recorded in `vendor/README.md` the way uPlot's are.
- Served by `GET /vendor/<name>` for the vendored files and
  `GET /static/urd.js` for urd's own script. Only the named files can be
  served, and the browser caches them for a long time.
- No htmx settings need changing. The defaults this design relies on are
  `mode: 'same-origin'` and `noSwap: [204, 304]`. The pages use no `hx-on`
  and no `js:` values.
- Every HTML response carries
  `Content-Security-Policy: script-src 'self'; object-src 'none'; base-uri 'none'`,
  set app-wide in an `after_request` hook. In htmx 4.0.0, `#processScripts`
  re-creates every `<script>` in swapped content as an inline script, and
  `hx-on` and `js:` values run through `new Function`. This policy blocks
  both: an inline script needs a nonce or `'unsafe-inline'`, and
  `new Function` needs `'unsafe-eval'`. So a markup injection that got past
  escaping still could not run a script. HTML escaping stays the first
  defence.
- The scripts load from `<head>` with `defer`. A history restore swaps
  `document.body`, which re-creates every `<script>` in it, so a script in the
  body would run again on each Back or Forward. `static/urd.js` also marks what
  it has set up, so a second pass over the same element does nothing.
- The existing app-wide same-origin and Host checks in `before_request` cover
  the new POST routes.
- The vendor audit test changes: uPlot must still make no network requests.
  htmx is allowed to, because requesting the app's own routes is its purpose.

## Removed

- The `report` command, `urd.report()` and its CLI options. Their job of
  changing stored defaults moves to Save as default.
- `test_report_writes_a_standalone_file_with_no_external_references`.
- The README and AGENTS sections that describe `report.html` and `report`.
  Filter documentation moves to the web controls.

Consequence: AGENTS.md says "the report is shared by handing someone the HTML
file". Without the file, a report is read in `urd serve`. The capacity HTML
export (`…/export`) stays, and it contains no scripts. AGENTS.md and
CONTEXT.md are updated to say this.

## Testing

- **Report:**
  - each tab's full page and fragment route returns 200
  - each tab computes only its own charts (assert on the chart ids rendered)
  - an unknown slug falls back on the page and returns 404 on the fragment
  - the page contains exactly the fragment HTML for the selected tab
- **Capacity fragment routes:**
  - status and content
  - the database is unchanged afterwards (compare all `capacity_*` tables)
  - removing a person updates `payload`
  - invalid input returns the message inside the fragment
- **Identity:** each full page contains exactly the HTML its fragment route
  returns for the same input.
- **Configuration and policy:**
  - the page links `/vendor/htmx.min.js`, and the route serves the file whose
    SHA-256 is recorded
  - every HTML response carries the Content-Security-Policy header
  - no rendered page contains an executable inline `<script>`: one without a
    `type`, or with a JavaScript type
  - an unknown file under `/vendor/` or `/static/` returns 404
- **Filters:**
  - tab links and fragment requests carry the current filter query string
  - Apply and tab switches leave `sync_state` and `report_window` unchanged
  - Save as default persists window, epics, components and thresholds
  - Save as default with one invalid value persists nothing
- **Manual check in the container,** because the repo has no browser tests:
  - switching tabs, and back and forward
  - charts render after a tab switch
  - totals update while typing, and the scroll position is kept
  - adding and removing a person, then saving, keeps the change
  - a 409 conflict shows its message in place
  - with JavaScript disabled, tabs and Preview still work
