#!/bin/sh
# Every test file, so the gate stays one command as files multiply. test_urd.py
# alone is 4900 lines, which is why new work gets its own files rather than
# appending to it.
set -eu
cd "$(dirname "$0")/.."
for f in test_urd.py test_projects.py test_wizard.py test_container.py test_webapp.py \
	test_views_report.py test_views_jobs.py test_views_wizard.py; do
	[ -f "$f" ] || continue
	printf '== %s\n' "$f"
	# Piping through tail throws away the test process's own exit status: under
	# `set -eu` without pipefail, a pipeline's status is its last command's
	# (tail's, always 0), so a failing test would print its traceback and this
	# script would still exit 0. tests/no-leaks.sh already carries the same
	# lesson at length (it decides on grep's output, never its exit status);
	# capture the output and branch on the test process's own status instead.
	if out=$(uv run --with duckdb --with flask python "$f" 2>&1); then
		printf '%s\n' "$out" | tail -1
	else
		printf '%s\n' "$out" | tail -20
		echo "FAILED: $f"
		exit 1
	fi
done
