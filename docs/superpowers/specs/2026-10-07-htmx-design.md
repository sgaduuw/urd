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
| Client library | htmx 2.0.11, vendored. | A CSS framework such as Bulma for tabs: it would restyle every element and fight the existing stylesheet. Vanilla JavaScript: possible, but htmx was the explicit choice. |
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
  remain a normal GET form that reloads the page, so their writes (date
  window, excluded epics, components) keep happening the way they do today.
  The page keeps the selected tab through a hidden `section` input.
- Thresholds submitted through the controls are now saved with
  `save_scope(thresholds=…)`. Before this change only the removed `report`
  command saved them.

### Charts and sorting after a swap

`PLOT_SCRIPT` and `SORT_SCRIPT` currently run once on load. Both become a
function `init(root)`. It runs on load for `document` and on `htmx:afterSettle`
for the swapped element. Chart data stays in the existing
`<script type="application/json" class="plot-data">` islands, which are data
and never executed.

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
| `#summary` | `_summary` | focus hours, forecast, commitment preview, delivery review once confirmed |
| `#grid` | `_grid` | the roster and day cells, the person picker, the hidden `payload` |
| `#rate` | new `_rate` | rate mode, manual rate, history candidates |
| `#plan` | `_render_plan_form` | the above plus actions and revision history |

### Interactions

| Trigger | Request | Swaps | Writes |
| --- | --- | --- | --- |
| Typing in a grid cell or the focus field | `POST …/plan/<team>/<sprint>/totals`, `hx-trigger="input changed delay:400ms"`, whole form included | per-person total cells (out of band) and `#summary` | none |
| Add person, Remove (a button per row, applied immediately to the working copy) | `POST …/plan/<team>/<sprint>/members` | `#grid` including its new `payload` | none |
| Rate mode, manual rate, history selection | `POST …/plan/<team>/<sprint>/rate` | `#rate` and `#summary` | none |
| Save, Confirm, Void, Review latest, Use team setup | the existing `POST` to the plan URL, `hx-select="#plan"` | `#plan` | unchanged |

- The three fragment routes only calculate from the submitted form. They take
  no `project.lock`, write nothing, and do not change the stale-preview token
  (`source_id`).
- Saves keep their route and logic. A successful save still redirects; htmx
  follows the redirect and selects `#plan` from the returned page.
- Preview stays as the no-JavaScript path. It also refreshes the Jira
  commitment preview and the stale-preview token, which live totals leave
  alone.

### Team page

Same pattern: `#weekly` holds the weekly grid, the person picker and the
hidden `payload`. `POST /<slug>/capacity/teams/<id>/members` adds or removes a
person in the working copy without writing. Save swaps the form.

## Errors

- Fragment routes put validation messages inside the fragment they return,
  for example "Work + Meetings hours must not exceed 24" in `#summary`, with
  status 200: the request succeeded, the input is incomplete.
- Save errors keep their status codes, 400 for invalid input and 409 for a
  conflict. The re-rendered `#plan` carries the message.
- htmx does not swap 4xx responses by default. The page sets
  `responseHandling` to swap 400 and 409 and keep the default for everything
  else.

## htmx configuration and security

- Vendored at `vendor/htmx.min.js`, version 2.0.11, licence 0BSD. Its version
  and SHA-256 are recorded in `vendor/README.md` the way uPlot's are.
- Served by one route, `GET /vendor/htmx.min.js`, with a long cache lifetime.
- Configured with a `<meta name="htmx-config">`:
  - `selfRequestsOnly: true` (the default, stated explicitly)
  - `allowEval: false`
  - `allowScriptTags: false`
  - the `responseHandling` above

  The pages use no `hx-on` or `js:` attributes, and swapped fragments never
  need a script executed.
- The existing app-wide same-origin and Host checks in `before_request` cover
  the new POST routes.
- The vendor audit test changes: uPlot must still make no network requests.
  htmx is allowed to, because requesting the app's own routes is its purpose.

## Removed

- The `report` command, `urd.report()` and its CLI options.
- `test_report_writes_a_standalone_file_with_no_external_references`.
- The README and AGENTS sections that describe `report.html` and `report`.
  Filter documentation moves to the web controls.

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
- **Configuration:** the page links `/vendor/htmx.min.js`, the route serves the
  file whose SHA-256 is recorded, and the meta config disables eval and script
  tags.
- **Thresholds:** submitted thresholds persist across requests.
- **Manual check in the container,** because the repo has no browser tests:
  - switching tabs, and back and forward
  - charts render after a tab switch
  - totals update while typing, and the scroll position is kept
  - adding and removing a person, then saving, keeps the change
  - a 409 conflict shows its message in place
  - with JavaScript disabled, tabs and Preview still work
