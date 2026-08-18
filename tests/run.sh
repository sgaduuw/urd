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
	uv run --with duckdb --with flask python "$f" | tail -1
done
