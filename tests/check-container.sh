#!/bin/sh
# Build first, then pass its tag. Requires Docker, or CONTAINER_ENGINE=podman.
set -eu
engine=${CONTAINER_ENGINE:-docker}
image=${1:?usage: sh tests/check-container.sh IMAGE}
volume=$($engine volume create "urd-smoke-$$")
trap '$engine volume rm "$volume" >/dev/null' EXIT
$engine run --rm -v "$volume:/var/lib/urd" "$image" python -c '
import os
import duckdb
assert os.getuid() != 0, "container runs as root"
with duckdb.connect("/var/lib/urd/smoke.duckdb") as db:
    db.execute("CREATE TABLE smoke AS SELECT 42 AS value")
'
$engine run --rm -v "$volume:/var/lib/urd" "$image" python -c '
import os
import duckdb
assert os.getuid() != 0, "container runs as root"
with duckdb.connect("/var/lib/urd/smoke.duckdb") as db:
    assert db.execute("SELECT value FROM smoke").fetchone() == (42,)
'
echo 'container: non-root, writable volume, restart persistence passed'
