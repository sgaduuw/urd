# Setup discovery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `/setup` into two pages so nobody has to know their workflow's statuses before using it: page one takes the connection and scope, page two shows the statuses the app found and prefills the four workflow fields.

**Architecture:** Discovery intersects two Jira calls the client already has and `sync` already makes: `GET /project/{key}/statuses` for which statuses the workflow uses, `GET /status` for each status's category. The slug stops being a question and becomes one function shared with `seed_from_env`. No server-side session: page two carries page one's answers in hidden fields, the way the existing confirm step already does.

**Tech Stack:** Python 3.11+, DuckDB, Flask. No new dependency.

**Spec:** `docs/superpowers/specs/2026-08-20-urd-setup-discovery-design.md`

## Global Constraints

- Python 3.11 or later. Dependencies stay exactly `duckdb` and `flask`.
- `test_urd.py` must still pass 305 tests and must not be edited.
- `./tests/run.sh` is a real gate and must be green. `uv run --with ruff ruff check .` must pass, line length 100. `./tests/no-leaks.sh .` must report `clean: .`
- **No em-dashes or en-dashes anywhere**, including comments and commit messages. A hook rejects the file otherwise.
- **No test may reach the network.** `test_helpers` installs a guard that refuses outbound requests; every test file must import it, and `wizard.validate` must be patched or given a fake client in any test that reaches it.
- The API token stays in the environment. It is never a form field, never written to a database, never logged.
- Public repository: no employer name, real Jira hostname, real project key, component, colleague name or ticket key. Test hostnames use `.invalid`.
- macOS with BSD userland: `sed -i` needs an extension argument, there is no `timeout`.
- Comments explain why, not what.

### Four rules about this plan's own test code

The previous plan for this repository produced fifteen defects, every one of them
in its test evidence rather than its design. These are the shapes that recurred:

1. **Every fixture value must be valid for every code path it reaches.** One fake
   returned `"updated": "u1"` for a column `derive` parses with `fromisoformat`, so
   the test could never reach its assertion.
2. **No assertion may be satisfied by a string that appears elsewhere on the page.**
   `"already" in body` matched a CSS comment. `"min_closed" in body` matched a form
   control's `name`. `"--min-closed" in body` matched a chart caption. Assert the
   whole message.
3. **Every mutation-table row must name a test that exists in this plan.** One row
   named a fixture that was never written, so the mutation survived.
4. **A test must exercise the call path production takes.** One called `.cursor()`
   while production never did, which hid a defect through thirteen reviews.

## File structure

| File | Responsibility |
| --- | --- |
| `urd.py` | Gains `project_slug(project)`. `seed_from_env` calls it instead of deriving inline, so the wizard and the environment path cannot drift. |
| `wizard.py` | Gains `Status`, `Discovery`, `discover(jira, project)` and `propose(statuses)`. Splits `_REQUIRED` into what a scope check needs and what `derive` needs. Still imports no Flask. |
| `views_wizard.py` | Two pages instead of one. |
| `test_webapp.py` | Tests for `project_slug` (where the `seed_from_env` tests already live). |
| `test_wizard.py` | Tests for discovery and the proposals. |
| `test_views_wizard.py` | Tests for the two-page flow. |

## Parallelisation

```
Wave 1   T1 urd.project_slug            T2 wizard.discover + propose
         urd.py, test_webapp.py         wizard.py, test_wizard.py
                              |
Wave 2   T3 the two-page flow
         views_wizard.py, test_views_wizard.py
```

Tasks 1 and 2 share no file and can run concurrently. Task 3 consumes both, so it
waits. Run the two concurrently in one tree: a worktree was tried on this repository
and every agent stalled without producing anything.

---

### Task 1: One slug derivation, shared

**Files:**
- Modify: `urd.py` (add `project_slug`, and use it in `seed_from_env` around line 1291)
- Modify: `test_webapp.py` (append)

**Interfaces:**
- Produces: `urd.project_slug(project) -> str`. Takes a project key or a comma-separated list, returns the derived slug for the first key. May return a string the registry will reject (empty, or leading hyphen); validating is the caller's job, and `seed_from_env` already catches that `ValueError`.

- [ ] **Step 1: Write the failing tests**

```python
# append to test_webapp.py
def test_project_slug_lowercases_and_takes_the_first_key():
    assert urd.project_slug("PROJ") == "proj"
    assert urd.project_slug("PROJ,OTHER") == "proj"
    assert urd.project_slug("  PROJ  ") == "proj"


def test_project_slug_replaces_what_the_charset_refuses():
    """The registry accepts [a-z0-9][a-z0-9-]* only, so anything else has to
    become a hyphen rather than reaching the filesystem."""
    assert urd.project_slug("MY PROJ") == "my-proj"
    assert urd.project_slug("A_B") == "a-b"


def test_project_slug_can_return_something_the_registry_rejects():
    """Deliberately not validated here: seed_from_env already catches the
    ValueError and the wizard shows it on the page, and two validators for one
    rule is how they drift."""
    assert urd.project_slug(",") == ""
    assert urd.project_slug("!!!") == "---"


def test_seed_from_env_and_the_wizard_derive_the_same_slug():
    """One function, so the environment path and the form cannot disagree about
    what a project key becomes on disk. This is the assertion that keeps them
    together; without it the two could drift silently."""
    volume = tempfile.mkdtemp()
    registry = projects_mod.ProjectRegistry(volume)
    urd.seed_from_env(registry, {"URD_SITE": "example.invalid",
                                 "URD_PROJECT": "PROJ,OTHER",
                                 "URD_EMAIL": "a@b.c",
                                 "URD_SINCE": "2026-01-01"})
    seeded = [p.slug for p in registry.projects()]
    assert seeded == [urd.project_slug("PROJ,OTHER")], seeded
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run --with duckdb --with flask python test_webapp.py`
Expected: `AttributeError: module 'urd' has no attribute 'project_slug'`. Confirm each of the four individually, and record the assertion that fired.

- [ ] **Step 3: Write the implementation**

In `urd.py`, above `seed_from_env`:

```python
def project_slug(project):
    """The database filename and URL segment a project key becomes.

    Shared with the setup form rather than duplicated: the slug is what the file
    is called and what the URL says, so two derivations would mean the
    environment path and the form disagreeing about where a project lives.

    Not validated here. It can return a string ProjectRegistry.add refuses, and
    that refusal is the one place the charset is enforced.
    """
    return re.sub(r"[^a-z0-9-]", "-", (project or "").split(",")[0].strip().lower())
```

Then in `seed_from_env`, replace the inline derivation with `slug = project_slug(project)`.

- [ ] **Step 4: Run the tests and the linters**

Run: `uv run --with duckdb --with flask python test_webapp.py` then `./tests/run.sh`
Expected: both green, `test_urd.py` still 305.

Run: `uv run --with ruff ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Mutation-check the tests**

`cp urd.py /tmp/urd.py.bak` first and restore from that copy. Never `git checkout`, which reverts to HEAD and deletes uncommitted work.

| Mutation | Must break |
| --- | --- |
| Drop `.lower()` | `test_project_slug_lowercases_and_takes_the_first_key` |
| Drop `.split(",")[0]` | the same test, on the two-key case |
| `seed_from_env` derives its slug inline again, differently (use `project.lower()` with no substitution) | `test_seed_from_env_and_the_wizard_derive_the_same_slug` |

- [ ] **Step 6: Commit**

```bash
git add urd.py test_webapp.py
git commit -m "refactor(urd): one slug derivation, shared with the setup form

The slug is the database filename and the URL segment, so deriving it in two
places would mean the environment path and the setup form disagreeing about where
a project lives. A test asserts both produce the same string for one key.

Deliberately not validated here: it can return something ProjectRegistry.add
refuses, and that refusal is the single place the charset is enforced."
```

---

### Task 2: Discovery and the proposals

**Files:**
- Modify: `wizard.py`
- Modify: `test_wizard.py` (append)

**Interfaces:**
- Consumes: `urd.Jira.statuses()` returning a list of `{"name": str, "statusCategory": {"key": str} | None}`, and `urd.Jira.project_statuses(key)` returning a list of issue types, each `{"statuses": [{"name": str}, ...]}`. Both raise `SystemExit` on a non-200.
- Produces:
  - `wizard.Status`, a `NamedTuple` with `name: str`, `category: str` (one of `new`, `indeterminate`, `done`, or `""` when the API sent none).
  - `wizard.Discovery`, a `NamedTuple` with `statuses: list[Status]` and `problem: str`.
  - `wizard.discover(proposal, token, opener=None) -> Discovery`. Never raises; a failure comes back as `problem` with an empty list.
  - `wizard.propose(statuses) -> dict` with keys `status_order`, `start_status`, `review_status`, `abandoned_status`, all strings, empty when nothing fits.
  - `wizard.REQUIRED_FOR_SCOPE` and `wizard.REQUIRED_FOR_DERIVE`, replacing `_REQUIRED`.

- [ ] **Step 1: Write the failing tests**

```python
# append to test_wizard.py
_INSTANCE_STATUSES = [
    {"name": "To Do", "statusCategory": {"key": "new"}},
    {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
    {"name": "Code Review", "statusCategory": {"key": "indeterminate"}},
    {"name": "Blocked", "statusCategory": {"key": "indeterminate"}},
    {"name": "Done", "statusCategory": {"key": "done"}},
    {"name": "Won't Do", "statusCategory": {"key": "done"}},
    {"name": "Retired", "statusCategory": {"key": "new"}},
    # The real API does send this, and test_urd.py already pins the case.
    {"name": "Odd", "statusCategory": None},
]

_WORKFLOW = [
    {"statuses": [{"name": "To Do"}, {"name": "In Progress"}]},
    {"statuses": [{"name": "Code Review"}, {"name": "Blocked"},
                  {"name": "Done"}, {"name": "Won't Do"}, {"name": "Odd"}]},
]


def _discovery_opener(statuses=None, workflow=None, fail=None):
    """Answers the two discovery endpoints. `fail` names a URL fragment that
    should return 403 instead, so the degradation path is exercised with the
    real client rather than a stub."""
    def opener(url, headers):
        if fail and fail in url:
            return 403, b'{"message": "no"}'
        if "/project/" in url:
            return 200, json.dumps(workflow if workflow is not None else _WORKFLOW).encode()
        if url.rstrip("/").endswith("/status"):
            payload = statuses if statuses is not None else _INSTANCE_STATUSES
            return 200, json.dumps(payload).encode()
        raise AssertionError(f"unexpected request: {url}")
    return opener


def test_discovery_keeps_only_the_project_s_own_statuses():
    """Retired is in the instance list but not this project's workflow, so it
    must not reach the proposal."""
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    assert found.problem == ""
    assert "Retired" not in [s.name for s in found.statuses]
    assert "To Do" in [s.name for s in found.statuses]


def test_discovery_attaches_the_category_from_the_instance_list():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    by_name = {s.name: s.category for s in found.statuses}
    assert by_name["To Do"] == "new"
    assert by_name["In Progress"] == "indeterminate"
    assert by_name["Done"] == "done"


def test_a_status_the_instance_list_does_not_describe_has_no_category():
    """statusCategory can be null, and a status in the workflow can be absent
    from the instance list entirely. Both end up uncategorised rather than
    guessed at."""
    workflow = [{"statuses": [{"name": "To Do"}, {"name": "Odd"},
                              {"name": "Unlisted"}]}]
    found = wizard.discover(_proposal(), "tok",
                            opener=_discovery_opener(workflow=workflow))
    by_name = {s.name: s.category for s in found.statuses}
    assert by_name["Odd"] == ""
    assert by_name["Unlisted"] == ""


def test_discovery_reports_a_refusal_instead_of_raising():
    """Either call can 403 on a restricted project, and setup must still finish."""
    found = wizard.discover(_proposal(), "tok",
                            opener=_discovery_opener(fail="/project/"))
    assert found.statuses == []
    assert "403" in found.problem or "could not" in found.problem.lower()


def test_the_proposed_order_is_category_order_with_uncategorised_last():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    order = wizard.propose(found.statuses)["status_order"].split(",")
    assert order[0] == "To Do"
    assert order.index("In Progress") < order.index("Done")
    assert order[-1] == "Odd", order


def test_the_first_indeterminate_status_is_proposed_as_the_start():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    assert wizard.propose(found.statuses)["start_status"] == "In Progress"


def test_a_review_looking_status_is_proposed_as_review():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    assert wizard.propose(found.statuses)["review_status"] == "Code Review"


def test_no_review_looking_status_leaves_review_blank():
    """A wrong guess here silently mislabels every review measurement, so
    nothing is better than something."""
    statuses = [wizard.Status("To Do", "new"), wizard.Status("Doing", "indeterminate")]
    assert wizard.propose(statuses)["review_status"] == ""


def test_rejection_looking_done_statuses_are_proposed_as_abandoned():
    found = wizard.discover(_proposal(), "tok", opener=_discovery_opener())
    assert wizard.propose(found.statuses)["abandoned_status"] == "Won't Do"


def test_propose_on_nothing_returns_four_empty_strings():
    """The degradation path renders these into the form, so they have to be
    strings rather than None."""
    proposed = wizard.propose([])
    assert set(proposed) == {"status_order", "start_status", "review_status",
                             "abandoned_status"}
    assert all(v == "" for v in proposed.values())


def test_validate_no_longer_demands_the_workflow_fields():
    """Page one has no status fields to send. Whether a scope works against Jira
    has nothing to do with the workflow ordering, so validate stopped asking."""
    assert "status_order" not in wizard.REQUIRED_FOR_SCOPE
    assert "start_status" not in wizard.REQUIRED_FOR_SCOPE
    assert "status_order" in wizard.REQUIRED_FOR_DERIVE
    result = wizard.validate(_proposal(status_order="", start_status=""), "tok",
                             opener=_ok_opener(3))
    assert result.ok is True, result.problem
```

`test_wizard.py` already imports `json`, verified, so no import change is needed.

Amend the existing `test_an_incomplete_proposal_is_refused_before_any_request`: its loop covers `_REQUIRED`, which no longer exists. Point it at `wizard.REQUIRED_FOR_SCOPE` and drop `status_order` and `start_status` from the fields it expects to be refused.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run --with duckdb --with flask python test_wizard.py`
Expected: `AttributeError: module 'wizard' has no attribute 'discover'`. Confirm each of the eleven individually and record the assertion that fired for each. Two of them (`_a_status_the_instance_list_does_not_describe`, `_no_review_looking_status`) construct `wizard.Status` directly, so they fail on the missing type first; that is expected.

- [ ] **Step 3: Write the implementation**

```python
# in wizard.py, replacing _REQUIRED
# What a scope check needs. The workflow fields are not here: whether a scope
# works against Jira has nothing to do with how its statuses are ordered, and
# page one of the form has not asked about them yet.
REQUIRED_FOR_SCOPE = ("site", "email", "project", "since")
# What derive refuses to run without. Checked when the scope is written, not
# when it is validated.
REQUIRED_FOR_DERIVE = ("status_order", "start_status")

_CATEGORY_ORDER = ("new", "indeterminate", "done")
# Substring matches on a status name, lowercased. Guesses, presented as guesses.
_REVIEW_HINTS = ("review", "qa")
_ABANDONED_HINTS = ("won't do", "wont do", "will not do", "rejected",
                    "cancelled", "canceled", "declined", "duplicate")


class Status(NamedTuple):
    name: str
    category: str


class Discovery(NamedTuple):
    statuses: list
    problem: str = ""


def discover(proposal, token, opener=None):
    """The project's statuses with their categories, from two calls.

    /project/{key}/statuses says which statuses this workflow uses but is not
    relied on for categories; /status carries statusCategory for the whole
    instance. Intersecting the two gives a list that is both scoped to the
    project and categorised, without sampling tickets and without needing the
    admin-only transition graph.

    Never raises. Either call can 403 on a restricted project, and a lost hint
    must not stop someone finishing setup.
    """
    jira = urd.Jira(proposal.site, proposal.email, token, opener=opener)
    try:
        workflow = jira.project_statuses(proposal.project.split(",")[0].strip())
        instance = jira.statuses()
    except SystemExit as exc:
        return Discovery([], f"could not read the workflow's statuses: {exc}")

    category = {}
    for entry in instance:
        name = entry.get("name")
        if name:
            category[name] = ((entry.get("statusCategory") or {}).get("key") or "")

    names = []
    for issue_type in workflow:
        for status in issue_type.get("statuses", []):
            name = status.get("name")
            if name and name not in names:
                names.append(name)
    return Discovery([Status(n, category.get(n, "")) for n in names])


def propose(statuses):
    """Prefill values for the four workflow fields.

    Only status_order is derived: it is every status the workflow uses, in
    category order. The other three are name guesses, and the page says so.
    Ordering within a category needs the transition graph, which needs admin
    rights, so derive's own listing after the first sync is what refines it.

    Uncategorised statuses sort last rather than being guessed into a bucket.
    """
    def rank(status):
        try:
            return _CATEGORY_ORDER.index(status.category)
        except ValueError:
            return len(_CATEGORY_ORDER)

    ordered = sorted(statuses, key=rank)
    moving = [s for s in ordered if s.category == "indeterminate"]
    done = [s for s in ordered if s.category == "done"]

    def first_hint(candidates, hints):
        for status in candidates:
            if any(h in status.name.lower() for h in hints):
                return status.name
        return ""

    review = first_hint(moving, _REVIEW_HINTS)
    return {
        "status_order": ",".join(s.name for s in ordered),
        "start_status": moving[0].name if moving else "",
        "review_status": review,
        "abandoned_status": ",".join(
            s.name for s in done
            if any(h in s.name.lower() for h in _ABANDONED_HINTS)),
    }
```

Update `validate` to iterate `REQUIRED_FOR_SCOPE` instead of `_REQUIRED`. `wizard.py` already imports `NamedTuple` from `typing`, verified, so no import change is needed.

- [ ] **Step 4: Run the tests and the linters**

Run: `uv run --with duckdb --with flask python test_wizard.py` then `./tests/run.sh`
Expected: both green, `test_urd.py` still 305.

Run: `uv run --with ruff ruff check .` and `./tests/no-leaks.sh .`
Expected: clean.

- [ ] **Step 5: Mutation-check the tests**

`cp wizard.py /tmp/wizard.py.bak` first and restore from that copy. Never `git checkout`.

| Mutation | Must break |
| --- | --- |
| `discover` returns every instance status, not just the workflow's | `test_discovery_keeps_only_the_project_s_own_statuses` |
| `category.get(n, "")` becomes `category.get(n, "new")` | `test_a_status_the_instance_list_does_not_describe_has_no_category` |
| `discover` lets the `SystemExit` propagate | `test_discovery_reports_a_refusal_instead_of_raising` |
| `rank` returns `-1` for an uncategorised status instead of last | `test_the_proposed_order_is_category_order_with_uncategorised_last` |
| `first_hint` returns the first candidate when nothing matches | `test_no_review_looking_status_leaves_review_blank` |
| `REQUIRED_FOR_SCOPE` keeps `status_order` | `test_validate_no_longer_demands_the_workflow_fields` |

- [ ] **Step 6: Commit**

```bash
git add wizard.py test_wizard.py
git commit -m "feat(setup): discover a project's statuses and propose the workflow fields

Intersects the two calls the client already had and sync already made: the
project's own statuses from /project/{key}/statuses, their categories from
/status. Authoritative and one request each, where sampling tickets would have
been a search plus a fetch per issue and would have missed any status no current
ticket occupies.

Only status_order is derived. Start, review and abandoned status are name
guesses, and propose keeps them separable so the page can say which is which.
Ordering within a category needs the admin-only transition graph, so derive's
listing after the first sync stays the thing that refines it.

Neither call can stop setup: a 403 on a restricted project comes back as a
problem string with an empty list.

validate no longer demands status_order or start_status, since whether a scope
works against Jira has nothing to do with how its statuses are ordered, and page
one of the form has not asked yet."
```

---

### Task 3: The two-page flow

**Files:**
- Modify: `views_wizard.py`
- Modify: `test_views_wizard.py`

**Interfaces:**
- Consumes: `urd.project_slug(project)` from Task 1; `wizard.discover(proposal, token, opener=None)`, `wizard.propose(statuses)`, `wizard.Status`, `wizard.REQUIRED_FOR_DERIVE` from Task 2; the existing `wizard.Proposal`, `wizard.validate`, `wizard.apply`, `render.notice`, `render.esc`, `registry.add`.
- Produces: no new interface. `/setup` becomes two pages.

- [ ] **Step 1: Write the failing tests**

```python
# append to test_views_wizard.py
_SCOPE = {"site": "example.invalid", "email": "a@b.c", "project": "PROJ",
          "component": "TEAM", "since": "2026-01-01"}


def _found(names_and_categories):
    return wizard_mod.Discovery(
        [wizard_mod.Status(n, c) for n, c in names_and_categories])


def _patched(monkey, validate=None, discover=None):
    """Replaces both network calls. Every test here goes through the real route,
    so without this the route would reach urd.token() and a live request."""
    monkey["validate"] = wizard_mod.validate
    monkey["discover"] = wizard_mod.discover
    wizard_mod.validate = validate or (
        lambda p, t, opener=None: wizard_mod.Result(True, who="A Person", issues=751))
    wizard_mod.discover = discover or (
        lambda p, t, opener=None: _found([("To Do", "new"),
                                          ("In Progress", "indeterminate"),
                                          ("Done", "done")]))


def _restore(monkey):
    wizard_mod.validate = monkey["validate"]
    wizard_mod.discover = monkey["discover"]


def test_page_one_asks_only_what_the_operator_knows():
    body = test_helpers.client(test_helpers.registry()).get(
        "/setup").get_data(as_text=True)
    for field in ("site", "email", "project", "component", "since"):
        assert f'name="{field}"' in body, field
    for field in ("slug", "status_order", "start_status", "review_status",
                  "abandoned_status"):
        assert f'name="{field}"' not in body, f"{field} should be on page two"


def test_page_two_prefills_the_workflow_fields_from_what_was_found():
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    assert 'value="To Do,In Progress,Done"' in body
    assert 'value="In Progress"' in body
    assert 'name="confirm"' in body


def test_page_two_prefills_the_slug_from_the_project_key():
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    assert f'value="{urd.project_slug("PROJ")}"' in body


def test_page_two_writes_nothing_until_confirmed():
    registry = test_helpers.registry()
    monkey = {}
    _patched(monkey)
    try:
        test_helpers.client(registry).post("/setup", data=_SCOPE)
    finally:
        _restore(monkey)
    assert registry.projects() == [], "page two must not create a database"


def test_confirming_writes_the_scope_including_the_discovered_workflow():
    registry = test_helpers.registry()
    monkey = {}
    _patched(monkey)
    try:
        response = test_helpers.client(registry).post("/setup", data={
            **_SCOPE, "slug": "proj", "status_order": "To Do,In Progress,Done",
            "start_status": "In Progress", "review_status": "",
            "abandoned_status": "", "confirm": "yes"})
    finally:
        _restore(monkey)
    assert response.status_code == 302
    project = registry.get("proj")
    assert project is not None
    assert urd.load_scope(project.con)["status_order"] == "To Do,In Progress,Done"


def test_a_failed_discovery_still_reaches_page_two():
    """A 403 on a restricted project costs the prefill, not the setup."""
    monkey = {}
    _patched(monkey, discover=lambda p, t, opener=None: wizard_mod.Discovery(
        [], "could not read the workflow's statuses: 403"))
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    assert "could not read the workflow's statuses: 403" in body
    assert 'name="status_order"' in body, "the field must still be there to type into"
    assert 'name="confirm"' in body, "setup must still be completable"


def test_confirming_without_a_status_order_is_refused_with_a_message():
    """derive refuses without it, and a project that cannot derive dead-ends on
    a page whose only action repeats the failure."""
    registry = test_helpers.registry()
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(registry).post("/setup", data={
            **_SCOPE, "slug": "proj", "status_order": "", "start_status": "",
            "review_status": "", "abandoned_status": "",
            "confirm": "yes"}).get_data(as_text=True)
    finally:
        _restore(monkey)
    assert "status order" in body.lower()
    assert registry.projects() == []


def test_page_two_says_which_values_are_guesses():
    """A guess presented as an answer is worse than a blank field, and the
    ordering within a category is genuinely not known here."""
    monkey = {}
    _patched(monkey)
    try:
        body = test_helpers.client(test_helpers.registry()).post(
            "/setup", data=_SCOPE).get_data(as_text=True)
    finally:
        _restore(monkey)
    assert "guess" in body.lower()
    assert "derive" in body.lower(), "the page should name what refines it"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run --with duckdb --with flask python test_views_wizard.py`
Expected: the page-one test fails because `slug` is still on page one. Confirm each of the eight individually and record what fired.

- [ ] **Step 3: Write the implementation**

Replace `views_wizard.py`'s `_FIELDS`, `_form` and `submit`:

```python
_SCOPE_FIELDS = ("site", "email", "project", "component", "since")
_WORKFLOW_FIELDS = ("slug", "status_order", "start_status", "review_status",
                    "abandoned_status")


def _inputs(names, values):
    return "".join(
        f'<label>{render.esc(name.replace("_", " "))} '
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
    lines = [message] if message else []
    if found.problem:
        lines.append(found.problem)
        lines.append("Type the workflow fields yourself, or leave them and let "
                     "derive list what it finds after the first sync.")
    else:
        lines.append("Status order is every status this project uses, in category "
                     "order. Start, review and abandoned status are guesses from "
                     "the names; check them.")
        lines.append("Ordering inside a category needs the transition graph, which "
                     "needs admin rights, so derive prints a better order from real "
                     "history after the first sync.")
    table = ""
    if found.statuses:
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
        table + _inputs(_WORKFLOW_FIELDS, values)
        + _hidden(_SCOPE_FIELDS, values)
        + '<input type="hidden" name="confirm" value="yes">'
        + '<button type="submit">Confirm and add</button>',
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
            values, wizard.Discovery([]),
            "missing: " + ", ".join(f.replace("_", " ") for f in missing))

    try:
        project = registry.add(values["slug"])
    except ValueError as exc:
        return _workflow_page(values, wizard.Discovery([]), str(exc))
    wizard.apply(project.con, _proposal_from(values))
    return flask.redirect(f"/{values['slug']}/")
```

- [ ] **Step 4: Run the tests and the linters**

Run: `uv run --with duckdb --with flask python test_views_wizard.py` then `./tests/run.sh`
Expected: both green, `test_urd.py` still 305.

Run: `uv run --with ruff ruff check .` and `./tests/no-leaks.sh .`
Expected: clean.

- [ ] **Step 5: Confirm no test reaches the network**

Every test here drives the real route, which calls `urd.token()` and then two Jira functions. Both are patched, but prove it rather than assume: run the suite with `urllib.request.OpenerDirector.open` replaced by something that records and raises, and report the count. It must be zero.

- [ ] **Step 6: Mutation-check the tests**

`cp views_wizard.py /tmp/views_wizard.py.bak` first and restore from that copy.

| Mutation | Must break |
| --- | --- |
| Put `slug` back in `_SCOPE_FIELDS` | `test_page_one_asks_only_what_the_operator_knows` |
| `submit` skips `wizard.propose`, leaving the fields empty | `test_page_two_prefills_the_workflow_fields_from_what_was_found` |
| `registry.add` is called before the confirm check | `test_page_two_writes_nothing_until_confirmed` |
| Drop the `REQUIRED_FOR_DERIVE` check | `test_confirming_without_a_status_order_is_refused_with_a_message` |
| `_workflow_page` drops `found.problem` from its lines | `test_a_failed_discovery_still_reaches_page_two` |
| `_workflow_page` drops the guess sentence | `test_page_two_says_which_values_are_guesses` |

- [ ] **Step 7: Verify by hand, once**

```bash
uv run --with duckdb --with flask python urd.py serve --volume /tmp/urd-setup-demo
```

Open `/setup`, submit a real scope, and check page two lists the statuses your own instance uses with sensible prefills. This is the first time the two calls run against a real Jira, and a shape the fakes got wrong will only show here. Report what the prefills came out as.

- [ ] **Step 8: Commit**

```bash
git add views_wizard.py test_views_wizard.py
git commit -m "feat(setup): ask for the workflow on a second page, prefilled

Page one asks only what the operator knows: site, email, project, component and
since. Submitting validates the scope, then discovers the project's statuses and
prefills the workflow fields on page two, so nobody has to know their status names
before using the tool.

The slug is no longer a question. It comes from the project key through
urd.project_slug and is shown on page two, editable for the one case that needs
it.

Page two says which values are derived and which are guesses, and names derive as
the thing that refines the ordering after a first sync. A discovery that 403s
still reaches page two with the fields blank and the reason shown, because a lost
hint must not stop setup.

Confirming refuses a blank status order rather than writing a project that cannot
derive and dead-ends on a page whose only action repeats the failure."
```

---

## Self-review

**Spec coverage.**

| Spec section | Task |
| --- | --- |
| Slug derived, not asked | 1, and shown on page two by 3 |
| Two pages, no server session, hidden fields | 3 |
| Discovery from the two existing calls, intersected | 2 |
| Sampling tickets rejected, with reasons | 2 (commit message and docstring) |
| `statusCategory` can be null; uncategorised sorts last and is called out | 2, and the table in 3 |
| status_order derived; start, review, abandoned guessed | 2 |
| Within-category ordering impossible here; derive refines it | 2, said on the page by 3 |
| Degradation: discovery never blocks setup | 2 returns a problem, 3 renders it |
| Out of scope: component picker, reconfiguration, transition graph | Absent by construction |
| Open decision 1, page two not skippable | 3, `REQUIRED_FOR_DERIVE` check |
| Open decision 2, show the evidence | 3, the `<details>` table |
| Open decision 3, both calls | 2 |

**Gap found and closed.** The spec did not mention that `wizard._REQUIRED` includes `status_order` and `start_status`, so page one could not have validated without them. Task 2 splits it into `REQUIRED_FOR_SCOPE` and `REQUIRED_FOR_DERIVE`, and amends the existing test that loops over the old name. Without this the plan would have failed at Task 3.

**Placeholder scan.** No TBD, no "add error handling", no "similar to Task N". Every code step carries the code.

**Type consistency.** `Status(name, category)` and `Discovery(statuses, problem)` are defined in Task 2 and constructed in Task 3's `_found` helper and its `Discovery([])` calls. `propose` returns exactly the four keys Task 3 merges into `values`. `project_slug` is defined in Task 1 and called in Task 3. `REQUIRED_FOR_DERIVE` is defined in Task 2 and iterated in Task 3.

**On this plan's own test code**, against the four rules at the top: every fixture value is a real status name or category string that the code path actually parses; the assertions use whole values (`value="To Do,In Progress,Done"`, the full problem sentence) rather than substrings that page furniture could satisfy, except `test_confirming_without_a_status_order_is_refused_with_a_message` and `test_page_two_says_which_values_are_guesses`, which check `"status order"`, `"guess"` and `"derive"` in the page and **could** be satisfied by the explanatory prose; Task 3's implementer should tighten those two to the exact sentences once the wording is settled, and the mutation rows for both will show whether they discriminate. Every mutation row names a test defined in this plan. Every test in Task 3 drives the real route rather than calling the page builders directly.
