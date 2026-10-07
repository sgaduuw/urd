"""The unified gate must discover failures and refuse incomplete suites."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).parent
REQUIRED = (
    "test_urd.py", "test_projects.py", "test_wizard.py", "test_container.py",
    "test_webapp.py", "test_views_report.py", "test_views_jobs.py", "test_views_wizard.py",
    "test_security.py", "test_sync_safety.py", "test_checks.py",
    "test_capacity.py", "test_capacity_history.py", "test_capacity_catalogue.py",
    "test_views_capacity.py", "test_capacity_report.py",
)


def test_gate_rejects_missing_tests_and_propagates_new_failures():
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        (root / "tests").mkdir()
        shutil.copy(ROOT / "tests/run.sh", root / "tests/run.sh")
        (root / "tests/no-leaks.sh").write_text("exit 0\n")
        (root / "requirements.txt").touch()
        for name in REQUIRED:
            (root / name).write_text("assert __debug__\n")
        # Avoid downloading tools or recursively executing this check. Python
        # test subprocesses still run for real, including failing assertions.
        bindir = root / "bin"
        bindir.mkdir()
        (bindir / "uv").write_text(
            '#!/bin/sh\n[ "$1" = tool ] && exit 0\n'
            'while [ "$1" != python ]; do shift; done\nshift\n'
            f'exec "{sys.executable}" "$@"\n'
        )
        (bindir / "shellcheck").write_text("#!/bin/sh\nexit 0\n")
        for executable in bindir.iterdir():
            executable.chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "PYTHONOPTIMIZE": "1"}

        def run():
            return subprocess.run(
                ["sh", "tests/run.sh"], cwd=root, env=env,
                capture_output=True, text=True,
            )

        result = run()
        assert result.returncode == 0, result.stdout + result.stderr
        for gate in (bindir / "uv", bindir / "shellcheck", root / "tests/no-leaks.sh"):
            original = gate.read_text()
            gate.write_text("#!/bin/sh\nexit 9\n")
            result = run()
            assert result.returncode != 0, f"gate failure ignored: {gate.name}"
            gate.write_text(original)
        (root / "test_new.py").write_text('assert False, "new test ran"\n')
        result = run()
        assert result.returncode != 0 and "new test ran" in result.stdout, result
        (root / "test_new.py").unlink()
        (root / REQUIRED[0]).unlink()
        result = run()
        assert result.returncode != 0, "missing required test silently passed"


if __name__ == "__main__":
    test_gate_rejects_missing_tests_and_propagates_new_failures()
    print("all tests passed")
