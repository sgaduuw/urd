#!/bin/sh
# Fails if anything employer-specific reaches the public repository: instance
# host, project keys, team name, ticket keys or colleagues' names. Scans
# exactly what git would publish (tracked files, plus untracked files that
# are not gitignored), not everything that happens to sit on disk, and every
# commit message, because git history outlives a fix.
#
# Run it against a directory known to contain leaks before trusting a clean
# result here: a pattern that matches nothing looks identical whether the tree
# is clean or the pattern is broken.
#
# ponytail: a literal list, not a config file. It is meant to be read and edited
# by hand when a new term needs excluding.
set -eu
target="${1:-.}"

# Case-insensitive terms.
loose='i3d\|ubisoft\|i3dnet'
# Case-sensitive terms: bare "meta" appears in "metadata" and "metric".
strict='\bMETA\b\|\bFM-[0-9]\|\bISM-[0-9]\|\bITSM\b\|Konstantelos\|Bohbot\|van Gerven\|Holtkamp\|Verhoef\|Nelms\|Haverkamp\|Jura\b'

hits=0

# The set of files that could ever reach the public repository: tracked files,
# plus untracked files a `git add -A` would pick up, minus anything
# gitignored. This is exactly what `git ls-files --cached --others
# --exclude-standard` reports, so a gitignored workspace (such as
# .superpowers/, where a report can legitimately quote these same leak terms
# as test data) is excluded by construction, not by a hardcoded directory
# name. NUL-separated throughout, so a filename with a space or a newline in
# it cannot split into the wrong number of fields.
list="$(mktemp)"
trap 'rm -f "$list"' EXIT
(cd "$target" && git ls-files -z --cached --others --exclude-standard) >"$list"

# Excludes are matched by basename, with an optional path prefix, the same
# scope the old grep --exclude flags had. no-leaks.sh is excluded from both
# checks so its own pattern list cannot self-match; LICENSE is excluded from
# the strict name check only, since it legitimately carries the author's own
# name.
not_self='\(^\|/\)no-leaks\.sh$'
not_license='\(^\|/\)LICENSE$'

# Piping straight into xargs, with no shell variable ever holding the
# filenames, means a zero length list invokes grep zero times rather than a
# grep left blocking on an unexpected stdin.
if grep -z -v -e "$not_self" -- "$list" | (cd "$target" && xargs -0 grep -niIH -e "$loose" --); then
	hits=1
fi
if grep -z -v -e "$not_self" -e "$not_license" -- "$list" \
		| (cd "$target" && xargs -0 grep -nIH -e "$strict" --); then
	hits=1
fi
if git -C "$target" log --format='%H %s%n%b' 2>/dev/null | grep -ni -e "$loose" -e "$strict"; then
	echo "^^ in commit messages"
	hits=1
fi

# Author and committer identity is published with every commit, and a work email
# gives away the employer just as plainly as the project keys do.
if git -C "$target" log --format='%H %ae %ce' 2>/dev/null | grep -ni -e "$loose"; then
	echo "^^ work email in commit identity; fix with git config user.email and rebase"
	hits=1
fi

if [ "$hits" -ne 0 ]; then
	echo "LEAK: the lines above must not be published" >&2
	exit 1
fi
echo "clean: $target"
