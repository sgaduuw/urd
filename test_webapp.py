import os
import re
import tempfile

import render
import test_helpers
import urd


def _configured_db():
    return test_helpers.configured_db()


def test_report_html_returns_what_report_writes():
    """One rendering path, not two. If these ever diverge, the served page and the
    archived file stop being the same report."""
    con = _configured_db()
    urd.derive(con, "To Do,In Progress,Review,Done", "In Progress", "Review")
    path = os.path.join(tempfile.mkdtemp(), "r.html")
    urd.report(con, path)
    with open(path) as fh:
        written = fh.read()
    assert urd.report_html(con) == written


def test_report_html_writes_no_file():
    """`report` defaults to writing report.html in the working directory. The
    server calls this thousands of times, so it must not touch the disk at all."""
    con = _configured_db()
    urd.derive(con, "To Do,In Progress,Review,Done", "In Progress", "Review")
    workdir = tempfile.mkdtemp()
    was = os.getcwd()
    os.chdir(workdir)
    try:
        urd.report_html(con)
        assert os.listdir(workdir) == [], os.listdir(workdir)
    finally:
        os.chdir(was)


def test_a_notice_is_a_whole_page_not_a_fragment():
    """First-run states are pages a browser lands on, so they need the doctype and
    the stylesheet the report has, or they arrive unstyled."""
    out = render.notice("Nothing synced yet", ["Press Refresh."])
    assert out.startswith("<!doctype html>")
    assert "</html>" in out.strip()[-16:]
    assert "<style>" in out
    assert "Nothing synced yet" in out
    assert "Press Refresh." in out


def test_a_notice_escapes_what_it_is_given():
    out = render.notice("<script>x</script>", ["<b>bold</b>"])
    assert "<script>x</script>" not in out
    assert "&lt;script&gt;" in out
    assert "<b>bold</b>" not in out


def test_a_notice_can_offer_a_post_action():
    """Refresh has to be a POST, so a notice cannot offer it as a plain link."""
    out = render.notice("Never synced", ["Nothing here yet."],
                        actions=[("Refresh", "/alpha/refresh", "post")])
    assert 'method="post"' in out
    assert 'action="/alpha/refresh"' in out
    assert "Refresh" in out


def test_a_notice_fetches_nothing():
    out = render.notice("Title", ["Body"], actions=[("Go", "/x", "get")])
    without_anchors = re.sub(r"<a\b[^>]*>", "", out)
    for pattern in (r"\bsrc\s*=", r"@import", r"url\(", r"\bfetch\s*\("):
        assert not re.search(pattern, without_anchors), pattern


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all tests passed")
