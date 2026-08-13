#!/bin/sh
# Fails if anything employer-specific reaches the public repository: instance
# host, project keys, team name, ticket keys or colleagues' names. Scans the
# working tree AND every commit message, because git history outlives a fix.
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

# LICENSE legitimately carries the author's own name, so it is excluded from the
# name check by scanning it separately for the loose terms only.
if grep -rniI --exclude-dir=.git --exclude=no-leaks.sh -e "$loose" "$target"; then
	hits=1
fi
if grep -rnI --exclude-dir=.git --exclude=no-leaks.sh --exclude=LICENSE -e "$strict" "$target"; then
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
