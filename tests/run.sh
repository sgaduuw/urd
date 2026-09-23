#!/bin/sh
# One gate for local work and CI. Assert-based tests must run unoptimized.
set -eu
cd "$(dirname "$0")/.."
unset PYTHONOPTIMIZE
for f in test_urd.py test_projects.py test_wizard.py test_container.py test_webapp.py \
	test_views_report.py test_views_jobs.py test_views_wizard.py \
	test_security.py test_sync_safety.py test_checks.py; do
	[ -f "$f" ] || { echo "Missing required test: $f" >&2; exit 1; }
done
uv tool run ruff==0.16.8 check .
shellcheck tests/*.sh
sh tests/no-leaks.sh
for f in test_*.py; do
	[ "$f" = test_helpers.py ] && continue
	printf '== %s\n' "$f"
	# Capture output without losing the test process's status in a pipeline.
	if out=$(uv run --isolated --with-requirements requirements.txt python "$f" 2>&1); then
		printf '%s\n' "$out" | tail -1
	else
		printf '%s\n' "$out" | tail -20
		echo "FAILED: $f"
		exit 1
	fi
done
