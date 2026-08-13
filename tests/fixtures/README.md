# Fixtures

Hand written, synthetic, and deliberately not captured from a real Jira
instance. Real issue JSON carries display names, email addresses, account ids,
avatar URLs and the site UUID, none of which belong in a public repository, and
scrubbing it is a step that only has to be forgotten once.

Each fixture contains only the fields the code actually reads, which makes the
set of fields `derive` depends on obvious. Project key `PROJ`, component `TEAM`,
people named after trees.

If a real response is ever needed to reproduce a bug, keep it as `*.raw.json`,
which is gitignored.
