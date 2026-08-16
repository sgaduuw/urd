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

# Case-insensitive terms. The home-path pattern is here because an agent report
# committed by mistake carried "/Users/<name>/Projects/urd/urd.py" twice, and
# every pattern above it passed: the file named no employer, no team and no
# ticket, only the machine's account name. A tool-generated absolute path is the
# most likely way this repository leaks an identity, and it is invisible to a
# list of terms someone has to think to add.
loose='i3d\|ubisoft\|i3dnet\|/Users/[A-Za-z0-9._-]\+\|/home/[A-Za-z0-9._-]\+'
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

# Decide on grep's OUTPUT, never its exit status. grep exits 1 for a batch
# with no match, an ordinary no-op rather than a failure, and when the file
# list is long enough that xargs splits it across several grep invocations,
# one empty batch next to one real hit aggregates their exit statuses into a
# false "nothing found", even though the hit was already printed. The mirror
# break is an empty or fully excluded file list: xargs then runs grep zero
# times and exits 0 for "nothing to do", which reads as a false "found
# something" if exit status were the signal instead. Capturing output
# sidesteps both: `|| true` stops the ordinary no-match case from tripping
# `set -e`, and it is the presence of output, not either program's exit
# code, that decides `hits`.
loose_hits=$(grep -z -v -e "$not_self" -- "$list" \
		| (cd "$target" && xargs -0 grep -niIH -e "$loose" --) || true)
if [ -n "$loose_hits" ]; then
	printf '%s\n' "$loose_hits"
	hits=1
fi
strict_hits=$(grep -z -v -e "$not_self" -e "$not_license" -- "$list" \
		| (cd "$target" && xargs -0 grep -nIH -e "$strict" --) || true)
if [ -n "$strict_hits" ]; then
	printf '%s\n' "$strict_hits"
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
