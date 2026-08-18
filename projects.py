"""One Project per database file, and the registry that owns them all.

Imports no Flask. The server's concurrency rests on two measured facts about
DuckDB, both recorded in the design spec: within one process a cursor can read
while a write transaction is open and sees the pre-write snapshot, and a second
process cannot open the file at all while a writer holds it, not even read-only.
So each file is opened exactly once, here, for the process's lifetime.
"""
import os
import re
import threading

import urd

# Lowercase, digits and hyphens. Slugs arrive from a URL path and a form field,
# so anything that could climb out of the volume is refused rather than resolved.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class JobState:
    """What a project's background sync is doing, held in memory.

    In memory and not in the database on purpose: a running sync holds the write
    transaction, so a job that reported itself through a table could not report
    itself at all while it ran.
    """

    def __init__(self):
        self.state = "idle"
        self.progress = ""
        self.message = ""

    def as_dict(self):
        return {"state": self.state, "progress": self.progress, "message": self.message}


class Project:
    def __init__(self, slug, path):
        self.slug = slug
        self.path = path
        self.lock = threading.Lock()
        self.job = JobState()
        self.con = None
        self.error = None
        try:
            self.con = urd.open_db(path)
        except Exception as exc:      # noqa: BLE001 - any failure is "listed as broken"
            # Listed, not raised: one unreadable file must not make every other
            # project unreachable.
            self.error = f"{type(exc).__name__}: {exc}"

    def configured(self):
        if self.con is None:
            return False
        scope = urd.load_scope(self.con)
        return bool(scope["site"] and scope["project"] and scope["earliest_since"])


class ProjectRegistry:
    def __init__(self, volume):
        self.volume = volume
        self._projects = {}
        os.makedirs(volume, exist_ok=True)
        for name in sorted(os.listdir(volume)):
            if name.endswith(".duckdb"):
                slug = name[: -len(".duckdb")]
                self._projects[slug] = Project(slug, os.path.join(volume, name))

    def projects(self):
        return [self._projects[slug] for slug in sorted(self._projects)]

    def get(self, slug):
        return self._projects.get(slug)

    def add(self, slug):
        if not _SLUG.match(slug or ""):
            raise ValueError(f"not a usable project slug: {slug!r}")
        if slug in self._projects:
            return self._projects[slug]
        project = Project(slug, os.path.join(self.volume, f"{slug}.duckdb"))
        self._projects[slug] = project
        return project
