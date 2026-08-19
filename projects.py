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
# No ^/$ anchors: this is always called through fullmatch, which already anchors
# both ends; keeping them was harmless but redundant.
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


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


def job_message(project):
    """A one-line status worth showing on any page this project appears on, if
    the job has anything to say: the failure message when the last attempt
    failed, or that one is already running. None when idle with nothing to
    report, which is true both before a first run and after a successful one,
    a distinction this deliberately does not have to make since neither case
    is worth a line on the page.

    Shared by every page that offers a Refresh button (the full report and
    the "never synced"/"synced but not derived" notices alike), so a refresh
    that fails is visible wherever it could have been clicked from, not only
    on the one page that happens to already have a report to decorate.
    """
    if project.job.state == "failed" and project.job.message:
        return project.job.message
    if project.job.state == "running":
        return "A refresh is already running for this project."
    return None


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
        # On a cursor, not self.con directly: this is called from request
        # threads and from start_refresh, so it can race a sync's writes to
        # sync_state exactly like a chart query can.
        scope = urd.load_scope(self.con.cursor())
        return bool(scope["site"] and scope["project"] and scope["earliest_since"])


class ProjectRegistry:
    def __init__(self, volume):
        self.volume = volume
        self._projects = {}
        os.makedirs(volume, exist_ok=True)
        for name in sorted(os.listdir(volume)):
            if name.endswith(".duckdb"):
                slug = name[: -len(".duckdb")]
                # A hand-dropped UPPER.duckdb or similar never passed add()'s
                # validator; skip it rather than register a slug the URL and
                # form-field charset check would have refused.
                if not _SLUG.fullmatch(slug):
                    continue
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

    thread = threading.Thread(target=run, name=f"refresh-{project.slug}", daemon=True)
    try:
        thread.start()
    except BaseException:
        # run()'s own finally is what normally releases this; if the thread
        # never actually started, that finally never runs, and the lock would
        # stay held until the process restarts.
        project.lock.release()
        raise
    return True
