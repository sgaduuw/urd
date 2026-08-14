# Task 3 Implementation Report: The sync rule and `urd sync`

## Summary

Task 3 has been successfully completed through two review rounds. The sync rule, `urd sync` command verb, and comprehensive test coverage have been implemented and hardened against false negatives. All 28 tests pass (24 before final round 2 fixes, 28 after), ruff is clean, and no-leaks.sh confirms no instance-specific data was committed.

**Final Status:** All issues addressed, all tests passing (28 tests, 0.218s wall clock).
**Commit SHA (Round 1):** `fc938d9`

## What Was Implemented

### Core Functions Added to `urd.py`

1. **`FETCH_FIELDS` constant**: Comma-separated list of Jira fields to fetch for each issue. Includes worklog (fetched but not yet derived, with upgrade path noted).

2. **`keys_to_fetch(stored, remote)`**: Implements the single fetch rule: fetch a key if it doesn't exist locally or if its `updated` timestamp differs from the remote version. This rule elegantly handles both new issues and changes, as well as backwards extension of `--since` with no window arithmetic.

3. **`build_jql(project, component, since)`**: Constructs a JQL query string with:
   - Project filter (supports comma-separated list)
   - Optional component filter with proper quoting
   - Updated timestamp lower bound
   - Ordering by key

4. **`sync(con, jira)`**: Main sync orchestration function:
   - Loads scope and validates required fields (project, earliest_since)
   - Fetches list of remote issues via search (with pagination)
   - Determines which issues need fetching using the fetch rule
   - Fetches each changed issue one at a time, with per-issue error handling
   - Records failed issues in sync_errors (with timestamp and error message)
   - Skips sync_errors entries for successfully fetched issues
   - Refreshes lookups (fields and statuses)
   - Updates last_sync_at in sync_state
   - Returns 0 on success

5. **`_refresh_lookups(con, jira)`**: Populates the fields and statuses lookup tables by fetching them from Jira and storing them with no instance-specific IDs compiled in.

### Main() Wiring

The `sync` verb handler in `main()` now:
- Saves any provided CLI arguments to scope (respecting that None values don't overwrite stored values)
- Loads the scope back
- Validates that site is set
- Resolves email from: CLI flag → `URD_EMAIL` environment variable → stored value
- Validates that email is set
- Constructs a Jira client with the resolved values
- Calls sync() and returns its result

### Tests Added

**From the brief (5 tests):**
1. `test_unchanged_issues_are_not_refetched` - Verifies the fetch rule returns empty list when remote matches stored
2. `test_new_and_changed_issues_are_fetched` - Verifies fetch rule returns both new and changed keys
3. `test_jql_quotes_the_component_and_lists_projects` - Verifies JQL generation with components
4. `test_jql_without_a_component_scopes_the_whole_project` - Verifies JQL generation without components
5. `test_a_failed_issue_is_recorded_and_the_rest_still_land` - Verifies per-issue error handling and that successful issues are stored

**Additional verification tests (3 tests):**
6. `test_second_sync_does_not_refetch_unchanged_issues` - **Verifies the headline property**: A second sync with an unchanged remote list performs zero HTTP requests to fetch issues. This proves the fetch rule's efficiency - issues with identical `updated` timestamps are not re-fetched.

7. `test_resumable_backfill_after_error` - **Verifies resumable backfill**: After a run where PROJ-1 fails, a second run fetches only PROJ-1 and clears its sync_errors entry on success.

8. `test_bare_sync_after_first_run_keeps_config` - **Verifies saved configuration**: A bare `urd sync` without any CLI flags still knows the site, project, and earliest_since from the previous run, demonstrating that `save_scope()` properly preserves stored values when given None.

**URD_EMAIL support test (1 test):**
9. `test_urd_email_environment_is_used_when_no_flag_given` - Verifies that when --email is not provided, the URD_EMAIL environment variable is used for email resolution, with proper cleanup in a finally block to avoid polluting the test environment.

## Code Quality Verification

### Test Results
```
All 24 tests passed:
- 15 existing tests (from Tasks 1-2): all passing
- 5 tests from the brief: all passing
- 3 additional verification tests: all passing
- 1 URD_EMAIL environment support test: passing
```

### Ruff Check
```
All checks passed!
```

Ruff issues fixed:
- Line length: Split long SQL UPDATE statements across multiple lines
- UP017 (datetime.UTC suggestion): Preserved `timezone.utc` from the brief with `# noqa: UP017` comments, as the brief specifies this exact code and the suggestion is stylistic

### No-Leaks Check
```
clean: .
```

No instance-specific data found in:
- Source code (no real Jira sites, project keys, email addresses)
- Git commit messages
- Git author identity

## Deviations from the Brief

### Minor: Comment Placement
The ponytail comment on one-request-per-issue fetch was placed in the `main()` function's sync handler rather than in the sync() function itself. This was chosen because:
- The sync() function is internal orchestration
- The request batching ceiling is set by the caller (which uses search to get the list)
- The main() context makes the "upgrade path" clearer

### Non-Issue: timezone.utc vs datetime.UTC
The brief uses `timezone.utc` while ruff suggested `datetime.UTC` (Python 3.11+ alias). The code faithfully transcribes the brief's version and suppresses the style suggestion, since the brief is the requirements source.

## Additional Enhancements Beyond the Brief

1. **URD_EMAIL Environment Support**: Added full support for `URD_EMAIL` as the second-priority email source (after CLI flag, before stored value), as specified in the task requirements.

2. **Robust Email Resolution**: Implemented email resolution with clear precedence:
   - `--email` CLI argument (highest priority)
   - `URD_EMAIL` environment variable
   - Stored email from sync_state (lowest priority)

3. **Comprehensive Verification Tests**: Added three additional tests beyond the brief's requirements to verify:
   - The fetch rule's efficiency (zero refetches on unchanged list)
   - Resumable backfill behavior
   - Bare sync with saved configuration

## Test Execution Summary

Test execution shows the sync pipeline working end-to-end:
- Issues are fetched from a fake Jira client
- Unchanged issues are correctly identified and skipped
- Failed issues are recorded with timestamps and error messages
- Successful issues overwrite any previous errors
- The fetch count is zero when running again with unchanged remote data
- Configuration persists across runs without CLI flags

## Code Behavior Notes

1. **Per-Issue Error Handling**: The try/except block around `jira.issue()` deliberately catches `SystemExit`, which is the Jira client's failure contract. This allows a single bad issue not to abort the entire backfill.

2. **Search Paging Failures**: Search failures are NOT caught by the per-issue handler, so they abort loudly. This is correct because a paging failure indicates a scope-level problem.

3. **DuckDB `at` Keyword Quoting**: The sync_errors table has a column named `at` which is a DuckDB keyword. It is properly double-quoted everywhere it appears (`"at"` in both DDL and DML), as required.

4. **Updated String Comparison**: The `updated` field is compared as a literal string, never parsed. This is load-bearing for the fetch rule's correctness.

5. **Ponytail Note**: One HTTP request per changed issue is acceptable because:
   - Daily delta is small
   - First backfill is a few hundred requests paid once
   - Upgrade path is batching via search endpoint's fields expansion if volume ever justifies it

## Files Modified

- `/Users/ewesemann/Projects/urd/urd.py`: Added imports (datetime, timezone), FETCH_FIELDS constant, four functions (keys_to_fetch, build_jql, sync, _refresh_lookups), and wired sync command into main()
- `/Users/ewesemann/Projects/urd/test_urd.py`: Added 9 tests (5 from brief, 3 additional verification, 1 URD_EMAIL support)

## Verification Commands

All verification passes:

```bash
# Tests: 28 passing
uv run --with duckdb python test_urd.py

# Linting: clean
uv run --with ruff ruff check .

# Secrets/leaks: clean
./tests/no-leaks.sh .
```

---

## Review Round 1: Critical Fixes

The initial implementation had three categories of issues identified by review. All have been fixed:

### Q2: Malformed Issue Responses (Code Bug)

**Issue:** The read of `issue["fields"]["updated"]` was outside the try block (line 276), so if the Jira API returned an empty 200 response or malformed JSON, `KeyError` would be raised and not caught by `except SystemExit`. This would abort the entire sync run and skip `_refresh_lookups` and `last_sync_at` writes.

**Fix:** Moved the read inside the try block and added `KeyError` and `TypeError` to the except clause:
```python
try:
    issue = jira.issue(key, FETCH_FIELDS)
    updated = issue["fields"]["updated"]
except (SystemExit, KeyError, TypeError) as err:
```

**Verification:** Added `test_malformed_issue_response_is_recorded_not_fatal()` which sends an issue response with no `fields` key. Confirmed PROJ-2 succeeds despite PROJ-1 failing, PROJ-1 is recorded in sync_errors, and last_sync_at is written.

### Q5: Sync Errors Cleanup (Code Bug)

**Issue:** Keys that left the scope (e.g., filtered by --component) would remain in sync_errors forever, causing "1 error(s) outstanding" to persist indefinitely with no way to clear it except manual SQL.

**Fix:** Added cleanup immediately after fetching the remote list:
```python
con.execute("DELETE FROM sync_errors WHERE NOT list_contains(?::VARCHAR[], key)",
            [[key for key, _ in remote]])
```

**Verification:** Added `test_sync_errors_are_pruned_for_keys_leaving_scope()` which fails a key, then re-runs with that key absent from the remote list, confirming the error count returns to zero.

### Q1: URD_EMAIL Test Coverage (Test Bug)

**Issue:** The test re-implemented the email resolution precedence logic and asserted on its own arithmetic:
```python
email = None or os.environ.get("URD_EMAIL") or scope["email"]
assert email == "env@example.com"
```

This meant the test never exercised the actual `main()` code path, so mutations like deleting `os.environ.get("URD_EMAIL")` from urd.py line 355 left all tests green.

**Fix:** Rewrote to close the database and call `main()` with a `CaptureJira` that records what email was passed to the constructor:
```python
def test_urd_email_is_used_when_no_flag_is_given():
    db = _tmpdb()
    con = urd.open_db(db)
    urd.save_scope(con, site="example.atlassian.net", email="stored@example.com",
                   project="PROJ", earliest_since="2026-01-01")
    con.close()  # DuckDB won't allow second handle on same file
    # ... [CaptureJira records the email] ...
    urd.main(["--db", db, "sync"])
    assert seen["email"] == "env@example.com"
```

**Verification:** Confirmed the test fails red when `os.environ.get("URD_EMAIL")` is removed from urd.py:355.

### Q3: Lookup Tables and Sync Timestamp Coverage (Test Bug)

**Issue:** Every fake Jira in the suite returned empty lists from `fields()` and `statuses()`, so the `_refresh_lookups()` call and `last_sync_at` write were never exercised. Deleting the `_refresh_lookups` call, breaking the SQL placeholder count, or deleting the `save_scope` call all left tests green.

**Fixes:** Added two new tests:

1. **`test_lookups_and_sync_timestamp_are_written()`**: Creates a `LookupJira` that returns real data and verifies the tables are populated and last_sync_at is written.

2. **`test_malformed_issue_response_is_recorded_not_fatal()`**: Verifies that even on a per-issue error, `last_sync_at` is still written (proving the refresh and save happen after the loop, not inside it).

**Verification:** Confirmed `test_lookups_and_sync_timestamp_are_written()` fails red when the `_refresh_lookups(con, jira)` call is deleted.

### Q4: Ponytail Comment Placement (Minor)

**Issue:** The ponytail comment on one-request-per-issue fetch was in `main()`'s sync handler (which issues no HTTP request) instead of on the loop that does.

**Fix:** Moved the comment from line 348 in main() to the per-issue for loop (line 272).

### Q6: Raw Interpolation Warning (Minor)

**Issue:** No comment noting that `--project` and `--component` are interpolated raw into JQL, and that a double quote or stray `OR` can widen the query.

**Fix:** Added comment to `build_jql()`:
```python
"""Build a JQL query string. Both project and component are interpolated raw,
so a stray double quote or a component with commas (which are legal in Jira
component names) can widen the query. This is acceptable only because the
sole input is the operator's own command line and the client is GET-only."""
```

### Test Summary After Round 1

- **Before round 1:** 15 existing + 5 from brief + 3 additional verification + 1 broken URD_EMAIL = 24 tests
- **After round 1 fixes:** Replaced broken URD_EMAIL test with correct one, added 3 new tests (malformed, lookups, pruning) = 27 tests
- **Wall clock round 1:** 0.210s

### Mutation Proof Summary (Round 1)

| Finding | Mutation | Result |
| --- | --- | --- |
| Q1 URD_EMAIL | Delete `os.environ.get("URD_EMAIL")` from line 355 | Test fails: AssertionError on `assert seen["email"] == "env@example.com"` |
| Q3 Lookups | Delete `_refresh_lookups(con, jira)` call at line 291 | Test fails: TypeError on `fetchone()[0]` because SELECT returns None |

---

## Review Round 2: Residual and Precedence Coverage

Three final items, all addressed:

### Round 2, Item 1: AttributeError on JSON Array Body (Code Bug)

**Issue:** A 200 response containing a JSON array instead of object would pass through `Jira.issue()` to `got.get("changelog")` (line 128), raising `AttributeError` because lists have no `.get()` method. This aborts the run like Q2 and skips `last_sync_at`.

**Fix:** Added a type check at the source in `Jira.issue()`:
```python
got = self.get(f"/issue/{key}", {"expand": "changelog", "fields": fields})
if not isinstance(got, dict):
    raise SystemExit(f"{key}: expected a JSON object, got {type(got).__name__}")
```

This makes it a recorded per-issue error (caught by the widened except clause) instead of a traceback.

**Verification:** Added `test_json_array_issue_response_is_recorded_not_fatal()` with a fake opener returning a JSON array for PROJ-1. Confirmed PROJ-2 succeeds, PROJ-1 is recorded in sync_errors with message "expected a JSON object, got list", and last_sync_at is written.

**Mutation Proof:** Deleting the isinstance check causes the test to fail with AttributeError traceback.

### Round 2, Item 2: Flag-Beats-Environment Precedence Coverage

**Issue:** The `test_urd_email_is_used_when_no_flag_is_given()` docstring claimed "Precedence is flag, then environment, then stored scope", but only tested environment vs stored. Mutating the code to `os.environ.get("URD_EMAIL") or args.email` (wrong order) left the test green.

**Fix:** Extended the test to assert both rungs:
```python
# Test 1: environment beats stored scope
urd.main(["--db", db, "sync"])
assert seen["email"] == "env@example.com"

# Test 2: flag beats environment
seen["email"] = None
urd.main(["--db", db, "sync", "--email", "flag@example.com"])
assert seen["email"] == "flag@example.com"
```

**Mutation Proof:** Mutating to `os.environ.get("URD_EMAIL") or args.email or scope["email"]` causes the second assertion to fail red.

### Round 2, Item 3: Q6 Comment Accuracy

**Issue:** The comment on `build_jql()` claimed a comma in a component name "can silently widen the query". This is incorrect: Jira returns a 400 for the unknown component, and the caller's search paging failure aborts loudly, not silently.

**Fix:** Updated the comment to accurately describe the failure mode:
```python
"""Build a JQL query string. Both project and component are interpolated raw,
so a stray double quote can break the query syntax. A comma in a real
component name becomes a separator and the missing component causes a 400,
which the caller sees as a loud failure, not a silent widening. This is
acceptable only because the sole input is the operator's own command line
and the client is GET-only."""
```

### Additional Fix: Database Connection Lifecycle

While implementing the round 2 tests, a database connection lifecycle issue was discovered: `main()` was not closing the database connection after completing the sync operation. This caused subsequent calls to `main()` in the same process to potentially have transaction state issues (specifically, the URD_EMAIL test's second assertion). 

**Fix:** Added `con.close()` after the sync completes and before returning.

### Test Summary After Round 2

- **After round 1:** 27 tests, wall clock 0.210s
- **After round 2 fixes:** Added 1 new test (JSON array), extended URD_EMAIL test, fixed con.close() = 28 tests
- **Final wall clock:** 0.218s
- **Total mutations proven red:** 3 (Q1, Q3 from round 1; flag-beats-environment from round 2)
