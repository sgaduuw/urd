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
        if not _SLUG.fullmatch(slug or ""):
            raise ValueError(f"not a usable project slug: {slug!r}")
        if slug in self._projects:
            return self._projects[slug]
        project = Project(slug, os.path.join(self.volume, f"{slug}.duckdb"))
        self._projects[slug] = project
        return project


def _default_jira(scope):
    return urd.Jira(scope["site"], scope["email"], urd.token())


def start_refresh(project, jira_factory=None):
    """Sync then derive on a background thread. False if it did not start.

    The lock is taken here rather than inside the thread, so the caller learns
    immediately whether it started. Two clicks would otherwise be two threads
    writing one database.
    """
    if project.con is None:
        project.job.state = "failed"
        project.job.message = project.error or "this database could not be opened"
        return False
    if not project.configured():
        project.job.state = "failed"
        project.job.message = "no scope yet: finish setup before refreshing"
        return False
    if not project.lock.acquire(blocking=False):
        return False

    project.job.state = "running"
    project.job.progress = "starting"
    project.job.message = ""
    factory = jira_factory or _default_jira

    def run():
        try:
            scope = urd.load_scope(project.con)
            project.job.progress = "syncing"
            urd.sync(project.con, factory(scope))
            project.job.progress = "deriving"
            urd.derive(project.con, scope["status_order"], scope["start_status"],
                       scope["review_status"], scope["abandoned_status"])
            project.job.state = "idle"
            project.job.progress = ""
        except BaseException as exc:      # noqa: BLE001 - SystemExit included
            # SystemExit is how urd reports every operational failure, and it is
            # not an Exception, so a bare `except Exception` would let a failed
            # sync kill the thread silently with the job stuck on "running".
            project.job.state = "failed"
            project.job.message = str(exc) or type(exc).__name__
        finally:
            project.lock.release()

    threading.Thread(target=run, name=f"refresh-{project.slug}", daemon=True).start()
    return True
